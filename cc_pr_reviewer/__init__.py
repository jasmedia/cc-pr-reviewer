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
import socket
import sqlite3
import subprocess
import sys
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
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
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
from textual.widgets.data_table import CellDoesNotExist
from textual.widgets.option_list import Option

# --- Configuration ---------------------------------------------------------

WORKSPACE = Path(os.environ.get("GH_PR_WORKSPACE", Path.home() / "gh-pr-workspace"))
REVIEW_DB_PATH = WORKSPACE / ".review_state.db"

# Capture hostname once at import time so reserve and release agree even
# if the box is renamed mid-session (DHCP, VPN connect, `hostnamectl`,
# `scutil --set ComputerName` on macOS). Without this, a release after
# a hostname change would find 0 rows on the WHERE-by-identity guard,
# leak the row permanently, and leave it unreapable by the same-host
# stale sweep (which compares `hostname == socket.gethostname()` afresh).
_APP_HOSTNAME = socket.gethostname()

# The prompt we hand Claude Code when it starts up in the PR's working tree.
# The PR Review Toolkit plugin will pick up on these cues and route to the
# right sub-agents.
REVIEW_PROMPT_CLAUDE = (
    "Please perform a comprehensive review of the current PR using the "
    "PR Review Toolkit. Run the relevant agents — Comment Analyzer, PR Test "
    "Analyzer, Silent Failure Hunter, Type Design Analyzer, Code Reviewer, "
    "and Code Simplifier — and give me a prioritised summary of findings "
    "with file:line references and suggested fixes."
)

# Codex and Gemini don't have a plugin marketplace, so the six toolkit
# agents ship as bundled Markdown files (see `cc_pr_reviewer/pr_review_agents/`).
# The order here is the order Codex/Gemini will be asked to apply them —
# code-reviewer first sets project-guideline context, then the targeted
# checks (silent failures, type design, tests), then the cleanup-oriented
# passes (comments, simplification). Changing the order is a behaviour
# change, so the tuple is the single source of truth consulted by both the
# prompt builder and the prereq check.
REVIEW_AGENT_FILES: tuple[str, ...] = (
    "code-reviewer.md",
    "silent-failure-hunter.md",
    "type-design-analyzer.md",
    "pr-test-analyzer.md",
    "comment-analyzer.md",
    "code-simplifier.md",
)


def _review_agents_dir() -> Path:
    """Return the bundled directory holding the file-based agent prompts.

    Resolved against `__file__` so editable installs (`uv sync`) and wheel
    installs (`pip install`) both work — hatchling packages the directory
    next to `__init__.py` in both cases. `importlib.resources` is avoided
    because the agents are read by an external subprocess (`codex` /
    `gemini`), not by Python — so we always need a filesystem path, not
    a `Traversable`.
    """
    return Path(__file__).parent / "pr_review_agents"


def _build_file_based_prompt() -> str:
    """Compose the review prompt for the file-based CLIs (codex, gemini).

    The agent files live in this repo (not in any Claude plugin), so the
    prompt simply lists their absolute paths and asks the CLI to read each
    one in turn. Identical for codex and gemini — they share the prompt and
    differ only in how they're launched.
    """
    agents_dir = _review_agents_dir()
    bullets = "\n".join(f"  • {agents_dir / name}" for name in REVIEW_AGENT_FILES)
    return (
        "Please perform a comprehensive review of the current PR by applying "
        "the six review criteria documented in these files (read each before "
        "reviewing):\n\n"
        f"{bullets}\n\n"
        "Then give me a prioritised summary of findings with file:line "
        "references and suggested fixes."
    )


REVIEW_PROMPT_FILE_BASED = _build_file_based_prompt()


def _build_cli_command(cli: CliChoice, prompt_text: str) -> list[str]:
    """Build the subprocess argv for handing control to the selected CLI.

    Flag picks mirror Claude's `--permission-mode acceptEdits` posture as
    closely as each CLI permits — no approval prompts, edits allowed
    inside the cloned PR workspace, but no broader host access:

    * `claude --permission-mode acceptEdits` (unchanged).
    * `codex --ask-for-approval never --sandbox workspace-write`. Picking
      `never` + `workspace-write` keeps the sandbox guard while skipping
      approval prompts. `--yolo` / `--dangerously-bypass-approvals-and-sandbox`
      is rejected because it also removes the sandbox — a behaviour shift
      vs the existing Claude flow.
    * `gemini --approval-mode auto_edit`. The `auto_edit` value is the
      documented analogue of Claude's `acceptEdits` (auto-approve edits,
      still gate risky operations). The older `--yolo` / `-y` flag is
      deprecated upstream in favour of `--approval-mode=yolo`.

    Only this function knows the flag surface — every other launch-path
    code path is CLI-agnostic, so flag-name churn in either upstream CLI
    is a one-function change.
    """
    if cli == "claude":
        return ["claude", "--permission-mode", "acceptEdits", prompt_text]
    if cli == "codex":
        return [
            "codex",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            prompt_text,
        ]
    if cli == "gemini":
        return ["gemini", "--approval-mode", "auto_edit", prompt_text]
    # Defensive: CliChoice is a Literal of exactly those three, so this
    # is reachable only if someone widens the type without updating this
    # switch. Make that failure loud rather than silently emitting a
    # mystery argv.
    raise ValueError(f"unknown CLI choice: {cli!r}")


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

# Appended after POST_INLINE_REREVIEW_APPROVE_SUFFIX (so it inherits the same
# gate: rereview AND the PR is NOT authored by the `gh` user). When we
# auto-approve, our own prior review threads on GitHub are still open —
# leaving them that way next to an APPROVE looks contradictory. This tells
# Claude to resolve the threads it considers addressed before submitting the
# APPROVE. Scoped to threads the `gh` user originally opened (first/root
# comment author == `gh` user); a "latest comment author" check would skip
# the common case where the PR author replied "fixed" or pushed a fix
# without replying. The fetched existing-comments list is capped/filtered
# (so not authoritative), so the prompt directs Claude to query
# `pullRequestReviewThreads` as the source of truth.
POST_INLINE_REREVIEW_RESOLVE_SUFFIX = (
    " If you do submit that APPROVE, first resolve any of your own "
    "previously-posted review threads that the current PR code has addressed. "
    "Get the current `gh` user via `gh api user --jq .login`; if that fails "
    "or returns empty, skip the resolve step entirely (note it in your final "
    "summary) and proceed to submit the APPROVE — do not guess the login. "
    "Otherwise, fetch the authoritative thread list via `gh api graphql` "
    "using `pullRequestReviewThreads(first: 100)` on the pull request, "
    "selecting `id`, `isResolved`, the first comment's author login, and "
    "`pageInfo { hasNextPage endCursor }`; page through with "
    "`after: <endCursor>` until `hasNextPage` is false. If the query or any "
    "subsequent page errors out (top-level `errors`, non-zero exit, or no "
    "`nodes`), skip the resolve step entirely (note the failure in your "
    "final summary) and proceed to submit the APPROVE — do not fall back "
    "to the inline existing-comments list, which is capped/filtered and "
    "lacks `id`/`isResolved`. In-scope threads are those whose first (root) "
    "comment author matches the current `gh` user and that are not already "
    "resolved; skip every other thread (don't touch other reviewers' "
    "threads). For each in-scope thread you judge addressed by the current "
    "code, call the `resolveReviewThread` mutation via `gh api graphql`, "
    "selecting `thread { isResolved }` in the response. Treat the mutation "
    "as successful only if the response has no top-level `errors` field "
    "AND `data.resolveReviewThread.thread.isResolved == true`; anything "
    "else (HTTP-200 with `errors`, `isResolved` still false, network blip) "
    "is a failure — record the GraphQL error verbatim and continue with "
    "the remaining candidates. Do not stall the APPROVE on a single "
    "mutation error. Zero resolutions is a valid outcome; the gate is "
    "finishing the candidate walk, not landing any specific number of "
    "resolutions. In your final terminal summary, list the threads you "
    "resolved, the ones you judged not-yet-addressed (one-line reason), "
    "and any that failed to resolve (with the GraphQL error)."
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

# Which coding-agent CLI to hand the review off to. Single source of truth
# for the cycle order (footer-key `c` and the modal Ctrl+L override both
# consult `_CLI_CYCLE`), so adding a fourth CLI later is one dict edit.
# Default is "claude" for back-compat with installs that predate the toggle.
CliChoice = Literal["claude", "codex", "gemini"]
_CLI_CYCLE: dict[CliChoice, CliChoice] = {
    "claude": "codex",
    "codex": "gemini",
    "gemini": "claude",
}
DEFAULT_CLI: CliChoice = "claude"

# Human-facing display names for banners, modal labels, and toast messages.
_CLI_DISPLAY: dict[CliChoice, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini",
}

# PyPI update-check lifecycle. "unavailable" is for source/editable installs
# where `_installed_version()` returns None and there's nothing to compare;
# the worker doesn't run and `action_upgrade` shows a tailored message rather
# than a misleading "check failed" error.
UpdateCheckState = Literal["pending", "current", "available", "failed", "unavailable"]


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


def _persisted_cli() -> CliChoice:
    """Read the persisted CLI choice without instantiating the full app.

    `check_prereqs` runs before `PRReviewer.__init__`, so it can't reach
    through `self.review_db`. Returns `DEFAULT_CLI` on any failure
    (missing DB, corrupt row, unrecognised value) so a transient DB
    issue can't fail the startup gate — the launcher's pre-flight
    check surfaces a missing binary at review time.
    """
    try:
        conn = _open_review_db()
    except sqlite3.Error:
        return DEFAULT_CLI
    try:
        v = _get_setting(conn, "cli", DEFAULT_CLI)
    finally:
        conn.close()
    return v if v in _CLI_CYCLE else DEFAULT_CLI


def _first_available_cli(preferred: CliChoice) -> CliChoice | None:
    """Walk the CLI cycle starting at `preferred`; return the first on PATH.

    Used at startup to pick a session-level fallback when the persisted
    CLI isn't installed on this machine — a Codex-only user who sees
    `claude` as the persisted default (because that's `DEFAULT_CLI`)
    should still be able to launch the TUI. The cycle order (claude →
    codex → gemini → claude) keeps the fallback predictable: the next
    CLI you'd land on if you pressed `c` once.

    Returns `None` if none of the three supported CLIs is installed —
    in which case the app genuinely can't review anything and startup
    should fail.
    """
    candidate = preferred
    for _ in range(len(_CLI_CYCLE)):
        if shutil.which(candidate) is not None:
            return candidate
        candidate = _CLI_CYCLE[candidate]
    return None


def check_prereqs() -> list[str]:
    """Return a list of human-readable problems (empty if everything is ready).

    Gates startup on the absolute minimum — `gh` (authenticated), `git`,
    and **at least one** of the three supported review CLIs on PATH.
    The specific CLI the user picked may be unavailable on this machine,
    but as long as one CLI is installed the TUI can launch and the user
    can fall back to / toggle to the installed one. The launcher does a
    second pre-flight check at review time that surfaces the
    CLI-specific problem (missing binary, missing toolkit plugin)
    against whichever CLI is selected for that launch.

    The toolkit-plugin check is intentionally NOT here: it only matters
    when cli=claude, and a Codex/Gemini user shouldn't be blocked from
    starting the TUI because the Claude plugin isn't installed. The
    pre-flight check inside `_launch_claude` covers it.
    """
    problems: list[str] = []
    if shutil.which("gh") is None:
        problems.append("`gh` CLI not found on PATH — install from https://cli.github.com")
    elif run(["gh", "auth", "status"]).returncode != 0:
        problems.append("`gh` is not authenticated — run: gh auth login")

    if _first_available_cli(_persisted_cli()) is None:
        problems.append(
            "no supported review CLI on PATH — install at least one of:\n"
            "    • Claude Code: https://claude.com (and: claude plugin install "
            f"{PR_REVIEW_TOOLKIT_PLUGIN})\n"
            "    • OpenAI Codex CLI: https://github.com/openai/codex\n"
            "    • Google Gemini CLI: https://github.com/google-gemini/gemini-cli"
        )
    elif not _review_agents_dir().is_dir():
        # The bundled agent prompts are mandatory for codex/gemini and
        # harmless for claude. If they're missing the install is broken
        # regardless of which CLI the user ends up on, so this stays a
        # hard startup failure.
        problems.append(
            f"bundled review-agent prompts missing at {_review_agents_dir()} "
            "— reinstall cc-pr-reviewer"
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


@dataclass(frozen=True)
class BuiltPrompt:
    """Result of `build_review_prompt` — prompt text plus banner metadata."""

    text: str
    rereview: bool
    existing_shown: int
    existing_total: int


def build_review_prompt(
    *,
    post_inline: bool,
    extra_prompt: str,
    existing: list[dict[str, Any]],
    fetch_ok: bool,
    my_login: str | None,
    author_login: str | None,
    cli: CliChoice = DEFAULT_CLI,
) -> BuiltPrompt:
    """Assemble the user message for the selected coding-agent CLI. Pure — no I/O.

    Isolated from `_launch_claude` so the conditional `POST_INLINE_*`
    suffix matrix (locked by `tests/test_cc_pr_reviewer.py`) is
    unit-testable in isolation; that matrix is the most regression-prone
    part of the file.

    `cli` selects the base review prompt: `claude` uses the
    plugin-driven `REVIEW_PROMPT_CLAUDE`, while `codex` and `gemini`
    share `REVIEW_PROMPT_FILE_BASED` (which references the bundled
    agent Markdown files). All `POST_INLINE_*` suffixes apply identically
    to every CLI — they're about `gh` CLI usage, not the reviewing
    agent. `cli` defaults to `"claude"` so existing callers and tests
    remain valid without churn.

    Contract: `fetch_existing_review_comments` guarantees that
    `fetch_ok=False` always returns `existing=[]`. Passing a non-empty
    `existing` with `fetch_ok=False` is a contradictory state — caught
    here at the API seam because, post-extraction, the two flags are
    independent kwargs and the contradiction is easy to construct
    accidentally (e.g. from a test stub).
    """
    if not fetch_ok and existing:
        raise AssertionError(
            "build_review_prompt: fetch_ok=False with non-empty existing is contradictory"
        )

    existing_block, shown = format_existing_comments(existing)

    # Compute against the raw `existing` list, not against `existing_block` —
    # `format_existing_comments` filters out entries missing path/created_at/
    # body, and we still want to raise the bar if the only surviving evidence
    # is in the unfiltered list.
    rereview = bool(my_login) and any(
        (c.get("user") or {}).get("login") == my_login for c in existing
    )
    # GitHub returns 422 ("Can not approve your own pull request") on
    # `event: APPROVE` for the author, so the auto-approve clause is gated
    # separately on authorship — we still raise the bar on self re-reviews
    # but drop the auto-approve instruction.
    rereview_can_approve = rereview and author_login != my_login

    base = REVIEW_PROMPT_CLAUDE if cli == "claude" else REVIEW_PROMPT_FILE_BASED
    sections = [base]
    # Strip defensively — the current ConfirmResult dataclass already strips,
    # but `build_review_prompt` is now an API boundary and a future caller
    # (or test) passing whitespace-only `extra_prompt` would otherwise render
    # an empty "Additional instructions from reviewer:" header followed by
    # nothing.
    stripped_extra = extra_prompt.strip()
    if stripped_extra:
        sections.append(f"Additional instructions from reviewer:\n{stripped_extra}")
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
                post += POST_INLINE_REREVIEW_RESOLVE_SUFFIX
        sections.append(post)

    return BuiltPrompt(
        text=PROMPT_SECTION_SEP.join(sections),
        rereview=rereview,
        existing_shown=shown,
        existing_total=len(existing),
    )


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
    # `timeout` covers Python-side waits; `busy_timeout` covers SQLite-side
    # waits on writes. Both matter now that multiple TUI instances can
    # contend on the in-progress table. WAL switches the DB file to a
    # multi-reader / single-writer mode so a tab's INSERT doesn't lock out
    # peers' SELECTs while it commits. The PRAGMAs are wrapped in
    # `suppress(OperationalError)` so a read-only mount surfaces as
    # degraded behaviour rather than a startup crash; the `CREATE TABLE`s
    # below stay load-bearing.
    # `check_same_thread=False` lets the periodic `_poll_in_progress`
    # worker (`@work(thread=True)`) read from this connection without
    # tripping Python's per-connection thread guard. Safe here because
    # WAL + `busy_timeout` serialise writers at the SQLite layer, and
    # Python's sqlite3 module already takes a per-connection mutex
    # around each `execute`/`commit` call. We never hold an open
    # transaction across thread boundaries — every helper here issues
    # its own commit.
    conn = sqlite3.connect(REVIEW_DB_PATH, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Two separate suppress blocks: WAL writes to disk (read-only mount
    # would raise), `busy_timeout` is a session-only hint that succeeds
    # on read-only filesystems too. A combined block would silently skip
    # busy_timeout if WAL fails — defeating the busy_timeout protection
    # for the very environments where contention is likeliest.
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA journal_mode=WAL")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA busy_timeout=5000")
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
    # `reviews_in_progress` is intentionally separate from `reviews` because
    # the lifetimes are orthogonal: rows here churn on a sub-minute scale
    # (one row per active `claude` subprocess), while `reviews` rows are
    # durable per-PR audit records. Keeping them apart leaves
    # `_record_review`'s UPSERT untouched and lets crash-recovery
    # `DELETE`s here never risk the audit table.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews_in_progress (
            pr_key TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            hostname TEXT NOT NULL,
            started_at TEXT NOT NULL
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


# --- In-progress reservations (cross-instance lock) ------------------------
#
# Every active `claude` review subprocess writes one row to the
# `reviews_in_progress` table while it runs and deletes it on exit. Any
# other cc-pr-reviewer instance polls the table to render an "in review"
# indicator and to gate `action_review` so the user can't accidentally
# launch a second `claude` against the same PR (which would have both tabs
# fighting over the same `gh pr checkout --force` working tree).
#
# Identity is `(pid, hostname)`. `started_at` is display-only (so cross-host
# clock skew is harmless). Stale rows from crashed peers are recovered
# lazily: when we see a row whose hostname matches ours but whose PID is
# dead, we delete it and proceed. Foreign-host rows are treated as opaque
# (we cannot probe a remote PID over NFS) — the override path in the UX
# layer is the escape hatch for genuinely orphaned remote rows.


@dataclass(frozen=True)
class InProgressHolder:
    """Identity of a `reviews_in_progress` row.

    Pulled out as a dataclass so call sites stop juggling
    `dict[str, Any]` shapes with implicit `int(...)`/`str(...)` casts on
    every read. Mirrors the `ConfirmResult`/`FilterChoice` pattern used
    elsewhere in this file. `started_at` is ISO-8601 with a trailing `Z`
    and is display-only — never load-bearing in identity checks.
    """

    pr_key: str
    pid: int
    hostname: str
    started_at: str


class ReviewInProgressError(Exception):
    """Raised when a reservation is blocked by a live (or unprobeable) holder.

    Carries the holder's identity so the caller can render a useful
    warning. We don't subclass `sqlite3.IntegrityError` because the cause
    isn't a schema problem — it's a normal cross-instance contention
    signal that the UX layer translates into a confirm-or-cancel modal.
    """

    def __init__(self, holder: InProgressHolder) -> None:
        super().__init__(
            f"PR {holder.pr_key} is being reviewed by pid {holder.pid} "
            f"on {holder.hostname} (since {holder.started_at})"
        )
        self.holder = holder


def _pid_alive(pid: int) -> bool:
    """True iff `pid` exists on the local host (Linux/macOS).

    `os.kill(pid, 0)` is the canonical POSIX liveness probe —
    `PermissionError` means the process exists but we can't signal it
    (treat as alive); `ProcessLookupError`/`OSError` means it's gone.

    On Windows, `os.kill(pid, 0)` raises `OSError [WinError 87]` for
    every PID (signal 0 isn't a valid Windows control event), so we
    can't probe liveness this way. We return True there — being
    conservative (a stuck-but-undetected holder is recoverable via the
    user-confirmed override; falsely declaring a live peer dead would
    silently double-launch). The cross-instance feature on Windows
    therefore degrades to a UX-level gate without crash recovery.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _row_to_holder(row: sqlite3.Row) -> InProgressHolder:
    return InProgressHolder(
        pr_key=str(row["pr_key"]),
        pid=int(row["pid"]),
        hostname=str(row["hostname"]),
        started_at=str(row["started_at"]),
    )


def _load_in_progress(conn: sqlite3.Connection) -> dict[str, InProgressHolder]:
    """Return all live in-progress rows, sweeping stale own-host rows in
    place. "Stale" = our hostname AND a dead PID; foreign-host rows are
    opaque and always returned. Sweeping here means the polling loop can
    `_load_in_progress(...)` and trust the result without a second pass.
    """
    rows = conn.execute("SELECT * FROM reviews_in_progress").fetchall()
    if not rows:
        return {}
    me = _APP_HOSTNAME
    live: dict[str, InProgressHolder] = {}
    stale: list[InProgressHolder] = []
    for r in rows:
        h = _row_to_holder(r)
        if h.hostname == me and not _pid_alive(h.pid):
            stale.append(h)
            continue
        live[h.pr_key] = h
    if stale:
        # Include `pid` in the WHERE so a same-host crash-and-restart
        # race (peer A crashed → peer B's reserve already swept and
        # re-inserted with its own PID before our DELETE) doesn't wipe
        # the fresh row. Without the pid guard, our DELETE would match
        # `(pr_key, hostname)` and remove the *new* holder.
        # Errors propagate: both callers already wrap this in
        # `try/except sqlite3.Error` and route the failure (abort + toast
        # in `action_review`, deduped warning in `_poll_in_progress`).
        # Suppressing here would hide read-only-mount, "database is
        # locked past busy_timeout", disk-full, and transient corruption
        # — the very signals those handlers exist to surface.
        conn.executemany(
            "DELETE FROM reviews_in_progress WHERE pr_key = ? AND hostname = ? AND pid = ?",
            [(h.pr_key, h.hostname, h.pid) for h in stale],
        )
        conn.commit()
    return live


def _reserve_in_progress(
    conn: sqlite3.Connection,
    pr_key: str,
    *,
    expected_holder: InProgressHolder | None = None,
) -> InProgressHolder:
    """Insert our marker row for `pr_key`. Returns the holder we wrote.

    `expected_holder` is the override path: the user explicitly chose
    "review anyway" against a holder they saw in the warn modal. We will
    atomically replace that holder iff the row's identity still matches
    — protecting against the modal-open → modal-confirm race where
    holder A finishes and a fresh holder B reserves before the user
    confirms. Without this discriminator a blind DELETE would silently
    evict B and let two tabs proceed into `gh pr checkout --force`.

    On `IntegrityError` (a peer beat us to INSERT), inspect the holder:
      * Stale own-host dead-PID → atomically replace and proceed.
      * Identity matches `expected_holder` → atomically replace.
      * Otherwise → raise `ReviewInProgressError` naming the actual
        current holder so the caller can re-prompt the user.
    """
    me_host = _APP_HOSTNAME
    me_pid = os.getpid()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    me = InProgressHolder(pr_key=pr_key, pid=me_pid, hostname=me_host, started_at=started_at)

    def _do_insert() -> None:
        conn.execute(
            "INSERT INTO reviews_in_progress (pr_key, pid, hostname, started_at) "
            "VALUES (?, ?, ?, ?)",
            (pr_key, me_pid, me_host, started_at),
        )

    try:
        _do_insert()
        conn.commit()
        return me
    except sqlite3.IntegrityError:
        pass

    row = conn.execute("SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)).fetchone()
    if row is None:
        # Conflict vanished between INSERT and re-SELECT (peer released
        # right behind us). Retry once. If it still raises, surface the
        # newest holder rather than swallowing the IntegrityError —
        # callers depend on the typed-exception contract to render UX.
        try:
            _do_insert()
            conn.commit()
            return me
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)
            ).fetchone()
            if row is None:
                # Truly degenerate (insert raises IntegrityError but no
                # conflicting row exists). Synthesize a holder so the
                # caller still gets a typed exception.
                raise ReviewInProgressError(
                    InProgressHolder(pr_key=pr_key, pid=0, hostname="?", started_at=started_at)
                ) from None

    holder = _row_to_holder(row)

    # Stale own-host: dead PID → replace. Atomic via Python sqlite3's
    # implicit transaction (DELETE+INSERT before any commit).
    if holder.hostname == me_host and not _pid_alive(holder.pid):
        if _atomic_replace(conn, holder, me):
            return me
        # Lost the race to another recoverer; re-read and decide.
        row = conn.execute(
            "SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)
        ).fetchone()
        if row is None:
            try:
                _do_insert()
                conn.commit()
                return me
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)
                ).fetchone()
                if row is None:
                    raise ReviewInProgressError(
                        InProgressHolder(pr_key=pr_key, pid=0, hostname="?", started_at=started_at)
                    ) from None
        holder = _row_to_holder(row)

    # Override mode: replace iff the holder is the one the user saw.
    if (
        expected_holder is not None
        and holder.pid == expected_holder.pid
        and holder.hostname == expected_holder.hostname
    ):
        if _atomic_replace(conn, holder, me):
            return me
        # Holder changed between SELECT and DELETE; re-read and surface.
        row = conn.execute(
            "SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)
        ).fetchone()
        if row is None:
            try:
                _do_insert()
                conn.commit()
                return me
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM reviews_in_progress WHERE pr_key = ?", (pr_key,)
                ).fetchone()
                if row is None:
                    raise ReviewInProgressError(
                        InProgressHolder(pr_key=pr_key, pid=0, hostname="?", started_at=started_at)
                    ) from None
        holder = _row_to_holder(row)

    raise ReviewInProgressError(holder)


def _atomic_replace(
    conn: sqlite3.Connection,
    expected: InProgressHolder,
    new: InProgressHolder,
) -> bool:
    """DELETE the row matching `expected`'s identity then INSERT `new`,
    inside a single implicit transaction (no commit between). Returns
    True iff the DELETE actually removed `expected`'s row (otherwise the
    holder identity changed and the caller must re-evaluate). Atomicity
    means peers see either the pre-state or the post-state — never an
    empty `(pr_key)` window during the swap.
    """
    cur = conn.execute(
        "DELETE FROM reviews_in_progress WHERE pr_key = ? AND pid = ? AND hostname = ?",
        (expected.pr_key, expected.pid, expected.hostname),
    )
    if cur.rowcount == 0:
        return False
    try:
        conn.execute(
            "INSERT INTO reviews_in_progress (pr_key, pid, hostname, started_at) "
            "VALUES (?, ?, ?, ?)",
            (new.pr_key, new.pid, new.hostname, new.started_at),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # WAL serialises writers, so this should be unreachable — but if
        # it ever fires, undo the DELETE and report failure.
        conn.rollback()
        return False
    return True


def _release_in_progress(conn: sqlite3.Connection, pr_key: str) -> None:
    """Delete our marker row. Idempotent and never raises — releasing
    must not tank the post-review path that records the review on rc==0.
    The `pid`/`hostname` guards prevent ever deleting a peer's row by
    mistake (e.g. if the user forced an override and our reserve replaced
    a peer row that itself later releases). On failure we still log to
    stderr so an orphaned reservation is at least diagnosable — silent
    swallowing here would mask a leaked row that no same-host sweep can
    reap (CLAUDE.md no-silent-fallback policy).
    """
    me_host = _APP_HOSTNAME
    me_pid = os.getpid()
    try:
        conn.execute(
            "DELETE FROM reviews_in_progress WHERE pr_key = ? AND pid = ? AND hostname = ?",
            (pr_key, me_pid, me_host),
        )
        conn.commit()
    except sqlite3.Error as e:
        # Suspended TUI: stderr lands above the "Press Enter to return"
        # prompt so the user actually sees it.
        print(
            f"warning: failed to release in-progress reservation for {pr_key}: {e}",
            file=sys.stderr,
        )


def _in_progress_age_str(started_at: str) -> str:
    """Format an in-progress row's `started_at` as a coarse age string for
    the warn-modal ("started 4m ago"). Falls back to the raw ISO string
    if parsing fails (foreign-host clock skew, malformed value, etc.).
    """
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return started_at
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _review_cell(
    pr: dict[str, Any],
    state: dict[str, dict[str, Any]],
    in_progress: bool = False,
) -> Text:
    """Render the "Reviews" column cell.

    Returns a Rich `Text` so styles (in-progress yellow, stale yellow)
    ride along into the DataTable. The `in_progress` flag is set by the
    polling loop when another `cc-pr-reviewer` instance has reserved this
    PR for review; we prepend a `⟳` glyph and bold-yellow the cell so it
    stands out without losing the count/stale info underneath.
    """
    entry = state.get(_pr_key(pr))
    if not entry:
        body = "-"
        style = ""
    else:
        count = entry.get("count", 0)
        stored_updated = entry.get("last_pr_updated_at", "")
        current_updated = pr.get("updatedAt", "")
        stale = stored_updated and current_updated and current_updated != stored_updated
        body = f"{count} stale" if stale else str(count)
        style = "yellow" if stale else ""
    if in_progress:
        return Text(f"⟳ {body}", style="bold yellow")
    return Text(body, style=style)


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


def _highlight_diff(diff: str) -> Text:
    """Colourise a unified-diff string the way `git diff` does.

    Returns a Rich `Text` so the modal can render it directly without going
    through markup parsing (diff bodies routinely contain `[` characters that
    would otherwise be mis-parsed).
    """
    # File-header lines from `git diff` / `gh pr diff` are matched with their
    # full leading sigil so they don't collide with content-line deletions of
    # comments such as `-- sql` (diff line `--- sql`) or YAML separators
    # (`---` → diff line `----`). The `--- a/`, `--- b/`, `--- /dev/null` form
    # is what git always emits for the file-header lines themselves.
    file_header_prefixes = (
        "diff --git",
        "index ",
        "similarity ",
        "rename ",
        "new file",
        "deleted file",
        "--- a/",
        "--- b/",
        "--- /dev/null",
        "+++ a/",
        "+++ b/",
        "+++ /dev/null",
    )
    out = Text()
    for raw_line in diff.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        nl = raw_line[len(line) :]
        if line.startswith(file_header_prefixes):
            style = "bold"
        elif line.startswith("@@"):
            style = "cyan"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        else:
            style = ""
        out.append(line, style=style)
        if nl:
            out.append(nl)
    return out


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
        body: str | Text
        try:
            r = run(["gh", "pr", "diff", str(self.number), "--repo", self.repo])
        except Exception as e:  # noqa: BLE001
            body = f"Error launching `gh pr diff`: {e}"
        else:
            if r.returncode == 0:
                body = _highlight_diff(r.stdout) if r.stdout else "(empty diff)"
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

    `cli` carries the per-launch CLI choice. It defaults to the global
    `PRReviewer.cli` at modal-open time and may be cycled inside the modal
    via Ctrl+L. The override is one-shot — the launcher honours it but
    does NOT write back to the global setting.
    """

    post_inline: bool
    extra_prompt: str = ""
    cli: CliChoice = DEFAULT_CLI

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
        # would otherwise win. CLI cycling is on `ctrl+l` for the same
        # reason — `ctrl+c` exits Textual.
        Binding("enter", "confirm", "Confirm", priority=True),
        Binding("ctrl+y", "confirm", "Confirm", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+n", "cancel", "Cancel", priority=True, show=False),
        Binding("ctrl+t", "toggle_post_inline", "Toggle post-inline", priority=True),
        Binding("ctrl+l", "cycle_cli", "Cycle CLI", priority=True),
    ]

    def __init__(self, prompt: str, default_cli: CliChoice = DEFAULT_CLI):
        super().__init__()
        self.prompt = prompt
        self.post_inline = True
        self.cli: CliChoice = default_cli

    def compose(self) -> ComposeResult:
        hint = (
            "[b]Enter[/] / [b]Ctrl+Y[/] confirm • [b]Esc[/] / [b]Ctrl+N[/] cancel "
            "• [b]Ctrl+T[/] toggle post-inline • [b]Ctrl+L[/] cycle CLI "
            "• [b]Shift+Enter[/] newline"
        )
        yield Vertical(
            Label(self.prompt, id="confirm-title", markup=False),
            Label(self._checkbox_text(), id="confirm-checkbox", markup=False),
            Label(self._cli_text(), id="confirm-cli", markup=False),
            Label("Extra prompt (optional):", id="confirm-extra-label"),
            ExtraPromptTextArea(id="confirm-extra"),
            Label(hint, id="confirm-hint"),
            id="confirm-container",
        )

    def _checkbox_text(self) -> str:
        mark = "[x]" if self.post_inline else "[ ]"
        return f"{mark} Post findings as inline PR comments"

    def _cli_text(self) -> str:
        return f"CLI: {_CLI_DISPLAY[self.cli]}"

    def action_confirm(self) -> None:
        text = self.query_one("#confirm-extra", TextArea).text
        self.dismiss(ConfirmResult(post_inline=self.post_inline, extra_prompt=text, cli=self.cli))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_post_inline(self) -> None:
        self.post_inline = not self.post_inline
        self.query_one("#confirm-checkbox", Label).update(self._checkbox_text())

    def action_cycle_cli(self) -> None:
        self.cli = _CLI_CYCLE[self.cli]
        self.query_one("#confirm-cli", Label).update(self._cli_text())


# --- In-progress warning modal ---------------------------------------------


class InProgressWarnScreen(ModalScreen[bool]):
    """Warn that another `cc-pr-reviewer` instance is already reviewing
    this PR, and ask whether to proceed anyway.

    Dismisses with `True` to override (caller should pass
    `force_in_progress=True` into `_launch_claude`), `False` to cancel.
    Kept distinct from `ConfirmScreen` because the intents don't overlap:
    `ConfirmScreen` tweaks launch options after the user decided to
    review; this screen asks whether the user wants to review at all.
    """

    BINDINGS = [
        Binding("o", "override", "Review anyway", priority=True),
        Binding("enter", "cancel", "Cancel", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("c", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, pr_label: str, holder: InProgressHolder, age: str) -> None:
        super().__init__()
        self.pr_label = pr_label
        self.holder = holder
        self.age = age

    def compose(self) -> ComposeResult:
        title = f"⟳ {self.pr_label} is already being reviewed"
        # Hostnames can contain `[` (rare but legal in some setups), and
        # PID/host/age are user-facing identity strings — keep markup off
        # for the title/body to avoid Rich parsing surprises. The hint
        # uses markup for emphasis on the keys.
        body = (
            f"Another cc-pr-reviewer instance reserved this PR\n"
            f"  pid {self.holder.pid} on {self.holder.hostname}, started {self.age}\n\n"
            "Launching a second review would have both tabs fight over\n"
            "the same `gh pr checkout --force` working tree."
        )
        hint = "[b]O[/] review anyway  •  [b]Enter[/] / [b]Esc[/] cancel"
        yield Vertical(
            Label(title, id="inprogress-title", markup=False),
            Label(body, id="inprogress-body", markup=False),
            Label(hint, id="inprogress-hint"),
            id="inprogress-container",
        )

    def action_override(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


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
    # `margin-right` on the changelog link reserves space for the clock to
    # its right. Without it the dock:right widgets pile up and overlap.
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
        # Pull the version from the App so the link tracks the same value the
        # lifecycle state machine sees. Escape it because Link parses Rich
        # markup — a PEP 440 local segment like "1.0+local[x]" would
        # otherwise raise MarkupError and kill header mount.
        version = self.app.installed_version  # type: ignore[attr-defined]
        link_text = f"📝 Release Notes (v{escape(version)})" if version else "📝 Release Notes"
        yield _HeaderLink(link_text, url=CHANGELOG_URL, id="changelog-link")
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
    #confirm-container, #filter-container, #inprogress-container {
        border: round $primary;
        padding: 1 2;
        margin: 4 8;
        background: $panel;
        height: auto;
    }
    #inprogress-container {
        border: round $warning;
    }
    #confirm-title, #filter-title, #inprogress-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #inprogress-title {
        color: $warning;
    }
    #inprogress-body {
        margin-bottom: 1;
    }
    #confirm-hint, #filter-hint, #inprogress-hint {
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

    TITLE = "CC PR Reviewer"
    SUB_TITLE = "Review Github PRs with Claude Code"

    BINDINGS = [
        Binding("r,f5", "refresh", "Refresh"),
        Binding("enter", "review", "Review"),
        Binding("o", "open_web", "Open in browser"),
        Binding("d", "show_diff", "View diff"),
        Binding("m", "toggle_mine", "Toggle my PRs"),
        Binding("f", "filter", "Filter by repo"),
        Binding("g", "toggle_group", "Group by"),
        Binding("s", "toggle_sort", "Sort by"),
        Binding("c", "toggle_cli", "CLI"),
        Binding("u", "upgrade", "Upgrade"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[dict[str, Any]] = []
        # Immutable: rebound wholesale, never mutated in place (writers must
        # swap a fresh tuple to stay safe across the FilterScreen worker).
        self.repo_cache: tuple[str, ...] = ()
        self.installed_version: str | None = _installed_version()
        self.latest_version: str | None = None
        # Update-check lifecycle: "pending" until the PyPI fetch returns,
        # then one of "current" / "available" / "failed" / "unavailable".
        # Drives both the badge (only shown for "available") and
        # `action_upgrade`'s status message (which needs to tell the user
        # *why* there's nothing to do).
        self.update_check_state: UpdateCheckState = "pending"
        self.review_db: sqlite3.Connection = _open_review_db()
        self.review_state: dict[str, dict[str, Any]] = _load_review_state(self.review_db)
        stored_filter = _get_setting(self.review_db, "repo_filter", "")
        self.repo_filter: str | None = stored_filter or None
        self.include_mine: bool = _get_setting(self.review_db, "include_mine", "0") == "1"
        stored_group = _get_setting(self.review_db, "group_by", "")
        self.group_by: GroupBy = stored_group if stored_group in _GROUP_CYCLE else ""
        stored_sort = _get_setting(self.review_db, "sort_by", "")
        self.sort_by: SortBy = stored_sort if stored_sort in _SORT_CYCLE else ""
        stored_cli = _get_setting(self.review_db, "cli", DEFAULT_CLI)
        persisted: CliChoice = stored_cli if stored_cli in _CLI_CYCLE else DEFAULT_CLI
        # If the persisted CLI isn't on PATH, fall back to whatever IS
        # installed (in cycle order from the persisted preference) so a
        # Codex-only user on a fresh install doesn't get stuck with the
        # `claude` default. The fallback is session-only — pressing `c`
        # is what persists a new choice. `check_prereqs` already
        # guaranteed at least one CLI is available, so the None branch
        # below is defensive (e.g. race where someone uninstalled the
        # CLI between prereq check and __init__).
        if shutil.which(persisted) is None:
            fallback = _first_available_cli(persisted)
            self.cli: CliChoice = fallback if fallback is not None else persisted
            self._cli_fallback_from: CliChoice | None = (
                persisted if fallback is not None and fallback != persisted else None
            )
        else:
            self.cli = persisted
            self._cli_fallback_from = None
        self._row_to_pr_idx: list[int | None] = []
        # Snapshot of `reviews_in_progress` rows from the most recent poll,
        # keyed by `pr_key`. `_poll_in_progress` diffs against this to
        # decide which cells need an update; `action_review` consults it
        # to gate launches against PRs another tab is currently reviewing.
        self._in_progress: dict[str, InProgressHolder] = {}
        # Tracks whether the most recent poll-error was already surfaced,
        # so a persistent failure doesn't spam a toast every 3 s.
        self._poll_error_shown: bool = False
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
        # Explicit column keys so `_poll_in_progress` can target the
        # "Reviews" cell via `table.update_cell(row_key, "reviews", …)`
        # without a full _populate rebuild. Other keys stay symmetrical
        # for free; today only "reviews" is referenced by name.
        table.add_column("Repository", key="repo")
        table.add_column("#", key="number")
        table.add_column("Title", key="title")
        table.add_column("Author", key="author")
        table.add_column("Updated", key="updated")
        table.add_column("Reviews", key="reviews")
        table.add_column("Last Review", key="last_review")
        table.add_column("", key="tags")
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
        # Surface the CLI fallback (set in `__init__` when the persisted
        # CLI wasn't on PATH) as a warning toast — startup goes through
        # without dying, but the user should know they're on a different
        # CLI than they configured.
        if self._cli_fallback_from is not None:
            self.notify(
                f"`{self._cli_fallback_from}` not on PATH — "
                f"using {_CLI_DISPLAY[self.cli]} this session. "
                "Press `c` to switch, or install the missing CLI.",
                severity="warning",
                timeout=10,
            )
        self.action_refresh()
        # Cross-instance "in review" indicator. 3 s is fast enough to feel
        # live (another tab finishing/starting a review is reflected
        # within one tick) and cheap enough to be invisible — one small
        # SELECT plus a bounded scan over `_row_to_pr_idx` per tick.
        self.set_interval(3.0, self._poll_in_progress)
        if self.installed_version is None:
            # Source/editable install: nothing to compare against on PyPI, so
            # skip the worker and surface a tailored message via `u`.
            self.update_check_state = "unavailable"
        else:
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
        # Highlight `c` whenever the user has moved off the default CLI,
        # so the footer always advertises a non-default selection without
        # consuming a separate status-bar slot.
        self._set_footer_active("c", self.cli != DEFAULT_CLI)

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
        self._set_pr_count("…")
        self._load_prs()

    @work(thread=True, exclusive=True)
    def _load_prs(self) -> None:
        repo = self.repo_filter
        try:
            data = fetch_review_prs(repo)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._set_status, f"Error fetching review PRs: {e}", True)
            # Without this, the "…" placeholder set by `action_refresh` /
            # `action_toggle_mine` would linger forever — the user can't
            # distinguish in-flight from failed without scanning the status
            # bar.
            self.call_from_thread(self._set_pr_count, "?")
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
        # Count review-requested PRs separately from `_mine=True` rows so the
        # primary number reflects what the user actually has to act on. Append
        # `(+N mine)` whenever the `m` toggle pulled extras in, mirroring the
        # status-bar's `(+mine: N)` style — without that suffix a `No PRs to
        # review` label looks wrong when mine-rows are visibly in the table.
        to_review = sum(1 for p in data if not p.get("_mine"))
        mine = sum(1 for p in data if p.get("_mine"))
        label = f"{to_review} to review" if to_review else "No PRs to review"
        if mine:
            label += f" (+{mine} mine)"
        self._set_pr_count(label)
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
                _review_cell(
                    pr,
                    self.review_state,
                    in_progress=_pr_key(pr) in self._in_progress,
                ),
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
        if self.update_check_state == "pending":
            self.notify("Checking for updates…", title="Upgrade", timeout=3)
            return
        if self.update_check_state == "current":
            self.notify(
                f"Already up to date (v{self.installed_version}).",
                title="Upgrade",
                timeout=4,
            )
            return
        if self.update_check_state == "unavailable":
            self.notify(
                "Running from source — no upgrade available. "
                f"Install via `uv tool install {PACKAGE_NAME}` to enable upgrades.",
                title="Upgrade",
                timeout=5,
            )
            return
        if self.update_check_state == "failed":
            self.notify(
                f"Update check failed — see {RELEASES_URL}",
                title="Upgrade",
                severity="error",
                timeout=5,
            )
            return
        assert self.latest_version is not None  # narrowed by "available" branch
        if shutil.which("uv") is None:
            self.notify(
                f"`uv` not on PATH — install uv (https://docs.astral.sh/uv/) "
                f"then run: uv tool upgrade {PACKAGE_NAME}",
                title="Upgrade",
                severity="error",
                timeout=6,
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
        prompt = f"Launch review for {repo}#{pr['number']}?\n{title}"
        pr_label = f"{repo}#{pr['number']}"

        def _confirm(expected_holder: InProgressHolder | None) -> None:
            def _proceed(result: ConfirmResult | None) -> None:
                if result is not None:
                    self._launch_claude(
                        pr,
                        result.post_inline,
                        result.extra_prompt,
                        cli=result.cli,
                        expected_holder=expected_holder,
                    )

            self.push_screen(ConfirmScreen(prompt, default_cli=self.cli), _proceed)

        # Use the cached snapshot from the periodic worker-thread poll
        # rather than a synchronous re-poll. Two reasons:
        #   1. A synchronous `_load_in_progress` on the keystroke path
        #      can stall the UI for up to `busy_timeout=5000` ms when
        #      the DB is contended (peer mid-`_atomic_replace`,
        #      NFS-hosted workspace) — exactly the freeze the worker
        #      poll was introduced to avoid.
        #   2. The hard safety boundary is `_reserve_in_progress` inside
        #      `_launch_claude`. If the cache misses a peer that just
        #      started 200 ms ago, the reserve still raises
        #      `ReviewInProgressError` and the launch path prints a
        #      message + waits for Enter. The cache is a UX optimisation
        #      to show the warn modal early, not the actual gate.
        holder = self._in_progress.get(_pr_key(pr))
        if holder is None:
            _confirm(expected_holder=None)
            return

        def _on_warn(override: bool | None) -> None:
            if override:
                # Pass the holder identity captured *now* (modal-open
                # time) into the override path. `_reserve_in_progress`
                # uses it as a discriminator: if the holder identity has
                # changed by reserve-time (peer A finished and a fresh
                # peer B reserved while the user was reading the modal),
                # the override fails closed rather than blindly evicting
                # B's legitimate row.
                _confirm(expected_holder=holder)

        self.push_screen(
            InProgressWarnScreen(
                pr_label=pr_label,
                holder=holder,
                age=_in_progress_age_str(holder.started_at),
            ),
            _on_warn,
        )

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
        self._set_pr_count("…")
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

    def action_toggle_cli(self) -> None:
        # CLI choice doesn't affect the table contents, so there's no
        # cursor preservation or re-populate to do — just cycle, persist,
        # update the footer indicator, and notify. The launcher reads
        # `self.cli` at action_review time.
        nxt = _CLI_CYCLE[self.cli]
        self.cli = nxt
        try:
            _set_setting(self.review_db, "cli", nxt)
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist CLI toggle: {e}", severity="warning")
        self.notify(f"CLI: {_CLI_DISPLAY[nxt]}", timeout=3)
        self._refresh_footer_indicators()

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

    def _launch_claude(
        self,
        pr: dict[str, Any],
        post_inline: bool,
        extra_prompt: str,
        cli: CliChoice = DEFAULT_CLI,
        expected_holder: InProgressHolder | None = None,
    ) -> None:
        repo_full = pr["repository"]["nameWithOwner"]
        owner, name = repo_full.split("/", 1)
        number = pr["number"]
        local_path = WORKSPACE / owner / name
        key = _pr_key(pr)

        # Pre-flight: surface a missing CLI binary as a toast in the TUI
        # rather than suspending and immediately failing at exec. This
        # primarily catches the per-launch override case (user toggles
        # CLI from the modal to one they don't have installed). Startup
        # `check_prereqs` only verifies that at least one CLI exists —
        # not the specific one being launched — so this check is the
        # one that catches "you toggled to claude but it's not here".
        if shutil.which(cli) is None:
            self.notify(
                f"`{cli}` not found on PATH — install it or pick a different CLI",
                severity="error",
                timeout=8,
            )
            return

        # Claude additionally needs the PR Review Toolkit plugin (the
        # base prompt invokes the toolkit's agents by name). Codex and
        # Gemini reference the bundled `.md` files instead, so no extra
        # check applies to them. The plugin check is the slow one — it
        # shells out to `claude plugin list --json` — so it stays gated
        # behind the binary check above.
        if cli == "claude" and _pr_review_toolkit_enabled() is False:
            self.notify(
                "PR Review Toolkit plugin not enabled — run "
                f"`claude plugin install {PR_REVIEW_TOOLKIT_PLUGIN}` "
                "or pick a different CLI",
                severity="error",
                timeout=10,
            )
            return

        # Suspend the TUI so the coding-agent CLI can take over stdin/stdout.
        with self.suspend():
            WORKSPACE.mkdir(parents=True, exist_ok=True)
            print(f"\n── Reviewing {repo_full}#{number} with {_CLI_DISPLAY[cli]} ──\n")

            # Reserve BEFORE clone/checkout: `gh pr checkout --force`
            # mutates the shared workspace tree. Two tabs both passing
            # the action_review gate would otherwise both run that
            # command and switch branches under each other — the very
            # race this feature exists to prevent. Hold the reservation
            # across the entire suspend block so every existing
            # early-return path still releases (clone-fail, checkout-fail,
            # ReviewInProgressError, Ctrl-C, clean exit).
            try:
                _reserve_in_progress(self.review_db, key, expected_holder=expected_holder)
            except ReviewInProgressError as e:
                print(
                    f"\nAnother review of {repo_full}#{number} is in progress "
                    f"(pid {e.holder.pid} on {e.holder.hostname}). Aborting — "
                    "wait for it to finish, or re-open the warn modal to "
                    "override the new holder.\n"
                )
                input("Press Enter to return to the TUI…")
                return

            try:
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

                # Capture once so the banner can disambiguate a structural
                # `rereview=False` (no prior comment from us) from a missing-data
                # `rereview=False` (login lookup failed, bar-raise silently dropped).
                # Without this seam the user can't tell the two apart, and the
                # `_current_gh_login` warning may have scrolled past during clone
                # or checkout output.
                my_login = _current_gh_login()
                built = build_review_prompt(
                    post_inline=post_inline,
                    extra_prompt=extra_prompt,
                    existing=existing,
                    fetch_ok=fetch_ok,
                    my_login=my_login,
                    author_login=(pr.get("author") or {}).get("login"),
                    cli=cli,
                )

                if not fetch_ok:
                    existing_desc = "existing comments: fetch failed"
                else:
                    existing_desc = (
                        f"existing comments: {built.existing_shown} in prompt of "
                        f"{built.existing_total} fetched"
                    )

                cmd = _build_cli_command(cli, built.text)
                post_inline_desc = "on" if post_inline else "off"
                if post_inline and built.rereview:
                    post_inline_desc += ", rereview"
                elif post_inline and my_login is None:
                    post_inline_desc += ", rereview-detection-unavailable"
                parts = [f"post-inline: {post_inline_desc}", existing_desc]
                if extra_prompt:
                    # `!r` keeps newlines/control chars visible so a misclick paste
                    # (e.g. a secret) is spottable before the CLI consumes it. The
                    # explicit `(+N more chars)` suffix is the load-bearing piece:
                    # without it, a 201-char paste renders identically to a clean
                    # 200-char one while the full text still flows into argv,
                    # defeating the whole point of the preview.
                    shown = extra_prompt[:EXTRA_PROMPT_BANNER_CAP]
                    hidden = len(extra_prompt) - len(shown)
                    suffix = f" (+{hidden} more chars)" if hidden else ""
                    parts.append(f"extra prompt: {shown!r}{suffix}")
                print(
                    f"\nLaunching {_CLI_DISPLAY[cli]} ({', '.join(parts)})"
                    " — exit the CLI when you're done.\n"
                )
                rc = subprocess.call(cmd, cwd=local_path)

                # Only count this as a review if the CLI exited cleanly.
                # Ctrl-C, crashes, or a failed launch leave rc != 0;
                # recording those would inflate the "Reviews" count and
                # reset staleness for a PR that wasn't actually reviewed,
                # hiding genuine drift from the next real session.
                if rc == 0:
                    self.review_state[key] = _record_review(
                        self.review_db,
                        key,
                        pr.get("updatedAt", ""),
                        head_sha,
                    )
                else:
                    print(
                        f"\n{_CLI_DISPLAY[cli]} exited with status {rc}; "
                        "not recording this as a review."
                    )

                input(
                    f"\n── {_CLI_DISPLAY[cli]} session ended. Press Enter to return to the TUI ──"
                )
            finally:
                _release_in_progress(self.review_db, key)

        # Refresh in case review state changed (e.g. you approved the PR).
        self.action_refresh()

    # --- helpers ---

    def _set_status(self, msg: str, error: bool = False) -> None:
        w = self.query_one("#status", Static)
        w.update(msg)
        w.set_class(error, "-error")

    def _set_pr_count(self, msg: str) -> None:
        # Reactive on the App; HeaderTitle re-renders automatically. The
        # bracketed `[msg]` segment is colored by `format_title`.
        self.title = f"{type(self).TITLE} [{msg}]"

    def format_title(self, title: str, sub_title: str) -> Content:
        # Two-tone styling on the title:
        #   • leading "CC PR Reviewer" → bold $primary (matches the theme
        #     primary palette color)
        #   • bracketed "[count]" suffix → bold $accent (same emphasis the
        #     standalone Static used to have)
        # Anything outside that pattern falls back to default header styling.
        bracket_start = title.find("[")
        bracket_end = title.rfind("]")
        if bracket_start != -1 and bracket_end > bracket_start:
            prefix = title[:bracket_start].rstrip()
            gap = title[len(prefix) : bracket_start]
            title_content = Content.assemble(
                (prefix, "bold $primary"),
                gap,
                (title[bracket_start : bracket_end + 1], "bold $accent"),
                title[bracket_end + 1 :],
            )
        else:
            title_content = Content.assemble((title, "bold $primary"))
        if sub_title:
            return Content.assemble(
                title_content,
                (" — ", "dim"),
                Content(sub_title).stylize("dim"),
            )
        return title_content

    @work(thread=True, exclusive=True)
    def _poll_in_progress(self) -> None:
        """Refresh the cross-instance in-progress snapshot and repaint
        only the cells whose state changed.

        Runs on a worker thread (mirrors `_load_prs` and
        `_check_for_update`) because the SELECT can block for up to
        `busy_timeout=5000` ms when the DB is contended (peer mid-reserve,
        NFS/SMB-hosted `$GH_PR_WORKSPACE`). On the main loop that would
        freeze the UI for a full tick. `exclusive=True` collapses
        overlapping ticks if a previous poll is still running.

        Uses `_load_in_progress` which sweeps stale own-host rows in
        place, so a peer that crashed mid-review is cleaned up here too.
        Marshals cell updates back to the main thread via
        `call_from_thread` (Textual widget access is main-thread-only).
        """
        try:
            new = _load_in_progress(self.review_db)
        except sqlite3.Error as e:
            # Polling failure shouldn't tear down the TUI. Leave
            # `self._in_progress` alone so the `⟳` glyph and
            # `action_review`'s gate remain *consistent* — both reflect
            # the last-known truth — even though we can't refresh them.
            # Clearing the dict here would create a worse lie: cells
            # would still show `⟳` (we can't repaint without the DB)
            # while the gate would silently say "no holder", letting
            # the user launch a duplicate review without warning.
            # The reserve in `_launch_claude` is still the hard
            # boundary, and the toast tells the user the snapshot is
            # stale. Dedupe so a persistent failure doesn't fire every
            # 3 s.
            self.call_from_thread(self._handle_poll_error, str(e))
            return
        self.call_from_thread(self._apply_in_progress_snapshot, new)

    def _handle_poll_error(self, message: str) -> None:
        if not self._poll_error_shown:
            self.notify(
                f"In-progress poll failed: {message}",
                severity="warning",
                timeout=6,
            )
            self._poll_error_shown = True

    def _apply_in_progress_snapshot(self, new: dict[str, InProgressHolder]) -> None:
        """Diff the new snapshot against the in-memory one and repaint
        only the affected `Reviews` cells. Runs on the main thread
        (called via `call_from_thread`)."""
        # A successful poll clears the dedupe latch so a subsequent
        # failure surfaces a fresh toast.
        self._poll_error_shown = False
        prev = self._in_progress
        if new == prev:
            return
        affected = set(new) ^ set(prev)
        self._in_progress = new
        if not affected:
            return
        try:
            table = self.query_one("#pr-table", DataTable)
        except NoMatches:
            return
        # Map pr_key → index in self.prs once, so we can update cells
        # without an O(N*M) scan when many PRs change state at once.
        key_to_idx: dict[str, int] = {}
        for i, pr in enumerate(self.prs):
            key_to_idx[_pr_key(pr)] = i
        for key in affected:
            idx = key_to_idx.get(key)
            if idx is None:
                # PR isn't currently rendered (filtered out, scrolled to
                # a different view, etc.). Nothing to repaint; the
                # snapshot still tracks it for `action_review`'s gate.
                continue
            pr = self.prs[idx]
            try:
                table.update_cell(
                    str(idx),
                    "reviews",
                    _review_cell(pr, self.review_state, in_progress=key in new),
                )
            except CellDoesNotExist:
                # Row gone between the snapshot and the update (filter
                # change, repopulate race). Skip; the next full populate
                # will paint the right state.
                continue

    @work(thread=True, exclusive=True)
    def _check_for_update(self) -> None:
        # Caller (`on_mount`) only reaches here when installed_version is set;
        # source installs short-circuit to "unavailable" without enqueueing.
        current = self.installed_version
        assert current is not None
        latest = _fetch_latest_version()
        if latest is None:
            self.call_from_thread(self._set_update_check_result, "failed", None)
            return
        state: UpdateCheckState = "available" if _is_newer(latest, current) else "current"
        self.call_from_thread(self._set_update_check_result, state, latest)

    def _set_update_check_result(self, state: UpdateCheckState, latest: str | None) -> None:
        # Guard against the worker firing after teardown: query_one would
        # raise NoMatches and surface in Textual's error log otherwise.
        if not self.is_mounted:
            return
        self.update_check_state = state
        self.latest_version = latest
        if state == "available" and latest is not None:
            w = self.query_one("#version-badge", Static)
            # Wrap dynamic parts in Text() — `latest` comes from external
            # PyPI JSON and shouldn't be trusted to be Rich-markup-safe.
            w.update(Text(f" ▲ v{latest} available — uv tool upgrade {PACKAGE_NAME} (press u) "))
            w.add_class("-visible")


# --- Entry point -----------------------------------------------------------


def main() -> None:
    problems = check_prereqs()
    if problems:
        print("⚠  Prerequisites not met:\n")
        for p in problems:
            print(f"  • {p}")
        print()
        raise SystemExit(1)
    PRReviewer().run()


if __name__ == "__main__":
    main()
