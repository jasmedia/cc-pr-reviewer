#!/usr/bin/env python3
"""
cc-pr-reviewer — a small Textual TUI that lists GitHub PRs where you are a
requested reviewer and hands any selected PR off to Claude Code, with the
PR Review Toolkit plugin driving the review.

Flow when you pick a PR and press Enter:
    1. Clone the repo into $GH_PR_WORKSPACE (if not already there)
    2. `gh pr checkout <N>` so Claude sees the PR's working tree
    3. Launch `claude` with a prompt that invokes the PR Review Toolkit agents
    4. When you /exit Claude, the TUI resumes

Prerequisites:
    • gh CLI, authenticated                     https://cli.github.com
    • claude CLI (Claude Code)                  https://docs.claude.com/claude-code
    • PR Review Toolkit plugin installed        https://claude.com/plugins/pr-review-toolkit

Run:
    uv sync
    uv run cc-pr-reviewer
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static
from textual.widgets.option_list import Option

# --- Configuration ---------------------------------------------------------

WORKSPACE = Path(os.environ.get("GH_PR_WORKSPACE", Path.home() / "gh-pr-workspace"))
REVIEW_DB_PATH = WORKSPACE / ".review_state.db"

# The prompt we hand Claude Code when it starts up in the PR's working tree.
# The PR Review Toolkit plugin will pick up on these cues and route to the
# right sub-agents.
REVIEW_PROMPT = (
    "Please perform a comprehensive review of the current PR using the "
    "PR Review Toolkit. Run the relevant agents — Comment Analyzer, PR Test "
    "Analyzer, Silent Failure Hunter, Type Design Analyzer, Code Reviewer, "
    "and Code Simplifier — and give me a prioritised summary of findings "
    "with file:line references and suggested fixes."
)

POST_INLINE_PROMPT = (
    "Additionally, publish each finding as an inline PR review comment on "
    "GitHub using the `gh` CLI. Create a single pending review via `gh api "
    "--method POST /repos/{owner}/{repo}/pulls/{number}/reviews` with an array "
    "of `comments` entries (each with `path`, `line`, and `body`), then submit "
    "the review with `event: COMMENT` so all findings appear grouped. Use the "
    "PR's head commit SHA when the endpoint requires `commit_id`."
)

# Appended to POST_INLINE_PROMPT only when we actually have existing comments
# to cross-reference (see `_launch_claude`). Without that list the instruction
# is meaningless and can cause hallucinated caution.
POST_INLINE_DEDUP_SUFFIX = (
    " Before submitting, cross-check each finding against the list of existing "
    "review comments above and drop any that duplicate a previously posted "
    "comment (same file+line+substantive point)."
)

# Appended to POST_INLINE_PROMPT when the existing-comments fetch failed. The
# alternative (empty `existing_block`) is indistinguishable from a PR that
# genuinely has no prior comments, so without this hint Claude would happily
# repost routine findings that were already flagged in the missing list.
POST_INLINE_FETCH_FAILED_SUFFIX = (
    " NOTE: existing-comment fetch failed, so no dedup list is available. "
    "Err on the side of not reposting findings that look routine or commonly "
    "raised; prefer fewer, clearly novel comments."
)

# Prompt sections are joined with this separator so multi-line blocks (e.g.
# the existing-comments list) don't get smashed into neighboring prose.
PROMPT_SECTION_SEP = "\n\n"

# Caps for the existing-comments block injected into the prompt. Bound prompt
# size on PRs with lots of prior review activity.
EXISTING_COMMENT_BODY_CAP = 200
EXISTING_COMMENT_LIST_CAP = 50

# Update check: once per startup, silent on failure.
PACKAGE_NAME = "cc-pr-reviewer"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
RELEASES_URL = "https://github.com/jasmedia/cc-pr-reviewer/releases"


def _installed_version() -> str | None:
    try:
        return _pkg_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return None


def _fetch_latest_version(timeout: float = 3.0) -> str | None:
    try:
        req = urllib.request.Request(PYPI_JSON_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
        v = data.get("info", {}).get("version")
        return v if isinstance(v, str) else None
    except Exception:  # noqa: BLE001
        return None


def _parse_semver(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_semver(latest) > _parse_semver(current)


# --- Subprocess helpers ----------------------------------------------------


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout/stderr as text."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


PR_REVIEW_TOOLKIT_PLUGIN = "pr-review-toolkit"
PR_REVIEW_TOOLKIT_URL = "https://claude.com/plugins/pr-review-toolkit"


def _pr_review_toolkit_enabled() -> bool | None:
    """True if the plugin is installed & enabled, False if not, None if undetectable."""
    r = run(["claude", "plugin", "list", "--json"])
    if r.returncode != 0:
        return None
    try:
        plugins = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for p in plugins:
        pid = p.get("id", "")
        # id format is "<plugin-name>@<marketplace>"; match by plugin name.
        if pid.split("@", 1)[0] == PR_REVIEW_TOOLKIT_PLUGIN and p.get("enabled"):
            return True
    return False


def check_prereqs() -> list[str]:
    """Return a list of human-readable problems (empty if everything is ready)."""
    problems: list[str] = []
    if shutil.which("gh") is None:
        problems.append("`gh` CLI not found on PATH — install from https://cli.github.com")
    else:
        if run(["gh", "auth", "status"]).returncode != 0:
            problems.append("`gh` is not authenticated — run: gh auth login")
    if shutil.which("claude") is None:
        problems.append("`claude` CLI not found on PATH — install Claude Code")
    else:
        enabled = _pr_review_toolkit_enabled()
        if enabled is False:
            problems.append(
                "PR Review Toolkit plugin not enabled — "
                f"install & enable from {PR_REVIEW_TOOLKIT_URL} "
                f"(or run: claude plugin install {PR_REVIEW_TOOLKIT_PLUGIN})"
            )
    if shutil.which("git") is None:
        problems.append("`git` not found on PATH — install git")
    return problems


PR_FIELDS = "number,title,repository,author,url,updatedAt,isDraft"


def _search_prs(extra: list[str]) -> list[dict[str, Any]]:
    r = run(
        [
            "gh",
            "search",
            "prs",
            "--state=open",
            "--archived=false",
            "--limit=100",
            "--json",
            PR_FIELDS,
            *extra,
        ]
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "gh search prs failed")
    return json.loads(r.stdout or "[]")


def _repo_filter_arg(repo: str | None) -> list[str]:
    repo = (repo or "").strip()
    return [f"--repo={repo}"] if repo else []


def fetch_review_prs(repo: str | None = None) -> list[dict[str, Any]]:
    """All open PRs across GitHub where @me is a requested reviewer."""
    return _search_prs(["--review-requested=@me", *_repo_filter_arg(repo)])


def fetch_my_prs(repo: str | None = None) -> list[dict[str, Any]]:
    """All open PRs across GitHub authored by @me."""
    return _search_prs(["--author=@me", *_repo_filter_arg(repo)])


def fetch_existing_review_comments(repo: str, number: int) -> tuple[list[dict[str, Any]], bool]:
    """Inline review comments already posted on the PR.

    Returns `(comments, ok)`. `ok=False` with a printed warning on
    transport/parse failure (non-zero exit, JSONDecodeError, non-list
    payload) so the caller can tell Claude dedup context is missing.
    `ok=True, comments=[]` means the PR genuinely has no inline comments.
    """
    # Single page (per_page=100 is GitHub's max). PRs with >100 inline
    # comments will miss the oldest ones; acceptable because we only
    # surface EXISTING_COMMENT_LIST_CAP most-recent entries anyway.
    r = run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{number}/comments?per_page=100",
        ]
    )
    target = f"{repo}#{number}"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
        print(f"warning: could not fetch existing comments for {target}: {err}")
        return [], False
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        print(f"warning: could not parse existing comments for {target}: {e}")
        return [], False
    if not isinstance(data, list):
        print(f"warning: unexpected comments payload for {target}: {type(data).__name__}")
        return [], False
    return data, True


def format_existing_comments(comments: list[dict[str, Any]]) -> tuple[str, int]:
    """Compact prompt block listing up to `EXISTING_COMMENT_LIST_CAP` most
    recent inline review comments (bodies truncated to
    `EXISTING_COMMENT_BODY_CAP` chars).

    Returns `(block, shown_count)` where `shown_count` is the number of
    entries actually rendered into `block`. Returns `("", 0)` when no
    usable entries remain after filtering.
    """
    # Drop entries we can't render a useful dedup anchor for:
    # - missing created_at: mixing None with strings TypeErrors sorted(),
    #   and substituting "" would silently reorder malformed entries.
    # - missing path: would render as a bare ":N" locus that Claude can't
    #   match against — dedup silently no-ops for that entry.
    # - empty/whitespace body: leaves Claude a locus with no substance, so
    #   any new finding at that location trivially passes the "clearly new
    #   info" test, defeating dedup.
    usable: list[tuple[dict[str, Any], str]] = []
    for c in comments:
        if not c.get("created_at") or not c.get("path"):
            continue
        body = " ".join((c.get("body") or "").split())
        if not body:
            continue
        usable.append((c, body))
    if not usable:
        return "", 0
    usable.sort(key=lambda cb: cb[0]["created_at"], reverse=True)
    truncated = len(usable) > EXISTING_COMMENT_LIST_CAP
    shown = usable[:EXISTING_COMMENT_LIST_CAP]

    lines = [
        "Existing review comments already posted on this PR (do NOT repost "
        "duplicates; you may extend or refine with clearly new info):"
    ]
    for c, body in shown:
        user = (c.get("user") or {}).get("login", "?")
        path = c["path"]
        # Outdated comments (line no longer in the PR's current diff) null
        # out `line` but keep `original_line`; fall back so we still emit a
        # locus, and label (outdated) so Claude doesn't suppress a legit
        # new finding on a line that's since been rewritten.
        line_no = c.get("line")
        if line_no is not None:
            locus = f"{path}:{line_no}"
        elif c.get("original_line") is not None:
            locus = f"{path}:{c['original_line']} (outdated)"
        else:
            locus = f"{path} (file-level)"
        if len(body) > EXISTING_COMMENT_BODY_CAP:
            body = body[: EXISTING_COMMENT_BODY_CAP - 1] + "…"
        lines.append(f'- @{user} on {locus} — "{body}"')
    if truncated:
        lines.append(f"(showing {EXISTING_COMMENT_LIST_CAP} most recent of {len(usable)} total)")
    return "\n".join(lines), len(shown)


def humanise(iso: str) -> str:
    """'2025-04-18T10:30:00Z' -> '3h'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# --- Review state (SQLite) -------------------------------------------------


def _pr_key(pr: dict[str, Any]) -> str:
    return f"{pr['repository']['nameWithOwner']}#{pr['number']}"


def _open_review_db() -> sqlite3.Connection:
    REVIEW_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REVIEW_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            pr_key TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_reviewed_at TEXT NOT NULL,
            last_pr_updated_at TEXT NOT NULL,
            last_head_sha TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def _load_review_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM reviews").fetchall()
    return {r["pr_key"]: dict(r) for r in rows}


def _record_review(
    conn: sqlite3.Connection,
    pr_key: str,
    pr_updated_at: str,
    head_sha: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO reviews (pr_key, count, last_reviewed_at, last_pr_updated_at, last_head_sha)
        VALUES (?, 1, ?, ?, ?)
        ON CONFLICT(pr_key) DO UPDATE SET
            count = count + 1,
            last_reviewed_at = excluded.last_reviewed_at,
            last_pr_updated_at = excluded.last_pr_updated_at,
            last_head_sha = excluded.last_head_sha
        """,
        (pr_key, now, pr_updated_at, head_sha or None),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reviews WHERE pr_key = ?", (pr_key,)).fetchone()
    return dict(row)


def _review_cell(pr: dict[str, Any], state: dict[str, dict[str, Any]]) -> str:
    entry = state.get(_pr_key(pr))
    if not entry:
        return "-"
    count = entry.get("count", 0)
    stored_updated = entry.get("last_pr_updated_at", "")
    current_updated = pr.get("updatedAt", "")
    stale = stored_updated and current_updated and current_updated != stored_updated
    return f"{count} stale" if stale else str(count)


def _last_reviewed_cell(pr: dict[str, Any], state: dict[str, dict[str, Any]]) -> str:
    entry = state.get(_pr_key(pr))
    if not entry:
        return ""
    iso = entry.get("last_reviewed_at", "")
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


# --- Diff modal ------------------------------------------------------------


class DiffScreen(ModalScreen):
    """A full-screen view of `gh pr diff` output."""

    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, repo: str, number: int):
        super().__init__()
        self.repo = repo
        self.number = number

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                f"Diff • {self.repo}#{self.number}   (q or Esc to close)",
                id="diff-title",
            ),
            Static("Loading diff…", id="diff-body"),
            id="diff-container",
        )

    def on_mount(self) -> None:
        self._load_diff()

    @work(thread=True)
    def _load_diff(self) -> None:
        r = run(["gh", "pr", "diff", str(self.number), "--repo", self.repo])
        body = r.stdout if r.returncode == 0 else f"Error:\n{r.stderr}"
        self.app.call_from_thread(
            self.query_one("#diff-body", Static).update,
            body or "(empty diff)",
        )


# --- Confirm modal ---------------------------------------------------------


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of a confirmed ConfirmScreen — distinct from cancel (None)."""

    post_inline: bool


class ConfirmScreen(ModalScreen[ConfirmResult | None]):
    """Confirm Claude Code launch for a PR review, with a post-inline toggle.

    Dismisses with None on cancel, or a ConfirmResult on confirm. Keeping
    cancel and confirm in separate shapes prevents a future truthy check
    (`if result:`) from silently swallowing the post-inline-off case.
    """

    BINDINGS = [
        Binding("enter,y", "confirm", "Yes"),
        Binding("escape,n,q", "cancel", "No"),
        Binding("p", "toggle_post_inline", "Toggle post inline"),
    ]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt
        self.post_inline = True

    def compose(self) -> ComposeResult:
        hint = (
            "[b]Enter[/] / [b]y[/] to proceed • [b]Esc[/] / [b]n[/] to cancel "
            "• [b]p[/] to toggle post-inline"
        )
        yield Vertical(
            Label(self.prompt, id="confirm-title"),
            Label(self._checkbox_text(), id="confirm-checkbox", markup=False),
            Label(hint, id="confirm-hint"),
            id="confirm-container",
        )

    def _checkbox_text(self) -> str:
        mark = "[x]" if self.post_inline else "[ ]"
        return f"{mark} Post findings as inline PR comments"

    def action_confirm(self) -> None:
        self.dismiss(ConfirmResult(post_inline=self.post_inline))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_post_inline(self) -> None:
        self.post_inline = not self.post_inline
        self.query_one("#confirm-checkbox", Label).update(self._checkbox_text())


# --- Filter modal ----------------------------------------------------------


# Real GitHub `nameWithOwner` values always contain '/', so this sentinel
# can't collide with a real repo id used on the OptionList.
CLEAR_FILTER_OPTION_ID = "__clear__"


@dataclass(frozen=True)
class FilterChoice:
    """Outcome of a confirmed FilterScreen — distinct from cancel (None).

    `repo=None` means "clear filter"; `repo="owner/name"` means "apply".
    Mirrors `ConfirmResult` so a future truthy check at the call site can't
    silently swallow the clear case.
    """

    repo: str | None


class FilterScreen(ModalScreen[FilterChoice | None]):
    """Pick a repo from the cached list to filter the PR view.

    Dismisses with a FilterChoice on Enter, or None on Esc. Press `r` inside
    the modal to re-fetch the unfiltered PR list and pick up repos that
    weren't present at boot.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("r", "refresh", "Refresh repos"),
    ]

    _BASE_TITLE = "Filter PRs by repo"

    def __init__(
        self,
        repos: list[str],
        current: str | None,
        refresh_repos: Callable[[], list[str]],
    ):
        super().__init__()
        self.repos = repos
        self.current = current
        self._refresh_repos = refresh_repos
        self._refreshing = False

    def compose(self) -> ComposeResult:
        title = self._BASE_TITLE if self.repos else f"{self._BASE_TITLE} (no repos cached yet)"
        yield Vertical(
            Label(title, id="filter-title"),
            OptionList(*self._build_options(), id="filter-list"),
            Label(
                "[b]Enter[/] select • [b]r[/] refresh • [b]Esc[/] cancel",
                id="filter-hint",
            ),
            id="filter-container",
        )

    def _build_options(self) -> list[Option]:
        options: list[Option] = [Option("(any repo — clear filter)", id=CLEAR_FILTER_OPTION_ID)]
        for repo in self.repos:
            options.append(Option(repo, id=repo))
        return options

    def on_mount(self) -> None:
        ol = self.query_one("#filter-list", OptionList)
        self._highlight_current(ol)
        ol.focus()

    def _highlight_current(self, ol: OptionList) -> None:
        if self.current and self.current in self.repos:
            ol.highlighted = self.repos.index(self.current) + 1  # +1 for the clear row
        else:
            ol.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id
        repo = None if chosen == CLEAR_FILTER_OPTION_ID else chosen
        self.dismiss(FilterChoice(repo=repo))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.query_one("#filter-title", Label).update(f"{self._BASE_TITLE} (refreshing…)")
        self._do_refresh()

    @work(thread=True, exclusive=True)
    def _do_refresh(self) -> None:
        try:
            repos = self._refresh_repos()
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._refresh_failed, str(e))
            return
        self.app.call_from_thread(self._apply_refresh, repos)

    def _apply_refresh(self, repos: list[str]) -> None:
        self.repos = repos
        self.app.repo_cache = tuple(repos)  # type: ignore[attr-defined]
        ol = self.query_one("#filter-list", OptionList)
        ol.clear_options()
        ol.add_options(self._build_options())
        self._highlight_current(ol)
        self.query_one("#filter-title", Label).update(self._BASE_TITLE)
        self._refreshing = False

    def _refresh_failed(self, err: str) -> None:
        truncated = err.strip().splitlines()[0][:80] if err.strip() else "unknown error"
        self.query_one("#filter-title", Label).update(
            f"{self._BASE_TITLE} (refresh failed: {truncated})"
        )
        if err.strip():
            self.app.notify(err, severity="error", timeout=10)
        self._refreshing = False


# --- Main app --------------------------------------------------------------


class PRDataTable(DataTable):
    # `action_select_cursor` is the Enter-key handler only; clicks go through
    # `_on_click`, which posts `RowSelected` directly. Overriding here routes
    # Enter to review while leaving mouse clicks as pure cursor moves.
    def action_select_cursor(self) -> None:
        self.app.action_review()  # type: ignore[attr-defined]


class PRReviewer(App):
    CSS = """
    Screen { background: $surface; }
    DataTable { height: 1fr; }
    #status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-darken-1;
        color: $text;
    }
    #status.-error { background: $error; }
    #version-badge {
        height: 1;
        width: 100%;
        content-align: right middle;
        padding: 0 1;
        background: $accent;
        color: $text;
        text-style: bold;
        display: none;
    }
    #version-badge.-visible { display: block; }
    #diff-container {
        border: round $primary;
        padding: 1;
        margin: 2 4;
        background: $panel;
    }
    #diff-title {
        text-style: bold;
        margin-bottom: 1;
        color: $accent;
    }
    #diff-body {
        height: 1fr;
        overflow-y: auto;
        padding: 1;
    }
    #confirm-container, #filter-container {
        border: round $primary;
        padding: 1 2;
        margin: 4 8;
        background: $panel;
        height: auto;
    }
    #confirm-title, #filter-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-hint, #filter-hint {
        color: $text-muted;
    }
    #filter-list {
        height: auto;
        max-height: 20;
        margin: 1 0;
    }
    #filter-hint {
        margin-top: 1;
    }
    """

    TITLE = "GitHub PR Reviewer"
    SUB_TITLE = "Review PRs with Claude Code"

    BINDINGS = [
        Binding("r,f5", "refresh", "Refresh"),
        Binding("enter", "review", "Review w/ Claude"),
        Binding("o", "open_web", "Open in browser"),
        Binding("d", "show_diff", "View diff"),
        Binding("m", "toggle_mine", "Toggle my PRs"),
        Binding("f", "filter", "Filter by repo"),
        Binding("u", "open_releases", "Releases"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[dict[str, Any]] = []
        self.include_mine: bool = False
        # Immutable: rebound wholesale, never mutated in place (writers must
        # swap a fresh tuple to stay safe across the FilterScreen worker).
        self.repo_cache: tuple[str, ...] = ()
        self.latest_version: str | None = None
        self.review_db: sqlite3.Connection = _open_review_db()
        self.review_state: dict[str, dict[str, Any]] = _load_review_state(self.review_db)
        stored_filter = _get_setting(self.review_db, "repo_filter", "")
        self.repo_filter: str | None = stored_filter or None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="version-badge")
        yield PRDataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Static("Loading…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.add_columns(
            "Repository", "#", "Title", "Author", "Updated", "Reviews", "Last Review", ""
        )
        self.action_refresh()
        self._check_for_update()

    # --- actions ---

    def action_refresh(self) -> None:
        self._set_status("Refreshing…")
        self._load_prs()

    @work(thread=True, exclusive=True)
    def _load_prs(self) -> None:
        repo = self.repo_filter
        try:
            data = fetch_review_prs(repo)
            if self.include_mine:
                seen = {(p["repository"]["nameWithOwner"], p["number"]) for p in data}
                for pr in fetch_my_prs(repo):
                    key = (pr["repository"]["nameWithOwner"], pr["number"])
                    if key in seen:
                        continue
                    pr["_mine"] = True
                    data.append(pr)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"Error: {e}", True)
            return
        self.call_from_thread(self._populate, data)

    def _filter_desc(self) -> str:
        return f" [repo={self.repo_filter}]" if self.repo_filter else ""

    def _populate(self, data: list[dict[str, Any]]) -> None:
        self.prs = data
        # Refresh repo cache only on unfiltered fetches; otherwise it would
        # shrink to whatever the active filter happens to allow. An empty
        # result is a legitimate "no repos" signal, so don't gate on `data`.
        if self.repo_filter is None:
            self.repo_cache = tuple(sorted({pr["repository"]["nameWithOwner"] for pr in data}))
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        mode = " (+mine)" if self.include_mine else ""
        filter_desc = self._filter_desc()
        if not data:
            self._set_status(
                f"No PRs awaiting your review 🎉{mode}{filter_desc}   "
                "(f: filter, m: mine, r: refresh, u: releases, q: quit)"
            )
            return
        for i, pr in enumerate(data):
            repo = pr["repository"]["nameWithOwner"]
            num = pr["number"]
            title = pr["title"]
            if len(title) > 70:
                title = title[:67] + "…"
            author = (pr.get("author") or {}).get("login", "?")
            updated = humanise(pr.get("updatedAt", ""))
            tags = []
            if pr.get("_mine"):
                tags.append("MINE")
            if pr.get("isDraft"):
                tags.append("DRAFT")
            reviews = _review_cell(pr, self.review_state)
            last_reviewed = _last_reviewed_cell(pr, self.review_state)
            table.add_row(
                repo,
                f"#{num}",
                title,
                author,
                updated,
                reviews,
                last_reviewed,
                " ".join(tags),
                key=str(i),
            )
        self._set_status(
            f"{len(data)} PR(s){mode}{filter_desc}   "
            "•  enter: review  •  d: diff  •  o: browser  •  f: filter  •  m: mine  "
            "•  r: refresh  •  u: releases  •  q: quit"
        )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#pr-table", DataTable)
        if table.row_count == 0:
            return None
        return self.prs[table.cursor_row]

    def action_open_web(self) -> None:
        pr = self._selected()
        if pr:
            webbrowser.open(pr["url"])

    def action_open_releases(self) -> None:
        webbrowser.open(RELEASES_URL)

    def action_show_diff(self) -> None:
        pr = self._selected()
        if pr:
            self.push_screen(DiffScreen(pr["repository"]["nameWithOwner"], pr["number"]))

    def action_review(self) -> None:
        pr = self._selected()
        if not pr:
            return
        repo = pr["repository"]["nameWithOwner"]
        title = pr.get("title", "")
        prompt = f"Launch Claude Code review for {repo}#{pr['number']}?\n{title}"

        def _proceed(result: ConfirmResult | None) -> None:
            if result is not None:
                self._launch_claude(pr, result.post_inline)

        self.push_screen(ConfirmScreen(prompt), _proceed)

    def action_toggle_mine(self) -> None:
        self.include_mine = not self.include_mine
        self.action_refresh()

    def action_filter(self) -> None:
        def _apply(result: FilterChoice | None) -> None:
            if result is None or result.repo == self.repo_filter:
                return
            if self._set_repo_filter(result.repo):
                self.action_refresh()

        self.push_screen(
            FilterScreen(list(self.repo_cache), self.repo_filter, self._fetch_unfiltered_repos),
            _apply,
        )

    def _fetch_unfiltered_repos(self) -> list[str]:
        """Blocking re-fetch of the unfiltered PR list; returns the new
        sorted repo list. The caller (FilterScreen._apply_refresh, on the
        main thread) owns the assignment to `repo_cache` so we don't mutate
        App state from a worker thread.

        Mirrors `_load_prs`'s scope: honors the current `include_mine`, so
        toggling `m` (which requires closing the modal first, since modals
        shadow App bindings) and re-opening will widen the cache on the next
        refresh.
        """
        repos = {pr["repository"]["nameWithOwner"] for pr in fetch_review_prs(None)}
        if self.include_mine:
            for pr in fetch_my_prs(None):
                repos.add(pr["repository"]["nameWithOwner"])
        return sorted(repos)

    def _set_repo_filter(self, value: str | None) -> bool:
        # Persist first so a write failure can't leave session and disk
        # diverged; warn and keep the prior value on failure.
        try:
            _set_setting(self.review_db, "repo_filter", value or "")
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist filter: {e}", severity="warning")
            return False
        self.repo_filter = value
        return True

    # --- launching claude ---

    def _launch_claude(self, pr: dict[str, Any], post_inline: bool) -> None:
        repo_full = pr["repository"]["nameWithOwner"]
        owner, name = repo_full.split("/", 1)
        number = pr["number"]
        local_path = WORKSPACE / owner / name

        # Suspend the TUI so Claude Code can take over stdin/stdout.
        with self.suspend():
            WORKSPACE.mkdir(parents=True, exist_ok=True)
            print(f"\n── Reviewing {repo_full}#{number} ──\n")

            if not local_path.exists():
                print(f"Cloning {repo_full} → {local_path}…")
                if subprocess.call(["gh", "repo", "clone", repo_full, str(local_path)]) != 0:
                    input("\nClone failed. Press Enter to return…")
                    return
            else:
                print(f"Fetching latest into {local_path}…")
                subprocess.call(["git", "fetch", "--all", "--prune"], cwd=local_path)

            print(f"\nChecking out PR #{number}…")
            if (
                subprocess.call(
                    ["gh", "pr", "checkout", str(number), "--force"],
                    cwd=local_path,
                )
                != 0
            ):
                input("\nCheckout failed. Press Enter to return…")
                return

            sha_r = run(["git", "rev-parse", "HEAD"], cwd=local_path)
            if sha_r.returncode != 0:
                err = (sha_r.stderr or sha_r.stdout).strip() or f"exit {sha_r.returncode}"
                print(f"warning: could not resolve HEAD in {local_path}: {err}")
                head_sha = ""
            else:
                head_sha = sha_r.stdout.strip()

            print("Fetching existing review comments…")
            existing, fetch_ok = fetch_existing_review_comments(repo_full, number)
            existing_block, shown = format_existing_comments(existing)

            sections = [REVIEW_PROMPT]
            if existing_block:
                sections.append(existing_block)
            if post_inline:
                post = POST_INLINE_PROMPT
                if existing_block:
                    post += POST_INLINE_DEDUP_SUFFIX
                elif not fetch_ok:
                    post += POST_INLINE_FETCH_FAILED_SUFFIX
                sections.append(post)
            prompt = PROMPT_SECTION_SEP.join(sections)

            if not fetch_ok:
                existing_desc = "existing comments: fetch failed"
            else:
                existing_desc = f"existing comments: {shown} in prompt of {len(existing)} fetched"

            cmd = ["claude", "--permission-mode", "acceptEdits", prompt]
            print(
                f"\nLaunching Claude Code "
                f"(post-inline: {'on' if post_inline else 'off'}, "
                f"{existing_desc}) "
                "— type /exit when you're done.\n"
            )
            rc = subprocess.call(cmd, cwd=local_path)

            # Only count this as a review if Claude exited cleanly. Ctrl-C,
            # crashes, or a failed launch leave rc != 0; recording those
            # would inflate the "Reviews" count and reset staleness for a
            # PR that wasn't actually reviewed, hiding genuine drift from
            # the next real session.
            if rc == 0:
                key = _pr_key(pr)
                self.review_state[key] = _record_review(
                    self.review_db,
                    key,
                    pr.get("updatedAt", ""),
                    head_sha,
                )
            else:
                print(f"\nClaude exited with status {rc}; not recording this as a review.")

            input("\n── Claude session ended. Press Enter to return to the TUI ──")

        # Refresh in case review state changed (e.g. you approved the PR).
        self.action_refresh()

    # --- helpers ---

    def _set_status(self, msg: str, error: bool = False) -> None:
        w = self.query_one("#status", Static)
        w.update(msg)
        w.set_class(error, "-error")

    @work(thread=True, exclusive=True)
    def _check_for_update(self) -> None:
        current = _installed_version()
        if current is None:
            return
        latest = _fetch_latest_version()
        if latest and _is_newer(latest, current):
            self.call_from_thread(self._show_update_badge, latest)

    def _show_update_badge(self, latest: str) -> None:
        self.latest_version = latest
        w = self.query_one("#version-badge", Static)
        w.update(f" ▲ v{latest} available — press u ")
        w.add_class("-visible")


# --- Entry point -----------------------------------------------------------


def main() -> None:
    problems = check_prereqs()
    if problems:
        print("⚠  Prerequisites not met:\n")
        for p in problems:
            print(f"  • {p}")
        print(
            "\nAlso make sure the PR Review Toolkit plugin is installed in Claude Code:"
            "\n  https://claude.com/plugins/pr-review-toolkit\n"
        )
        raise SystemExit(1)
    PRReviewer().run()


if __name__ == "__main__":
    main()
