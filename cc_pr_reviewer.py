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

import contextlib
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
from typing import Any, Literal

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Link,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets._footer import FooterKey
from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderIcon, HeaderTitle
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

# Appended to the post-inline prompt only when the existing inline review
# comments (from `pulls/{n}/comments` — top-level review bodies and issue
# comments are NOT included) contain at least one entry authored by the
# current `gh` user — i.e. this tool has already reviewed this PR before.
# Third-party review comments don't count; we don't want to raise the bar
# just because someone else commented.
POST_INLINE_REREVIEW_SUFFIX = (
    " You (the authenticated `gh` user) have already reviewed this PR in a "
    "previous pass, so raise the bar: only post findings that are clearly "
    "important (e.g., correctness, security, data loss, broken contracts, "
    "breaking changes, concurrency bugs, resource leaks, significant "
    "performance regressions). Skip anything minor, stylistic, or NIT-level."
)

# Appended after POST_INLINE_REREVIEW_SUFFIX only when the PR is NOT
# self-authored. GitHub returns 422 ("Can not approve your own pull request")
# on `event: APPROVE` for the author, so this clause is unsafe to send when
# the `gh` user is the PR author — but the raised bar above still applies.
POST_INLINE_REREVIEW_APPROVE_SUFFIX = (
    " If after filtering the only remaining findings are minor or NIT-level, "
    "submit an APPROVE review with no inline comments (use `event: APPROVE` "
    "and omit the `comments` array) instead of `event: COMMENT`."
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

# Cap for the reviewer-supplied extra prompt echoed in the launch banner.
# The full text still goes to claude; this only bounds the on-screen preview
# so the banner stays one terminal row on long pastes.
EXTRA_PROMPT_BANNER_CAP = 200

# Update check: once per startup, silent on failure.
PACKAGE_NAME = "cc-pr-reviewer"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
RELEASES_URL = "https://github.com/jasmedia/cc-pr-reviewer/releases"
CHANGELOG_URL = "https://github.com/jasmedia/cc-pr-reviewer/blob/main/CHANGELOG.md"

# Single source of truth for the group-by cycle. Adding a new mode means
# editing only this dict — `__init__`'s whitelist load and the toggle action
# both consult it, so the legal-value list never drifts.
GroupBy = Literal["", "repo", "author"]
_GROUP_CYCLE: dict[GroupBy, GroupBy] = {"": "repo", "repo": "author", "author": ""}

# Same pattern for the sort cycle. "" preserves the natural order from the
# data sources (best-match for `gh search prs`, updated-desc from the my-PRs
# GraphQL query); "updated" sorts the merged list by `updatedAt` descending.
SortBy = Literal["", "updated"]
_SORT_CYCLE: dict[SortBy, SortBy] = {"": "updated", "updated": ""}


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


_MY_PRS_PAGE_SIZE = 100

_MY_PRS_GRAPHQL = f"""
query {{
  viewer {{
    pullRequests(
      first: {_MY_PRS_PAGE_SIZE},
      states: OPEN,
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{
      pageInfo {{ hasNextPage }}
      nodes {{
        number
        title
        url
        updatedAt
        isDraft
        author {{ login }}
        repository {{ nameWithOwner isArchived }}
      }}
    }}
  }}
}}
"""


def fetch_my_prs(repo: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """All open PRs across GitHub authored by @me.

    Returns `(nodes, warning)`. `warning` is a non-fatal message the caller
    should surface as a toast — currently used for two cases:
      • the result was truncated at `first: _MY_PRS_PAGE_SIZE`,
      • GraphQL returned `errors` *with* surviving `data` (partial success).
    Hard failures (transport, parse, fully-failed query) still raise.

    Uses GraphQL `viewer.pullRequests` rather than `gh search prs --author=@me`
    because the GitHub Search API has indexing delays — recently-pushed PRs
    can be missing from search results for hours, especially in low-traffic
    personal repos. `viewer.pullRequests` reads the authoritative DB and
    surfaces PRs immediately on creation.
    """
    r = run(["gh", "api", "graphql", "-f", f"query={_MY_PRS_GRAPHQL}"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "gh api graphql failed")
    try:
        payload = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse graphql response: {e}") from e

    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    pull_requests = ((data or {}).get("viewer") or {}).get("pullRequests")
    errors = payload.get("errors") or []

    # `gh api graphql` exits 0 even when the GraphQL layer returns errors.
    # Distinguish hard failure (no usable data) from partial success: the
    # spec allows `data` and `errors` together (e.g. one inaccessible repo
    # while others succeed), and discarding everything would erase a 99/100
    # successful response on the strength of one bad node.
    if not pull_requests:
        if errors:
            msg = "; ".join(e.get("message", str(e)) for e in errors)
            raise RuntimeError(f"graphql errors: {msg}")
        raise RuntimeError("graphql response missing data.viewer.pullRequests")

    nodes = pull_requests.get("nodes") or []
    # Drop entries whose `repository` failed to resolve — `_load_prs` keys
    # on `repository.nameWithOwner` and would `TypeError`/`KeyError` here.
    nodes = [n for n in nodes if isinstance(n.get("repository"), dict)]
    # Match the `--archived=false` behavior of the search-based path.
    nodes = [n for n in nodes if not n["repository"].get("isArchived")]
    if repo:
        nodes = [n for n in nodes if n["repository"].get("nameWithOwner") == repo]

    warning_parts: list[str] = []
    if errors:
        msg = "; ".join(e.get("message", str(e)) for e in errors)
        warning_parts.append(f"my-PRs query returned with errors (showing partial data): {msg}")
    if (pull_requests.get("pageInfo") or {}).get("hasNextPage"):
        warning_parts.append(
            f"Showing {_MY_PRS_PAGE_SIZE} most-recently-updated of your open "
            "PRs; older ones omitted."
        )
    return nodes, ("; ".join(warning_parts) or None)


_GH_LOGIN: str | None = None


def _current_gh_login() -> str | None:
    """Login of the authenticated `gh` user, or None if undetectable.

    Cached on first success only — a transient `gh` failure (auth blip, network
    timeout) must not disable rereview detection for the rest of the session,
    so failures retry on the next call. Mirrors `fetch_existing_review_comments`
    by surfacing the underlying error to the user.
    """
    global _GH_LOGIN
    if _GH_LOGIN is not None:
        return _GH_LOGIN
    r = run(["gh", "api", "user", "--jq", ".login"])
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
        print(f"warning: could not detect gh login (rereview detection disabled): {err}")
        return None
    login = r.stdout.strip() or None
    _GH_LOGIN = login
    return login


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
            VerticalScroll(
                Static("Loading diff…", id="diff-body", markup=False),
                id="diff-scroll",
            ),
            id="diff-container",
        )

    def on_mount(self) -> None:
        # Focus the scroll container so arrow keys / PgUp / PgDn / Home / End
        # scroll the diff. `Static` isn't focusable, so without this the modal
        # would receive keys but have nowhere to send them.
        self.query_one("#diff-scroll", VerticalScroll).focus()
        self._load_diff()

    @work(thread=True)
    def _load_diff(self) -> None:
        # Catch broadly so an OSError / FileNotFoundError from `gh` doesn't
        # kill the worker silently and leave the modal stuck on "Loading…".
        try:
            r = run(["gh", "pr", "diff", str(self.number), "--repo", self.repo])
        except Exception as e:  # noqa: BLE001
            body = f"Error launching `gh pr diff`: {e}"
        else:
            if r.returncode == 0:
                body = r.stdout or "(empty diff)"
            else:
                err = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
                body = f"Error (exit {r.returncode}):\n{err}"
        self.app.call_from_thread(
            self.query_one("#diff-body", Static).update,
            body,
        )


# --- Confirm modal ---------------------------------------------------------


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of a confirmed ConfirmScreen — distinct from cancel (None).

    `extra_prompt` is normalised on construction: leading/trailing whitespace
    is stripped so a whitespace-only value can never reach the prompt-builder
    and inject an empty `Additional instructions from reviewer:` section.
    """

    post_inline: bool
    extra_prompt: str = ""

    def __post_init__(self) -> None:
        # `frozen=True` blocks attribute assignment; bypass via __setattr__
        # to enforce the strip invariant on the type itself rather than at
        # each call site.
        object.__setattr__(self, "extra_prompt", self.extra_prompt.strip())


class ExtraPromptTextArea(TextArea):
    """TextArea variant where Shift+Enter inserts a newline.

    TextArea consumes plain Enter to insert a newline internally, so the
    surrounding screen must use a `priority=True` binding to win and
    route Enter to confirm. The Shift+Enter handling here is added via
    the public BINDINGS extension point rather than overriding the
    private `_on_key` hook — that way an upstream rename or signature
    change can't silently turn Shift+Enter into a confirm-and-submit
    (which would happen if the override became dead code while the
    screen's priority Enter binding kept firing).

    Note: terminals without modifyOtherKeys / kitty keyboard support
    can't distinguish Shift+Enter from Enter; on those, Shift+Enter
    behaves like Enter (confirms). Pasting multi-line text still works
    for multi-line input.
    """

    BINDINGS = [
        Binding("shift+enter", "insert_newline", priority=True, show=False),
    ]

    def action_insert_newline(self) -> None:
        self.insert("\n")


class ConfirmScreen(ModalScreen[ConfirmResult | None]):
    """Confirm Claude Code launch for a PR review, with a post-inline toggle
    and an optional free-form extra-prompt textbox.

    Dismisses with None on cancel, or a ConfirmResult on confirm. Keeping
    cancel and confirm in separate shapes prevents a future truthy check
    (`if result:`) from silently swallowing the post-inline-off case.
    """

    # Auto-focus the textbox so typing extra prompt is zero-keystroke. The
    # priority bindings below ensure Enter / Ctrl-modified shortcuts still
    # fire from inside the focused TextArea.
    AUTO_FOCUS = "#confirm-extra"

    BINDINGS = [
        # `priority=True` is mandatory on every binding here: TextArea has
        # focus by default, and without priority its `_on_key` would consume
        # Enter (insert "\n") and the Ctrl-prefixed letters before our
        # actions ever ran.
        #
        # Toggle is on `ctrl+t` (not `ctrl+p`) because Textual's command
        # palette is a `priority=True` App-level binding on `ctrl+p` and
        # would otherwise win.
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("ctrl+y", "confirm", "Confirm", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+n", "cancel", "Cancel", priority=True, show=False),
        Binding("ctrl+t", "toggle_post_inline", "Toggle post-inline", priority=True),
    ]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt
        self.post_inline = True

    def compose(self) -> ComposeResult:
        hint = (
            "[b]Enter[/] / [b]Ctrl+Y[/] confirm • [b]Esc[/] / [b]Ctrl+N[/] cancel "
            "• [b]Ctrl+T[/] toggle post-inline • [b]Shift+Enter[/] newline"
        )
        yield Vertical(
            Label(self.prompt, id="confirm-title", markup=False),
            Label(self._checkbox_text(), id="confirm-checkbox", markup=False),
            Label("Extra prompt (optional):", id="confirm-extra-label"),
            ExtraPromptTextArea(id="confirm-extra"),
            Label(hint, id="confirm-hint"),
            id="confirm-container",
        )

    def _checkbox_text(self) -> str:
        mark = "[x]" if self.post_inline else "[ ]"
        return f"{mark} Post findings as inline PR comments"

    def action_confirm(self) -> None:
        text = self.query_one("#confirm-extra", TextArea).text
        self.dismiss(ConfirmResult(post_inline=self.post_inline, extra_prompt=text))

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


class _HeaderLink(Link):
    # Suppress Link's default `enter → Open link` footer entry; clicking still
    # works, and we don't want it crowding the bindings row.
    BINDINGS = [Binding("enter", "open_link", "Open link", show=False)]


class HeaderWithChangelog(Header):
    # `margin-right: 10` reserves the clock's column width — without it the
    # link and the (also dock: right) clock pile on the same edge and overlap.
    DEFAULT_CSS = """
    HeaderWithChangelog #changelog-link {
        dock: right;
        width: auto;
        padding: 0 1;
        margin-right: 10;
        content-align: center middle;
        background: transparent;
        text-style: none;
        pointer: pointer;
    }
    HeaderWithChangelog #changelog-link:hover {
        pointer: pointer;
    }
    HeaderWithChangelog #changelog-link:focus {
        background: transparent;
        text-style: bold;
        pointer: pointer;
    }
    """

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _HeaderLink("📝 Release Notes", url=CHANGELOG_URL, id="changelog-link")
        yield (
            HeaderClock().data_bind(Header.time_format) if self._show_clock else HeaderClockSpace()
        )


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
    #diff-scroll {
        height: 1fr;
        padding: 1;
    }
    #diff-body {
        height: auto;
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
    #confirm-extra-label {
        margin-top: 1;
        color: $text-muted;
    }
    #confirm-extra {
        height: 5;
        margin-bottom: 1;
    }
    #filter-list {
        height: auto;
        max-height: 20;
        margin: 1 0;
    }
    #filter-hint {
        margin-top: 1;
    }
    FooterKey.-state-active .footer-key--key {
        background: $success;
        color: $text;
    }
    FooterKey.-state-active .footer-key--description {
        background: $success;
        color: $text;
        text-style: bold;
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
        Binding("g", "toggle_group", "Group by"),
        Binding("s", "toggle_sort", "Sort by"),
        Binding("u", "upgrade", "Upgrade"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[dict[str, Any]] = []
        # Immutable: rebound wholesale, never mutated in place (writers must
        # swap a fresh tuple to stay safe across the FilterScreen worker).
        self.repo_cache: tuple[str, ...] = ()
        self.latest_version: str | None = None
        self.review_db: sqlite3.Connection = _open_review_db()
        self.review_state: dict[str, dict[str, Any]] = _load_review_state(self.review_db)
        stored_filter = _get_setting(self.review_db, "repo_filter", "")
        self.repo_filter: str | None = stored_filter or None
        self.include_mine: bool = _get_setting(self.review_db, "include_mine", "0") == "1"
        stored_group = _get_setting(self.review_db, "group_by", "")
        self.group_by: GroupBy = stored_group if stored_group in _GROUP_CYCLE else ""
        stored_sort = _get_setting(self.review_db, "sort_by", "")
        self.sort_by: SortBy = stored_sort if stored_sort in _SORT_CYCLE else ""
        self._row_to_pr_idx: list[int | None] = []
        # Last mine-fetch error from `_load_prs`. Forwarded into pure
        # render-toggle calls of `_populate` so a previously-shown ERROR
        # badge isn't silently dropped when the user presses `g`.
        self._last_mine_error: str | None = None

    def compose(self) -> ComposeResult:
        yield HeaderWithChangelog(show_clock=True)
        yield Static("", id="version-badge")
        yield PRDataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Static("Loading…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.add_columns(
            "Repository", "#", "Title", "Author", "Updated", "Reviews", "Last Review", ""
        )
        # The Footer recomposes whenever the screen's active bindings change
        # (e.g. on modal push/pop), which wipes any per-FooterKey class we
        # set. Subscribing here re-applies our `-state-active` tags *after*
        # each such recompose so the highlights survive modal interactions.
        #
        # `call_after_refresh` is chained twice: Footer also schedules its
        # recompose via `call_after_refresh` from the same signal, so a
        # single defer would land BEFORE Footer remounts its FooterKeys
        # (causing our class to be set on doomed widgets and lost). The
        # double defer pushes us past Footer's mount cycle.
        self.screen.bindings_updated_signal.subscribe(
            self,
            lambda _screen: self.call_after_refresh(
                lambda: self.call_after_refresh(self._refresh_footer_indicators)
            ),
        )
        self._refresh_footer_indicators()
        self.action_refresh()
        self._check_for_update()

    def _refresh_footer_indicators(self) -> None:
        """Re-apply the `-state-active` class on every state-bearing key.

        Centralised so that both bindings-signal callbacks and post-action
        calls (toggle mine, apply filter) end up at the same place — adding
        a new state-bearing key only takes a single line here.
        """
        self._set_footer_active("m", self.include_mine)
        self._set_footer_active("f", self.repo_filter is not None)
        self._set_footer_active("g", bool(self.group_by))
        self._set_footer_active("s", bool(self.sort_by))

    def _set_footer_active(self, key: str, active: bool, retries: int = 2) -> None:
        """Toggle the `-state-active` CSS class on the FooterKey for `key`.

        Footer mounts its `FooterKey` children only after it processes the
        `bindings_updated_signal`. If our App-level subscriber runs before
        Footer's, the query is empty on first try — so we re-schedule via
        `call_after_refresh` until the FooterKey appears (bounded retries
        keep this from spinning if Footer is hidden / never mounted).
        """
        for fk in self.query(FooterKey):
            if fk.key == key:
                fk.set_class(active, "-state-active")
                return
        if retries > 0:
            self.call_after_refresh(self._set_footer_active, key, active, retries - 1)

    # --- actions ---

    def action_refresh(self) -> None:
        self._set_status("Refreshing…")
        self._load_prs()

    @work(thread=True, exclusive=True)
    def _load_prs(self) -> None:
        repo = self.repo_filter
        try:
            data = fetch_review_prs(repo)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"Error fetching review PRs: {e}", True)
            return

        # Fetch my-PRs separately so a failure here doesn't drop the
        # review-PR list, and so the user sees an explicit error rather
        # than an empty MINE column when the my-PRs fetch fails.
        mine_error: str | None = None
        mine_warning: str | None = None
        if self.include_mine:
            try:
                mine, mine_warning = fetch_my_prs(repo)
            except Exception as e:  # noqa: BLE001
                mine_error = str(e)
                mine = []
            seen = {(p["repository"]["nameWithOwner"], p["number"]) for p in data}
            for pr in mine:
                key = (pr["repository"]["nameWithOwner"], pr["number"])
                if key in seen:
                    continue
                pr["_mine"] = True
                data.append(pr)
        self.call_from_thread(self._populate, data, mine_error, mine_warning)

    def _filter_desc(self) -> str:
        return f" [repo={self.repo_filter}]" if self.repo_filter else ""

    def _group_desc(self) -> str:
        return f" [group={self.group_by}]" if self.group_by else ""

    def _sort_desc(self) -> str:
        return f" [sort={self.sort_by}]" if self.sort_by else ""

    def _populate(
        self,
        data: list[dict[str, Any]],
        mine_error: str | None = None,
        mine_warning: str | None = None,
        quiet: bool = False,
    ) -> None:
        self.prs = data
        # Sticky so pure render-toggles (e.g. `action_toggle_group` →
        # `_populate(self.prs, mine_error=self._last_mine_error, quiet=True)`)
        # can preserve the ERROR badge a previous fetch produced.
        self._last_mine_error = mine_error
        # Always reset before any early-return so an empty-data populate
        # leaves the row map consistent with the rendered table — `_selected`
        # already short-circuits on `row_count == 0`, but a stale list here
        # is a latent trap for future call sites.
        self._row_to_pr_idx = []
        # Refresh repo cache only on unfiltered fetches; otherwise it would
        # shrink to whatever the active filter happens to allow. An empty
        # result is a legitimate "no repos" signal, so don't gate on `data`.
        if self.repo_filter is None:
            self.repo_cache = tuple(sorted({pr["repository"]["nameWithOwner"] for pr in data}))
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        if mine_warning and not quiet:
            self.notify(mine_warning, severity="warning", timeout=8)
        # Surface mine-count (or the my-PRs fetch error) so the user can tell
        # at a glance whether the toggle pulled in any of their own PRs —
        # otherwise an empty MINE column is indistinguishable from a silent
        # my-PRs fetch failure.
        if mine_error:
            # Guard against an empty/whitespace-only error string (e.g. a
            # bare `RuntimeError()` stringifies to "") — `"".splitlines()`
            # is `[]`, and `[0]` would IndexError on the UI thread.
            first_line = (mine_error.splitlines() or [""])[0][:80] or "unknown error"
            mode = f" (+mine: ERROR — {first_line})"
            if not quiet:
                self.notify(
                    f"Couldn't fetch your authored PRs: {mine_error}",
                    severity="error",
                    timeout=10,
                )
        elif self.include_mine:
            mine_count = sum(1 for p in data if p.get("_mine"))
            mode = f" (+mine: {mine_count})"
        else:
            mode = " (mine: off)"
        filter_desc = self._filter_desc()
        group_desc = self._group_desc()
        sort_desc = self._sort_desc()
        if not data:
            self._set_status(
                f"No PRs awaiting your review 🎉{mode}{filter_desc}{group_desc}{sort_desc}   "
                "(f: filter, m: mine, g: group, s: sort, r: refresh, u: upgrade, q: quit)",
                error=bool(mine_error),
            )
            return

        def _emit_pr_row(i: int, pr: dict[str, Any]) -> None:
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
            table.add_row(
                repo,
                f"#{num}",
                title,
                author,
                updated,
                _review_cell(pr, self.review_state),
                _last_reviewed_cell(pr, self.review_state),
                " ".join(tags),
                key=str(i),
            )
            self._row_to_pr_idx.append(i)

        if not self.group_by:
            # Reorder at render time only — `self.prs` stays in fetch order
            # so toggling sort off restores the natural data-source ordering
            # without needing a refresh.
            if self.sort_by == "updated":
                indices = sorted(
                    range(len(data)),
                    key=lambda i: data[i].get("updatedAt", ""),
                    reverse=True,
                )
            else:
                indices = list(range(len(data)))
            for i in indices:
                _emit_pr_row(i, data[i])
        else:

            def _key(pr: dict[str, Any]) -> str:
                if self.group_by == "repo":
                    return pr["repository"]["nameWithOwner"]
                return (pr.get("author") or {}).get("login", "?")

            # `updatedAt` drives both within-group and across-group ordering
            # below; the silent `""` default would bucket schema-broken PRs
            # at the bottom of their group where they're easy to miss. Surface
            # it once per populate so a real upstream break is visible.
            if not quiet and any(not p.get("updatedAt") for p in data):
                self.notify(
                    "Some PRs are missing `updatedAt` — group ordering may be off.",
                    severity="warning",
                    timeout=6,
                )
            buckets: dict[str, list[int]] = {}
            for i, pr in enumerate(data):
                buckets.setdefault(_key(pr), []).append(i)
            for k in buckets:
                buckets[k].sort(key=lambda i: data[i].get("updatedAt", ""), reverse=True)
            # Sort groups by their most-recently-updated PR (desc) so active
            # repos/authors float to the top — alphabetical buries hot groups.
            group_order = sorted(
                buckets.keys(),
                key=lambda k: data[buckets[k][0]].get("updatedAt", ""),
                reverse=True,
            )
            for gk in group_order:
                idxs = buckets[gk]
                # `escape(gk)` — `gk` is a GitHub login or `nameWithOwner`,
                # both of which can contain `[` (notably `dependabot[bot]`),
                # which Rich would otherwise parse as a markup tag and crash.
                table.add_row(
                    f"[bold]▼ {escape(gk)}[/]  [dim]({len(idxs)})[/]",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    key=f"hdr:{gk}",
                )
                self._row_to_pr_idx.append(None)
                for i in idxs:
                    _emit_pr_row(i, data[i])
        self._set_status(
            f"{len(data)} PR(s){mode}{filter_desc}{group_desc}{sort_desc}   "
            "•  enter: review  •  d: diff  •  o: browser  •  f: filter  •  m: mine  "
            "•  g: group  •  s: sort  •  r: refresh  •  u: upgrade  •  q: quit",
            error=bool(mine_error),
        )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#pr-table", DataTable)
        if table.row_count == 0:
            return None
        row = table.cursor_row
        if row is None or row >= len(self._row_to_pr_idx):
            return None
        idx = self._row_to_pr_idx[row]
        if idx is None:
            # Cursor is on a group-header row — distinguish from an empty
            # table so the user gets feedback rather than thinking Enter
            # is broken.
            self.notify("Group header — select a PR row", severity="information", timeout=2)
            return None
        return self.prs[idx]

    def action_open_web(self) -> None:
        pr = self._selected()
        if pr:
            webbrowser.open(pr["url"])

    def action_upgrade(self) -> None:
        if self.latest_version is None:
            self._set_status("No update available (or check is still in progress).")
            return
        if shutil.which("uv") is None:
            self._set_status(
                f"`uv` not on PATH — install uv (https://docs.astral.sh/uv/) "
                f"then run: uv tool upgrade {PACKAGE_NAME}",
                error=True,
            )
            return
        cmd = ["uv", "tool", "upgrade", PACKAGE_NAME]
        rc = 1
        with self.suspend():
            print(f"\n$ {' '.join(cmd)}\n")
            try:
                rc = subprocess.call(cmd)
            except OSError as e:
                print(f"\nFailed to launch `uv`: {e}")
            if rc == 0:
                print(f"\nUpgraded to v{self.latest_version}. Restart cc-pr-reviewer.")
            else:
                print(
                    f"\nUpgrade failed (exit {rc}). "
                    f"If you installed via pip/pipx, run: pip install -U {PACKAGE_NAME}. "
                    f"See {RELEASES_URL}"
                )
            with contextlib.suppress(EOFError):
                input("\nPress Enter to continue…")
        if rc == 0:
            self.exit()

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
                self._launch_claude(pr, result.post_inline, result.extra_prompt)

        self.push_screen(ConfirmScreen(prompt), _proceed)

    def action_toggle_mine(self) -> None:
        self.include_mine = not self.include_mine
        state = "on" if self.include_mine else "off"
        # Persist so the toggle sticks across sessions. Mirrors `repo_filter`:
        # warn but keep the in-session toggle flipped if the write fails, so
        # the user's current view still reflects what they pressed.
        try:
            _set_setting(self.review_db, "include_mine", "1" if self.include_mine else "0")
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist mine toggle: {e}", severity="warning")
        self.notify(f"My PRs: {state}", timeout=3)
        self._set_status(f"Refreshing… (mine {state})")
        self._refresh_footer_indicators()
        self._load_prs()

    def action_toggle_group(self) -> None:
        # Capture the cursor's PR identity before re-populate clears the
        # table — without this, toggling on always parks the cursor on the
        # first group header (a no-op row), which combined with `_selected`
        # returning None for headers makes Enter/d/o appear broken until
        # the user manually moves down. Inlined (rather than via
        # `_selected()`) so the read doesn't fire `_selected`'s
        # header-row notify.
        table = self.query_one("#pr-table", DataTable)
        prev_key: tuple[str, int] | None = None
        if table.row_count and 0 <= (table.cursor_row or 0) < len(self._row_to_pr_idx):
            idx = self._row_to_pr_idx[table.cursor_row]
            if idx is not None:
                p = self.prs[idx]
                prev_key = (p["repository"]["nameWithOwner"], p["number"])

        nxt = _GROUP_CYCLE[self.group_by]
        self.group_by = nxt
        try:
            _set_setting(self.review_db, "group_by", nxt)
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist group toggle: {e}", severity="warning")
        self.notify(f"Group: {nxt or 'off'}", timeout=3)
        self._refresh_footer_indicators()
        # Render-only re-populate: forward the last fetch's mine_error so
        # the ERROR badge isn't lost, and `quiet=True` to suppress
        # re-toasting toasts the user already saw on the original fetch.
        self._populate(self.prs, mine_error=self._last_mine_error, quiet=True)

        if prev_key is not None:
            for row, idx in enumerate(self._row_to_pr_idx):
                if idx is None:
                    continue
                p = self.prs[idx]
                if (p["repository"]["nameWithOwner"], p["number"]) == prev_key:
                    table.move_cursor(row=row)
                    break

    def action_toggle_sort(self) -> None:
        # Mirrors `action_toggle_group`: capture cursor PR identity, cycle the
        # mode, persist, render-only re-populate, then restore the cursor onto
        # the same PR at its new row.
        table = self.query_one("#pr-table", DataTable)
        prev_key: tuple[str, int] | None = None
        if table.row_count and 0 <= (table.cursor_row or 0) < len(self._row_to_pr_idx):
            idx = self._row_to_pr_idx[table.cursor_row]
            if idx is not None:
                p = self.prs[idx]
                prev_key = (p["repository"]["nameWithOwner"], p["number"])

        nxt = _SORT_CYCLE[self.sort_by]
        self.sort_by = nxt
        try:
            _set_setting(self.review_db, "sort_by", nxt)
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist sort toggle: {e}", severity="warning")
        self.notify(f"Sort: {nxt or 'default'}", timeout=3)
        self._refresh_footer_indicators()
        self._populate(self.prs, mine_error=self._last_mine_error, quiet=True)

        if prev_key is not None:
            for row, idx in enumerate(self._row_to_pr_idx):
                if idx is None:
                    continue
                p = self.prs[idx]
                if (p["repository"]["nameWithOwner"], p["number"]) == prev_key:
                    table.move_cursor(row=row)
                    break

    def action_filter(self) -> None:
        def _apply(result: FilterChoice | None) -> None:
            if result is None or result.repo == self.repo_filter:
                return
            if self._set_repo_filter(result.repo):
                # The modal-pop's bindings_updated_signal fires before this
                # callback runs, so the highlight refresh from the signal
                # subscriber sees the OLD filter value. Refresh again here
                # so the `f` key reflects the just-applied filter.
                self._refresh_footer_indicators()
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

    def _launch_claude(self, pr: dict[str, Any], post_inline: bool, extra_prompt: str) -> None:
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

            # Compute rereview against the raw `existing` list, not against
            # `existing_block` — `format_existing_comments` filters out entries
            # missing path/created_at/body, and we still want to raise the bar
            # if the only surviving evidence is in the unfiltered list.
            #
            # The APPROVE clause is gated separately on authorship: GitHub
            # rejects self-approval with 422, so we still raise the bar on
            # self re-reviews but drop the auto-approve instruction.
            my_login = _current_gh_login()
            author_login = (pr.get("author") or {}).get("login")
            rereview = bool(my_login) and any(
                (c.get("user") or {}).get("login") == my_login for c in existing
            )
            rereview_can_approve = rereview and author_login != my_login

            sections = [REVIEW_PROMPT]
            if extra_prompt:
                sections.append(f"Additional instructions from reviewer:\n{extra_prompt}")
            if existing_block:
                sections.append(existing_block)
            if post_inline:
                post = POST_INLINE_PROMPT
                if existing_block:
                    post += POST_INLINE_DEDUP_SUFFIX
                elif not fetch_ok:
                    post += POST_INLINE_FETCH_FAILED_SUFFIX
                if rereview:
                    post += POST_INLINE_REREVIEW_SUFFIX
                    if rereview_can_approve:
                        post += POST_INLINE_REREVIEW_APPROVE_SUFFIX
                sections.append(post)
            prompt = PROMPT_SECTION_SEP.join(sections)

            if not fetch_ok:
                existing_desc = "existing comments: fetch failed"
            else:
                existing_desc = f"existing comments: {shown} in prompt of {len(existing)} fetched"

            cmd = ["claude", "--permission-mode", "acceptEdits", prompt]
            post_inline_desc = "on" if post_inline else "off"
            if post_inline and rereview:
                post_inline_desc += ", rereview"
            parts = [f"post-inline: {post_inline_desc}", existing_desc]
            if extra_prompt:
                # `!r` keeps newlines/control chars visible so a misclick paste
                # (e.g. a secret) is spottable before claude consumes it. The
                # explicit `(+N more chars)` suffix is the load-bearing piece:
                # without it, a 201-char paste renders identically to a clean
                # 200-char one while the full text still flows into claude's
                # argv, defeating the whole point of the preview.
                shown = extra_prompt[:EXTRA_PROMPT_BANNER_CAP]
                hidden = len(extra_prompt) - len(shown)
                suffix = f" (+{hidden} more chars)" if hidden else ""
                parts.append(f"extra prompt: {shown!r}{suffix}")
            print(f"\nLaunching Claude Code ({', '.join(parts)}) — type /exit when you're done.\n")
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
        w.update(f" ▲ v{latest} available — uv tool upgrade {PACKAGE_NAME} (press u) ")
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
