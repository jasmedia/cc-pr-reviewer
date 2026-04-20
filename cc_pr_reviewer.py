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
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, Static

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
    " Additionally, publish each finding as an inline PR review comment on "
    "GitHub using the `gh` CLI. Create a single pending review via `gh api "
    "--method POST /repos/{owner}/{repo}/pulls/{number}/reviews` with an array "
    "of `comments` entries (each with `path`, `line`, and `body`), then submit "
    "the review with `event: COMMENT` so all findings appear grouped. Use the "
    "PR's head commit SHA when the endpoint requires `commit_id`."
)


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
            "--limit=100",
            "--json",
            PR_FIELDS,
            *extra,
        ]
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "gh search prs failed")
    return json.loads(r.stdout or "[]")


def fetch_review_prs() -> list[dict[str, Any]]:
    """All open PRs across GitHub where @me is a requested reviewer."""
    return _search_prs(["--review-requested=@me"])


def fetch_my_prs() -> list[dict[str, Any]]:
    """All open PRs across GitHub authored by @me."""
    return _search_prs(["--author=@me"])


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
    conn.commit()
    return conn


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


# --- Main app --------------------------------------------------------------


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
    """

    TITLE = "GitHub PR Reviewer"
    SUB_TITLE = "Review PRs with Claude Code"

    BINDINGS = [
        Binding("r,f5", "refresh", "Refresh"),
        Binding("enter,c", "review", "Review w/ Claude"),
        Binding("o", "open_web", "Open in browser"),
        Binding("d", "show_diff", "View diff"),
        Binding("m", "toggle_mine", "Toggle my PRs"),
        Binding("a", "toggle_auto_accept", "Toggle auto-accept"),
        Binding("p", "toggle_post_inline", "Toggle post inline"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[dict[str, Any]] = []
        self.include_mine: bool = False
        self.auto_accept: bool = True
        self.post_inline: bool = True
        self.review_db: sqlite3.Connection = _open_review_db()
        self.review_state: dict[str, dict[str, Any]] = _load_review_state(self.review_db)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Static("Loading…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.add_columns(
            "Repository", "#", "Title", "Author", "Updated", "Reviews", "Last Review", ""
        )
        self.action_refresh()

    # --- actions ---

    def action_refresh(self) -> None:
        self._set_status("Refreshing…")
        self._load_prs()

    @work(thread=True, exclusive=True)
    def _load_prs(self) -> None:
        try:
            data = fetch_review_prs()
            if self.include_mine:
                seen = {(p["repository"]["nameWithOwner"], p["number"]) for p in data}
                for pr in fetch_my_prs():
                    key = (pr["repository"]["nameWithOwner"], pr["number"])
                    if key in seen:
                        continue
                    pr["_mine"] = True
                    data.append(pr)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"Error: {e}", True)
            return
        self.call_from_thread(self._populate, data)

    def _populate(self, data: list[dict[str, Any]]) -> None:
        self.prs = data
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        mode = " (+mine)" if self.include_mine else ""
        auto = "on" if self.auto_accept else "off"
        post = "on" if self.post_inline else "off"
        if not data:
            self._set_status(
                f"No PRs awaiting your review 🎉{mode}   "
                f"auto-accept: {auto}   post-inline: {post}   "
                "(m: mine, a: auto-accept, p: post-inline, r: refresh, q: quit)"
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
            f"{len(data)} PR(s){mode}   auto-accept: {auto}   post-inline: {post}   "
            "•  enter: review  •  d: diff  •  o: browser  •  m: mine  "
            "•  a: auto-accept  •  p: post-inline  •  r: refresh  •  q: quit"
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

    def action_show_diff(self) -> None:
        pr = self._selected()
        if pr:
            self.push_screen(DiffScreen(pr["repository"]["nameWithOwner"], pr["number"]))

    def action_review(self) -> None:
        pr = self._selected()
        if pr:
            self._launch_claude(pr)

    def action_toggle_mine(self) -> None:
        self.include_mine = not self.include_mine
        self.action_refresh()

    def action_toggle_auto_accept(self) -> None:
        self.auto_accept = not self.auto_accept
        self._populate(self.prs)

    def action_toggle_post_inline(self) -> None:
        self.post_inline = not self.post_inline
        self._populate(self.prs)

    # --- launching claude ---

    def _launch_claude(self, pr: dict[str, Any]) -> None:
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
            head_sha = sha_r.stdout.strip() if sha_r.returncode == 0 else ""

            prompt = REVIEW_PROMPT + (POST_INLINE_PROMPT if self.post_inline else "")
            cmd = ["claude"]
            if self.auto_accept:
                cmd += ["--permission-mode", "acceptEdits"]
            cmd.append(prompt)
            print(
                f"\nLaunching Claude Code "
                f"(auto-accept: {'on' if self.auto_accept else 'off'}, "
                f"post-inline: {'on' if self.post_inline else 'off'}) "
                "— type /exit when you're done.\n"
            )
            subprocess.call(cmd, cwd=local_path)

            key = _pr_key(pr)
            self.review_state[key] = _record_review(
                self.review_db,
                key,
                pr.get("updatedAt", ""),
                head_sha,
            )

            input("\n── Claude session ended. Press Enter to return to the TUI ──")

        # Refresh in case review state changed (e.g. you approved the PR).
        self.action_refresh()

    # --- helpers ---

    def _set_status(self, msg: str, error: bool = False) -> None:
        w = self.query_one("#status", Static)
        w.update(msg)
        w.set_class(error, "-error")


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
