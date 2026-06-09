#!/usr/bin/env python3
"""
cc-pr-reviewer — a small Textual TUI that lists GitHub PRs where you are a
requested reviewer and hands any selected PR off to a coding-agent CLI
(Claude Code, OpenAI Codex CLI, or Google Gemini CLI), with the PR Review
Toolkit (Claude) or the bundled `.agents/skills/` (Codex/Gemini) driving
the review.

Flow when you pick a PR and press Enter:
    1. Clone the repo into $GH_PR_WORKSPACE (if not already there)
    2. `gh pr checkout <N>` so the CLI sees the PR's working tree
    3. For codex/gemini: materialise the six bundled review skills into
       `<workspace>/.agents/skills/` for auto-discovery (cleaned up on exit)
    4. Launch the chosen CLI with a prompt that drives the six review dimensions
    5. When you /exit the CLI, the TUI resumes

Prerequisites:
    • gh CLI, authenticated                     https://cli.github.com
    • at least one of:
        - Claude Code                           https://docs.claude.com/claude-code
          (+ PR Review Toolkit plugin           https://claude.com/plugins/pr-review-toolkit)
        - OpenAI Codex CLI                      https://github.com/openai/codex
        - Google Gemini CLI                     https://github.com/google-gemini/gemini-cli

Run:
    uv sync
    uv run cc-pr-reviewer
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
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
from textual.timer import Timer
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
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

# Codex and Gemini don't have a plugin marketplace, but both natively
# auto-discover skills at `<workspace>/.agents/skills/<name>/SKILL.md`. The
# six toolkit agents ship as bundled SKILL.md files (see
# `cc_pr_reviewer/skills/`); at launch we materialise them into the
# checked-out PR's workspace and clean up on exit. Names appear in the
# review prompt prefixed with `$` to force explicit activation — we want
# every dimension to run, not have the model implicitly pick a subset.
#
# The order here is the order the prompt asks for them — code-reviewer
# first sets project-guideline context, then the targeted checks (silent
# failures, type design, tests), then the cleanup-oriented passes
# (comments, simplification). Changing the order is a behaviour change,
# so the tuple is the single source of truth consulted by both the
# prompt builder and the materialise/cleanup helpers.
REVIEW_SKILLS: tuple[str, ...] = (
    "code-reviewer",
    "silent-failure-hunter",
    "type-design-analyzer",
    "pr-test-analyzer",
    "comment-analyzer",
    "code-simplifier",
)

# Friendly labels for each skill — used in the ConfirmScreen agent
# checkboxes AND in the Claude base prompt's "Run the relevant agents — …"
# enumeration. Mirrors the upstream PR Review Toolkit plugin's agent names
# (so prompts read naturally and match how those agents announce themselves).
REVIEW_SKILL_LABELS: dict[str, str] = {
    "code-reviewer": "Code Reviewer",
    "silent-failure-hunter": "Silent Failure Hunter",
    "type-design-analyzer": "Type Design Analyzer",
    "pr-test-analyzer": "PR Test Analyzer",
    "comment-analyzer": "Comment Analyzer",
    "code-simplifier": "Code Simplifier",
}

# Topic phrase per skill — assembled into the skill-based prompt's "they
# cover …" clause. Keeping it per-skill (rather than the previous fixed
# six-way enumeration) means an unchecked agent drops both its `$mention`
# AND its coverage phrase, so the prompt stays coherent for any subset.
_SKILL_COVERAGE: dict[str, str] = {
    "code-reviewer": "project-guideline compliance",
    "silent-failure-hunter": "silent failures",
    "type-design-analyzer": "type design",
    "pr-test-analyzer": "test coverage",
    "comment-analyzer": "comments",
    "code-simplifier": "code simplification",
}

SKILL_FILE_NAME = "SKILL.md"


def _skills_dir() -> Path:
    """Return the bundled directory holding the Codex/Gemini SKILL.md files.

    Resolved against `__file__` so editable installs (`uv sync`) and wheel
    installs (`pip install`) both work — hatchling packages the directory
    next to `__init__.py` in both cases. `importlib.resources` is avoided
    because the SKILL.md files are read by an external subprocess
    (`codex` / `gemini`) after we copy them into the PR workspace, not by
    Python — so we always need a filesystem path, not a `Traversable`.
    """
    return Path(__file__).parent / "skills"


def _join_agents(items: list[str]) -> str:
    """Oxford-style enumeration: "A", "A and B", "A, B, and C"."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# Cardinal words for small counts, used in "the {N} review skills available
# in this workspace" — reads more naturally than a digit for ≤ 10.
_COUNT_WORDS: dict[int, str] = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
}


def _build_claude_prompt(selected: tuple[str, ...] = REVIEW_SKILLS) -> str:
    """Compose the review prompt for Claude (uses the PR Review Toolkit plugin).

    When `selected` is empty the prompt asks for a generic toolkit review with
    no agent-name pinning — useful for doc-only PRs where the user opted out of
    every targeted agent but still wants the plugin to run. Otherwise the named
    agents are spelled out so the plugin's router picks exactly those.
    """
    if not selected:
        return (
            "Please perform a comprehensive review of the current PR using the "
            "PR Review Toolkit and give me a prioritised summary of findings "
            "with file:line references and suggested fixes."
        )
    labels = [REVIEW_SKILL_LABELS[name] for name in selected]
    return (
        "Please perform a comprehensive review of the current PR using the "
        f"PR Review Toolkit. Run the relevant agents — {_join_agents(labels)} — "
        "and give me a prioritised summary of findings with file:line "
        "references and suggested fixes."
    )


REVIEW_PROMPT_CLAUDE = _build_claude_prompt()


def _build_skill_based_prompt(selected: tuple[str, ...] = REVIEW_SKILLS) -> str:
    """Compose the review prompt for the skill-based CLIs (codex, gemini).

    Codex and Gemini are *documented* to auto-discover skills at
    session start by scanning `.agents/skills/<name>/SKILL.md` and to
    inject only the metadata (name + description) into context — the
    full SKILL.md body loads on activation. Refs:
    https://developers.openai.com/codex/skills and
    https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/skills.md.
    If either CLI changes its loading semantics, the launch still runs
    and the `$<name>` mentions still appear in the prompt — but the
    skills wouldn't activate. The frontmatter-shape check in
    `check_prereqs` catches a corrupted *install*, but not an upstream
    CLI behaviour change.

    `$<name>` mentions force explicit activation — we want every
    *selected* review dimension to run, not have the model implicitly
    pick a subset.

    Identical for codex and gemini — they share both the prompt and the
    `.agents/skills/` interop convention, and differ only in how they're
    launched. `selected` accepts a subset so the user can drop agents
    that don't apply (e.g. test/code-review skills on a doc-only PR);
    when empty, the prompt degrades to a generic review with no skill
    mentions so the agent doesn't fail-search for skills we didn't
    materialise.
    """
    if not selected:
        return (
            "Please perform a comprehensive review of the current PR and "
            "give me a prioritised summary of findings with file:line "
            "references and suggested fixes."
        )
    mentions = ", ".join(f"${name}" for name in selected)
    coverage = _join_agents([_SKILL_COVERAGE[name] for name in selected])
    n = len(selected)
    count = _COUNT_WORDS.get(n, str(n))
    skill_word = "skill" if n == 1 else "skills"
    activation = (
        "Activate the skill — it covers" if n == 1 else "Activate each skill in turn — they cover"
    )
    return (
        "Please perform a comprehensive review of the current PR using the "
        f"{count} review {skill_word} available in this workspace: {mentions}. "
        f"{activation} {coverage} — then give me a prioritised summary of "
        "findings with file:line references and suggested fixes."
    )


REVIEW_PROMPT_SKILL_BASED = _build_skill_based_prompt()


@dataclass(frozen=True)
class _SkillSnapshot:
    """Pre-materialise state of one `.agents/skills/<name>/` dir.

    `original_skill_md` is the byte content of any pre-existing
    `SKILL.md` at the target (or `None` if the file didn't exist).
    `skill_dir_existed` records whether the parent skill dir was
    already there — so cleanup only `rmdir`s the dirs we created.
    """

    original_skill_md: bytes | None
    skill_dir_existed: bool


@dataclass(frozen=True)
class _MaterialisedSkills:
    """Restoration manifest produced by `_materialise_skills`.

    Captures pre-launch workspace state so `_cleanup_skills` can restore
    the worktree byte-for-byte even when the PR happens to ship its own
    `.agents/skills/<our-name>/SKILL.md`. Without this, materialise
    would overwrite a tracked file and cleanup would `unlink` it,
    leaving the working tree dirty after every review.
    """

    workspace: Path
    skills: dict[str, _SkillSnapshot]
    skills_root_existed: bool
    agents_dir_existed: bool


def _materialise_skills(
    workspace: Path, selected: tuple[str, ...] | None = None
) -> _MaterialisedSkills:
    """Copy each bundled SKILL.md into `<workspace>/.agents/skills/<name>/SKILL.md`.

    Codex and Gemini auto-discover skills under `.agents/skills/`. We
    write the bundled skills there so they're picked up by the CLI at
    session start. Pre-existing content at every target path (SKILL.md
    bytes, parent-dir presence) is snapshotted into the returned
    `_MaterialisedSkills` manifest BEFORE the write, so `_cleanup_skills`
    can restore the worktree exactly — including the rare case where a
    reviewed repo ships its own competing skill of the same name.

    `selected` defaults to all of `REVIEW_SKILLS`. When the caller passes a
    subset (e.g. ConfirmScreen lets the reviewer skip the code-review and
    test agents for a doc-only PR), only those skills are materialised so
    codex/gemini's auto-discovery doesn't even see the unselected ones —
    that's stronger than just omitting them from the prompt mentions, since
    those CLIs can implicitly activate any skill present in `.agents/skills/`.

    Unknown names are rejected up-front with `ValueError`. The `shutil.copy2`
    block below catches `OSError` (disk full, perms, read-only FS) and
    re-raises it as a `RuntimeError("failed to materialise skill …")`; without
    this guard, a typo'd skill name would surface as that same RuntimeError
    via `FileNotFoundError` on the bundled source, misclassifying a bad-input
    bug as an environment failure.
    """
    if selected is None:
        selected = REVIEW_SKILLS
    else:
        unknown = [name for name in selected if name not in REVIEW_SKILL_LABELS]
        if unknown:
            raise ValueError(
                f"unknown skill name(s) in selected: {unknown!r}; "
                f"expected subset of {list(REVIEW_SKILLS)!r}"
            )

    skills_root = workspace / ".agents" / "skills"
    agents_dir = workspace / ".agents"

    agents_dir_existed = agents_dir.is_dir()
    skills_root_existed = skills_root.is_dir()

    snapshots: dict[str, _SkillSnapshot] = {}
    src_dir = _skills_dir()

    for name in selected:
        skill_dir = skills_root / name
        skill_md = skill_dir / SKILL_FILE_NAME

        skill_dir_existed = skill_dir.is_dir()
        original_bytes = skill_md.read_bytes() if skill_md.is_file() else None

        snapshots[name] = _SkillSnapshot(
            original_skill_md=original_bytes,
            skill_dir_existed=skill_dir_existed,
        )

        skill_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_dir / name / SKILL_FILE_NAME, skill_md)
        except OSError as e:
            # Disk full, permission denied, read-only FS — surface which
            # skill failed so the user has a clear diagnostic instead of
            # a bare `shutil` traceback. The partial manifest still has
            # snapshots for every skill we DID write to, so `finally`
            # cleanup can restore those before propagating this error.
            raise RuntimeError(f"failed to materialise skill {name!r}: {e}") from e

    return _MaterialisedSkills(
        workspace=workspace,
        skills=snapshots,
        skills_root_existed=skills_root_existed,
        agents_dir_existed=agents_dir_existed,
    )


def _rmdir_if_empty(path: Path) -> None:
    """`rmdir` only if the dir is empty; warn on any other OSError.

    Replaces the previous `contextlib.suppress(OSError)`, which would
    have hidden a perms regression or a stale lock the same as the
    expected ENOTEMPTY case — leaving orphan files with no signal.
    """
    try:
        path.rmdir()
    except OSError as e:
        if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            print(f"warning: could not rmdir {path}: {e}")


def _cleanup_skills(manifest: _MaterialisedSkills) -> None:
    """Reverse of `_materialise_skills` using the manifest it returned.

    For each skill: if a SKILL.md existed before materialise, restore
    those exact bytes; otherwise unlink the one we wrote. Then `rmdir`
    any parent dir we created (`rmdir` only succeeds on empty dirs, so
    sibling content the user owns is preserved automatically).

    The byte restore uses `write_bytes` rather than swallowing errors —
    if we can't put the user's tracked file back, that's a data-loss
    bug we want loud, not silent. The unlink branch uses an explicit
    `suppress(FileNotFoundError)` rather than `missing_ok=True` so that
    other `OSError` subtypes (perms, replaced-by-dir) still surface
    instead of masking the originating launch exception when this
    runs inside `finally`.
    """
    skills_root = manifest.workspace / ".agents" / "skills"
    for name, snap in manifest.skills.items():
        skill_md = skills_root / name / SKILL_FILE_NAME
        if snap.original_skill_md is not None:
            skill_md.write_bytes(snap.original_skill_md)
        else:
            with contextlib.suppress(FileNotFoundError):
                skill_md.unlink()
        if not snap.skill_dir_existed:
            _rmdir_if_empty(skills_root / name)

    if not manifest.skills_root_existed:
        _rmdir_if_empty(skills_root)
    if not manifest.agents_dir_existed:
        _rmdir_if_empty(manifest.workspace / ".agents")


def _build_cli_command(cli: CliChoice, prompt_text: str) -> list[str]:
    """Build the subprocess argv for handing control to the selected CLI.

    Flag picks minimise manual permission prompts so the review runs
    end-to-end with little intervention, while keeping edits scoped to the
    cloned PR workspace and no broader host access:

    * `claude --permission-mode auto`. Auto mode lets Claude Code's
      classifier auto-approve the actions a review needs — edits plus the
      `git`/`gh api …` bash calls the post-inline path runs — while still
      gating genuinely risky operations. This is broader than the older
      `acceptEdits` (which auto-approved edits but still prompted on every
      bash command), so the reviewer isn't interrupted mid-run.
    * `codex --ask-for-approval never --sandbox workspace-write`. Picking
      `never` + `workspace-write` keeps the sandbox guard while skipping
      approval prompts. `--yolo` / `--dangerously-bypass-approvals-and-sandbox`
      is rejected because it also removes the sandbox — a behaviour shift
      vs the existing Claude flow.
    * `gemini --approval-mode auto_edit`. The `auto_edit` value is the
      documented analogue of auto-approving edits while still gating
      risky operations. The older `--yolo` / `-y` flag is
      deprecated upstream in favour of `--approval-mode=yolo`.

    Only this function knows the flag surface — every other launch-path
    code path is CLI-agnostic, so flag-name churn in either upstream CLI
    is a one-function change.
    """
    if cli == "claude":
        return ["claude", "--permission-mode", "auto", prompt_text]
    if cli == "codex":
        # `-c sandbox_workspace_write.network_access=true` keeps the
        # filesystem sandbox (writes scoped to the workspace) but
        # restores network access — required so `gh api …` calls in the
        # post-inline review path can reach GitHub. Without this override
        # codex's workspace-write sandbox blocks network by default,
        # and the review silently fails to publish inline comments.
        return [
            "codex",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
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

# Appended as its own section when the workspace has a CodeGraph index
# (`.codegraph/`). CodeGraph (https://github.com/colbymchenry/codegraph) is a
# local MCP server that pre-indexes the codebase into a SQLite knowledge
# graph and exposes symbol/call-graph/impact tools to the coding-agent CLI.
# When the MCP server is wired up but the agent isn't nudged, it still
# defaults to grep+Read fan-out — the hint steers it toward direct queries
# so the sub-agents the review spawns answer from the index instead of
# re-deriving relationships. CLI-agnostic: the MCP surface is identical
# across claude/codex/gemini, and the suffix is gated only on the index
# being present in the workspace (not on the CLI in use).
CODEGRAPH_HINT_SUFFIX = (
    "This workspace has a CodeGraph index (`.codegraph/`). For "
    "symbol-relationship questions during review, prefer the CodeGraph MCP "
    "tools (`codegraph_context`, `codegraph_impact`, `codegraph_callers`, "
    "`codegraph_callees`, `codegraph_trace`, `codegraph_search`) over "
    "grep+Read loops — they answer from the pre-built index in one call "
    "instead of fanning out across the codebase. Reach for raw grep/Read "
    "only to confirm a specific detail CodeGraph didn't cover."
)

# The per-symbol blast-radius nudge, split out of CODEGRAPH_HINT_SUFFIX so
# it's appended ONLY when we didn't precompute an affected-files block. When
# the `codegraph affected` block IS present it already answers "what does the
# diff reach", so re-asking the agent to run `codegraph_impact` on every
# touched symbol is doubly wasteful: it spends prompt tokens AND provokes a
# fan-out of tool calls whose results re-bloat the agent's context — the exact
# memorization cost the index was meant to avoid. `codegraph_impact` still
# appears in the hint's tool-list parenthetical above, so the tool stays
# discoverable for ad-hoc use either way.
CODEGRAPH_IMPACT_NUDGE = (
    "Run `codegraph_impact` on each symbol touched by the diff to scope "
    "blast radius before commenting."
)

# Cap on how many affected-test paths we'll inline into the prompt. Codegraph
# can return hundreds on PRs touching widely-imported files; past ~30 the
# list stops being a useful scoping hint and starts crowding out other
# context (and the agent can always query `codegraph affected`/`codegraph_impact`
# itself for the long tail). The block annotates the overflow so the agent
# knows the list was truncated, not authoritative.
CODEGRAPH_AFFECTED_TESTS_CAP = 30

# Prompt sections are joined with this separator so multi-line blocks (e.g.
# the existing-comments list) don't get smashed into neighboring prose.
PROMPT_SECTION_SEP = "\n\n"

# Caps for the existing-comments block injected into the prompt. Bound prompt
# size on PRs with lots of prior review activity. The body cap is a dedup
# anchor, not the full comment — ~120 chars is enough to identify a finding's
# gist for the "don't repost duplicates" check, and trims up to ~40% off this
# block (the largest variable section) on heavily-reviewed PRs.
EXISTING_COMMENT_BODY_CAP = 120
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

# Auto-refresh interval cycle (seconds): off → 15m → 30m → 1h → off. The
# `a` footer key cycles through these. Same single-source-of-truth pattern
# as the group/sort cycles. `parse_refresh_interval` clamps any
# hand-edited stored value and `action_cycle_refresh` falls back to the
# first enabled step for a stored value not on the cycle. Default is 1h —
# the issue-#49 ask is to refresh the list hourly out of the box.
_DEFAULT_REFRESH_SECS = 3600
_REFRESH_CYCLE: dict[int, int] = {0: 900, 900: 1800, 1800: 3600, 3600: 0}

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

# CLIs that consume bundled `.agents/skills/` material (i.e. ones that
# need `_materialise_skills` / `_cleanup_skills` around the launch).
# Names the concept once so the materialise-site and finally-block
# gates can't drift apart — without it, adding a fourth skill-using
# CLI later means updating two literal tuples and forgetting one would
# leak files (materialise without cleanup) or skip skills entirely
# (cleanup-gate hit, materialise-gate missed). Claude has its own
# plugin marketplace and doesn't use this path.
_SKILL_BASED_CLIS: frozenset[CliChoice] = frozenset({"codex", "gemini"})

# Human-facing display names for banners, modal labels, and toast messages.
_CLI_DISPLAY: dict[CliChoice, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini",
}

# Per-CLI remediation prose printed when `.codegraph/` and the binary are
# both present but the CLI's MCP config doesn't reference codegraph.
# Codegraph's installer (`codegraph install`) supports `--target=claude`
# and `--target=codex` but NOT `--target=gemini` — gemini doesn't appear
# in the installer's target list (verified against codegraph v0.9.5,
# which errors `Unknown --target id(s): gemini. Known: claude, cursor,
# codex, opencode, hermes`). For gemini we point at the manual-setup
# path instead, otherwise a copy-paste would land users on a confusing
# installer error. Keeping the prose in a dict (rather than building it
# inline at the launch site) makes it cheap to keep accurate as
# codegraph upstream adds/removes installer targets.
_CODEGRAPH_INSTALL_HINT: dict[CliChoice, str] = {
    "claude": (
        "Run `codegraph install --target=claude --location=global --yes` "
        "to wire it up, then restart Claude Code once."
    ),
    "codex": (
        "Run `codegraph install --target=codex --location=global --yes` "
        "to wire it up, then restart Codex once."
    ),
    "gemini": (
        "CodeGraph's installer has no `--target=gemini` option — add an "
        "`mcpServers.codegraph` entry to `~/.gemini/settings.json` manually "
        "(see https://github.com/colbymchenry/codegraph#manual-setup-alternative "
        "for the canonical JSON snippet), then restart Gemini once."
    ),
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
    """True if the plugin is installed & enabled, False if not, None if undetectable.

    Callers must treat `None` distinctly from `False` — `None` means we
    couldn't determine plugin status (binary missing, subprocess hung
    past the timeout, malformed JSON), not "no". Silently treating
    `None` as `True` (or as `False`) is exactly the silent-fallback
    anti-pattern called out in CLAUDE.md.

    The 5 s timeout guards the TUI thread: this runs synchronously
    before `self.suspend()` inside `_launch_claude`, so a stalled
    `claude` (slow disk, hung child, network-mounted plugin dir, an
    unresponsive marketplace probe) would otherwise freeze the entire
    TUI indefinitely with no frame updates and no Ctrl-C handling.
    """
    try:
        r = run(["claude", "plugin", "list", "--json"], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
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
    (missing DB, corrupt row, unrecognised value, unwritable workspace)
    so a transient I/O issue can't crash startup before the TUI mounts
    — the launcher's pre-flight check surfaces a missing binary at
    review time anyway.

    Both exception classes are needed: `_open_review_db()` calls
    `Path.mkdir(parents=True, exist_ok=True)` which raises `OSError`
    on a read-only or permission-denied workspace dir; and
    `_get_setting()` raises `sqlite3.Error` on a corrupt settings table
    or an `OperationalError: database is locked past busy_timeout`. The
    original implementation caught only the connect-time `sqlite3.Error`
    and left both of these uncovered.
    """
    try:
        conn = _open_review_db()
        try:
            v = _get_setting(conn, "cli", DEFAULT_CLI)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return DEFAULT_CLI
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
    else:
        skills_dir = _skills_dir()
        missing: list[str] = []
        malformed: list[str] = []
        for name in REVIEW_SKILLS:
            skill_md = skills_dir / name / SKILL_FILE_NAME
            if not skill_md.is_file():
                missing.append(name)
                continue
            # A present-but-malformed SKILL.md (empty file from a
            # half-extracted wheel, frontmatter stripped by a bad merge)
            # would pass an `is_file()` check and only surface as a
            # "skill not found" error from codex/gemini mid-review.
            # Cheap shape check (~200 bytes per skill at startup) keeps
            # the failure in the same `problems` flow as missing files.
            try:
                head = skill_md.read_bytes()[:512].decode("utf-8", errors="replace")
            except OSError:
                malformed.append(name)
                continue
            if not head.startswith("---\n") or f"name: {name}" not in head:
                malformed.append(name)
        if missing:
            # The bundled skills are mandatory for codex/gemini and harmless
            # for claude. Verify every expected SKILL.md is on disk (not just
            # the parent dir) so a partial extraction surfaces here rather
            # than later as a "skill not found" runtime error from the CLI.
            problems.append(
                f"bundled review skills missing at {skills_dir} "
                f"(missing: {', '.join(missing)}) — reinstall cc-pr-reviewer"
            )
        if malformed:
            problems.append(
                f"bundled review skills present but malformed at {skills_dir} "
                f"(missing/incorrect frontmatter: {', '.join(malformed)}) — "
                "reinstall cc-pr-reviewer"
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


def fetch_my_latest_review(
    repo: str, number: int, login: str
) -> tuple[dict[str, Any] | None, bool]:
    """Most recent review *submitted by `login`* on PR `repo#number`.

    Returns `(review, ok)`. `ok=False` (with a printed warning) means the
    lookup couldn't be performed — empty login, non-zero exit, parse error, or
    a non-list payload; `review` is `None` in that case. `ok=True` with
    `review=None` means a clean fetch that found no submitted review by
    `login`. `ok=True` with a dict gives `id`/`state`/`submitted_at` for the
    latest submitted review (state ∈ {APPROVED, CHANGES_REQUESTED, COMMENTED};
    PENDING/DISMISSED rows are skipped as non-verdicts).

    The `ok` flag exists so a caller can distinguish "no prior review" from
    "couldn't tell" — collapsing them (as an earlier version did) let a
    transient pre-session API blip be read as "no baseline", which could then
    fire a spurious notification for a pre-existing review. Mirrors
    `fetch_existing_review_comments`'s `(value, ok)` shape.

    Single page (`per_page=100`, GitHub's max) mirrors
    `fetch_existing_review_comments` — a PR with >100 reviews by one user is
    not a case worth paginating for, and `gh api --paginate` over an array
    endpoint emits concatenated arrays that aren't valid JSON.
    """
    if not login:
        # No `gh` login means we can't identify our own reviews at all — treat
        # as "couldn't tell", not "no review", so the caller stays quiet.
        print(f"warning: no gh login to match reviews for {repo}#{number} (Slack notify skipped)")
        return None, False
    r = run(["gh", "api", f"repos/{repo}/pulls/{number}/reviews?per_page=100"])
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
        print(f"warning: couldn't fetch reviews for {repo}#{number} (Slack notify skipped): {err}")
        return None, False
    try:
        reviews = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        print(f"warning: couldn't parse reviews for {repo}#{number} (Slack notify skipped): {e}")
        return None, False
    if not isinstance(reviews, list):
        print(f"warning: unexpected reviews payload for {repo}#{number} (Slack notify skipped)")
        return None, False
    latest: dict[str, Any] | None = None
    for rv in reviews:
        if not isinstance(rv, dict):
            continue
        if (rv.get("user") or {}).get("login") != login:
            continue
        if rv.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            continue
        # Reviews come back in chronological order, so the last match wins.
        latest = {
            "id": rv.get("id"),
            "state": rv.get("state"),
            "submitted_at": rv.get("submitted_at"),
        }
    return latest, True


# Maps a GitHub review `state` to (emoji, human verdict) for the Slack message.
# Falls back to a generic line for any unexpected state so the notification is
# still sent rather than silently dropped.
_REVIEW_STATE_VERDICT: dict[str, tuple[str, str]] = {
    "APPROVED": ("✅", "approved"),
    "CHANGES_REQUESTED": ("📝", "requested changes on"),
    "COMMENTED": ("💬", "left comments on"),
}


def _slack_escape(s: str) -> str:
    """Escape the three characters Slack treats specially in mrkdwn `text`.

    Slack's incoming-webhook docs require `&`, `<`, `>` to be HTML-escaped so a
    PR title like `Fix <Foo> & bar` renders verbatim instead of being parsed as
    (or mangling) Slack markup. `&` must go first or the later replacements'
    ampersands would be double-escaped.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_payload(
    *,
    repo: str,
    number: int,
    title: str,
    url: str,
    author_login: str | None,
    reviewer_login: str | None,
    state: str,
) -> dict[str, Any]:
    """Build the Slack incoming-webhook JSON body for a completed review.

    Pure (no I/O) so the message wording is unit-testable. Posts plain text
    to a shared channel: GitHub handles are rendered as `@handle` literal
    text — NOT Slack `<@id>` mentions, which would need a login→Slack-id map
    we deliberately don't maintain (issue #51 scoped this to plain text). The
    user-controlled `title` is mrkdwn-escaped; the other fields are
    structurally constrained (logins, `owner/name`, a GitHub URL) and need no
    escaping.
    """
    emoji, verdict = _REVIEW_STATE_VERDICT.get(state, ("🔔", f"reviewed ({state.lower()})"))
    reviewer = f"@{reviewer_login}" if reviewer_login else "A reviewer"
    author = f"@{author_login}" if author_login else "the author"
    safe_title = _slack_escape(title)
    text = f"{emoji} {reviewer} {verdict} {repo}#{number}: {safe_title}\nAuthor: {author} — {url}"
    return {"text": text}


def _post_slack_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    """POST `payload` as JSON to a Slack incoming webhook. Best-effort but loud.

    A configured webhook that fails (network error, HTTP error, malformed URL)
    prints a warning and returns — the notification is a side-benefit recorded
    after the review subprocess already returned, so it must never crash the
    launch handler. Reaching this function means the caller already confirmed a
    non-empty URL, so a failure here is a real, surfaced error rather than the
    intentional off-switch (which is an empty URL the caller never posts).

    The `except (OSError, ValueError)` clause is what actually surfaces non-2xx
    responses: `urlopen` follows 3xx and raises `HTTPError` (an `OSError`
    subclass, like `URLError`) for 4xx/5xx, so the in-context status check is
    just defensive belt-and-suspenders and rarely fires. `ValueError` covers a
    malformed URL string.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            if not (200 <= resp.status < 300):
                print(f"warning: Slack webhook returned HTTP {resp.status}")
    except (OSError, ValueError) as e:
        print(f"warning: Slack notification failed: {e}")


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


def _codegraph_mcp_registered(
    cli: CliChoice,
    workspace: Path | None,
    home: Path | None = None,
) -> bool | None:
    """Detect whether the codegraph MCP server is wired into the CLI's config.

    Returns one of three states (matching the `_pr_review_toolkit_enabled`
    pattern — None is distinct from False, and callers must treat it
    that way):

    - `True`: a `codegraph` MCP entry was found in at least one of the
      per-CLI config locations probed (global user-level for every CLI;
      plus workspace-local for claude and gemini — codex has no
      project-local MCP path).
    - `False`: every config file we checked exists and confirms there
      is no codegraph entry — the user has the CLI configured but
      didn't run `codegraph install` for it (or ran it for a different
      CLI than the one currently selected via the `c` toggle).
    - `None`: undetectable — either NO config file existed at any
      checked path, OR a file existed but couldn't be parsed/read.
      Callers gating the Tier 1 prompt suffix should treat `None` the
      same as `False` (don't promise tools we can't confirm).

    Why distinguish `None` from `False`: a user who sees the warning
    benefits from prose that names the actual failure shape (no config
    vs. unreadable config), so cc-reviewer's launch banner reads
    "couldn't find or parse a {cli} config" for None and "config exists
    but lacks the codegraph entry" for False. The distinction is also
    intentionally lossy on parse-failures: with multiple candidates
    (claude/gemini both have global + workspace-local), if EARLIER
    candidates fail to parse but a LATER one validly contains the
    entry, we return `True` — under-promising on a valid setup just
    because the global file was transiently malformed would be worse
    than the warning we'd otherwise print. The `had_parse_error` flag
    only matters when no candidate returned True.

    Why this lives between cc-reviewer and the agent: cc-reviewer's
    Tier 1 prompt suffix tells the agent "use these MCP tools". If the
    MCP server isn't actually registered with the CLI we're about to
    launch, the agent reads the suffix, finds no tools, falls back to
    grep+Read, and surfaces the mismatch to the reviewer — the exact
    user-visible failure that motivated this probe. The binary-on-PATH
    check from the prior fix is a strong proxy but doesn't catch the
    case where someone installed the binary directly (e.g. `npm i -g
    @colbymchenry/codegraph`) without running `codegraph install` to
    write per-CLI MCP entries.

    Gemini caveat: codegraph's installer has no `--target=gemini`
    option (only `claude`, `cursor`, `codex`, `opencode`, `hermes` as
    of v0.9.5), so a positive `True` result for `cli == "gemini"` is
    *always* from a hand-wired `mcpServers.codegraph` entry. A
    perpetual False for gemini-only users isn't a regression — it
    reflects codegraph upstream.

    `workspace` is the PR's checked-out clone — used to probe
    project-local config files (claude `<workspace>/.mcp.json`, gemini
    `<workspace>/.gemini/settings.json`). Pass `None` to skip the
    workspace-local probes entirely — required for the startup health
    check, which runs before a PR is selected and therefore has no
    workspace to probe. `home` defaults to `Path.home()` and is
    overridable for tests so we can stand up fake config trees under
    `tmp_path` without monkeypatching `Path.home`.

    The check is best-effort: codegraph's installer writes canonical
    `mcpServers.codegraph` (JSON) or `[mcp_servers.codegraph]` (TOML)
    entries, which is exactly what we detect. A user with a
    non-standard config layout will see False here and a printed
    warning; their tools may still load fine at runtime, but
    cc-reviewer can't confirm it ahead of time.
    """
    home = home or Path.home()
    json_candidates: list[Path] = []
    toml_candidates: list[Path] = []
    if cli == "claude":
        # `~/.claude.json` is the canonical global location; `<workspace>/
        # .mcp.json` is Claude Code's project-local convention. The
        # workspace-local probe is appended only when `workspace` is
        # passed (per-launch); startup callers pass `None`.
        json_candidates = [home / ".claude.json"]
        if workspace is not None:
            json_candidates.append(workspace / ".mcp.json")
    elif cli == "codex":
        # Codex stores MCP config in `~/.codex/config.toml` (TOML format,
        # global only — codex doesn't have a project-local MCP path the
        # way Claude and Gemini do). `workspace` is unused here.
        toml_candidates = [home / ".codex" / "config.toml"]
    elif cli == "gemini":
        json_candidates = [home / ".gemini" / "settings.json"]
        if workspace is not None:
            json_candidates.append(workspace / ".gemini" / "settings.json")
    else:
        # Mirrors `_build_cli_command`'s house pattern: a future 4th
        # CliChoice member must fail loud here rather than silently
        # falling through to a wrong-but-plausible branch (e.g. probing
        # gemini's config for a new CLI). Type-checker would catch this
        # too, but a runtime raise is the belt-and-suspenders fallback.
        raise ValueError(f"unknown CLI choice: {cli!r}")

    any_file_seen = False
    had_parse_error = False
    for path in json_candidates:
        # `path.exists()`/`is_file()` reach into the filesystem and
        # re-raise `PermissionError` (EACCES is NOT in pathlib's ignored-
        # errno set) when an ancestor dir like `~`, `~/.codex`, or
        # `~/.gemini` lacks search/execute permission. Wrapping the
        # stat-level calls in the same `OSError` bucket as the `read_text`
        # below means an unreadable tree degrades to `None`/parse-error
        # honestly instead of bubbling a traceback out of `_launch_claude`
        # post-`suspend()` (or out of `on_mount` for the startup path
        # this PR adds).
        try:
            if not path.exists():
                continue
            is_regular_file = path.is_file()
        except OSError:
            had_parse_error = True
            continue
        if not is_regular_file:
            # Path slot exists but isn't a regular file (accidental
            # `mkdir ~/.claude.json`, dangling symlink, device node).
            # Bucket as parse-failure: we can't read it, so we can't
            # confirm — but distinct from "no config at all" so the
            # warning prose is accurate ("couldn't find or parse").
            had_parse_error = True
            continue
        any_file_seen = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # File exists but unreadable/corrupt. Continue to the next
            # candidate rather than returning None immediately — a
            # corrupt global config must NOT suppress a valid workspace-
            # local one, otherwise we'd under-promise on the exact
            # setup this probe exists to support.
            had_parse_error = True
            continue
        # A readable-but-structurally-malformed config (top-level not a
        # dict, or `mcpServers` present but not a dict) is "undetectable",
        # not "confirmed-not-registered": the file is unusable for our
        # purpose, so the honest state is None. Without bucketing these
        # branches into `had_parse_error`, the helper would fall through
        # to `return False`, and `_launch_claude` would print the
        # "config exists but lacks the entry — run `codegraph install`"
        # remediation, which won't fix a structurally broken config.
        # `isinstance(mcp, dict)` ALSO guards against substring-style
        # false-positives: `"codegraph" in "codegraph"` is True (char
        # membership) and `"codegraph" in ["codegraph"]` is True (list
        # membership), both of which would wrongly resolve to a True
        # registration if the dict-typed check were dropped.
        if not isinstance(data, dict):
            had_parse_error = True
            continue
        if "mcpServers" not in data:
            # Key simply absent is a valid "no entry" state, not a
            # malformed shape — falls through to the final `return False`
            # below if nothing else trips. Distinguishing absent-vs-null
            # requires the `in` check; `data.get("mcpServers")` would
            # collapse `{}` and `{"mcpServers": None}` into the same
            # `None`, hiding the malformed case from the bucket below.
            continue
        mcp = data["mcpServers"]
        if not isinstance(mcp, dict):
            # Key present but the value is malformed (string, list, null,
            # primitive). `isinstance(mcp, dict)` also guards against
            # substring-style false-positives — `"codegraph" in "codegraph"`
            # (char membership) and `"codegraph" in ["codegraph"]` (list
            # membership) are both truthy and would wrongly resolve to a
            # True registration if the dict-typed check were dropped.
            had_parse_error = True
            continue
        if "codegraph" in mcp:
            return True

    for path in toml_candidates:
        # Same stat-level guard as the JSON loop above — PermissionError
        # on an unreadable ancestor (e.g. `chmod 000 ~/.codex`) must
        # degrade cleanly to "had_parse_error" rather than crashing the
        # caller post-`suspend()` or during `on_mount`.
        try:
            if not path.exists():
                continue
            is_regular_file = path.is_file()
        except OSError:
            had_parse_error = True
            continue
        if not is_regular_file:
            had_parse_error = True
            continue
        any_file_seen = True
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            had_parse_error = True
            continue
        # Match conservatively on the canonical section header form
        # `[mcp_servers.codegraph]` — that's what `codegraph install`
        # writes. Alternative shapes (inline tables under
        # `[mcp_servers]`, quoted dotted keys) fall through to False,
        # which is the safer outcome (the user sees a warning and can
        # confirm wiring) rather than us regex-guessing past a
        # malformed match. Avoids a tomllib dependency that would bump
        # the min Python to 3.11.
        if re.search(r"^\[mcp_servers\.codegraph\]\s*$", text, re.MULTILINE):
            return True

    if had_parse_error or not any_file_seen:
        return None
    return False


CodegraphSetupState = Literal["not-installed", "wired", "binary-only"]


def _check_codegraph_setup(
    cli: CliChoice,
    home: Path | None = None,
) -> CodegraphSetupState:
    """Workspace-free startup health check for codegraph + per-CLI MCP wiring.

    Returns a coarse three-state diagnosis suitable for a per-invocation
    toast at TUI startup AND on each `c` toggle (no internal dedup —
    every call evaluates fresh, every binary-only result re-emits the
    toast). Distinct from
    `_codegraph_mcp_registered` in two ways:
      1. No workspace argument — startup runs before a PR is selected,
         so workspace-local config probes (`<workspace>/.mcp.json`,
         `<workspace>/.gemini/settings.json`) are intentionally
         skipped. A user who has only wired CodeGraph project-locally
         in a specific workspace will see "binary-only" at startup;
         the per-launch verification then catches the wired state
         once they pick a PR in that workspace.
      2. Folds "False" (config exists, no entry) and "None"
         (undetectable / unparseable) into a single `"binary-only"`
         bucket — at startup we only need to know whether MCP tools
         will be available for the active CLI, not the precise reason
         they aren't.

    States:
      - `"not-installed"`: `codegraph` binary is not on PATH. No toast
        — don't nag users who haven't installed CodeGraph.
      - `"wired"`: binary present AND global MCP entry exists for `cli`.
        No toast — the happy path.
      - `"binary-only"`: binary present but the active CLI's global
        MCP config has no codegraph entry. One actionable toast —
        the user has installed the binary but their selected CLI
        doesn't know about it, so the per-launch suffix would be
        suppressed.

    Per-launch verification stays in `_launch_claude` and remains the
    source of truth for "does this specific review have MCP tools?".
    Startup is an early-warning surface, not a replacement.
    """
    if shutil.which("codegraph") is None:
        return "not-installed"
    # `workspace=None` skips workspace-local probes; we deliberately
    # don't know the workspace at startup.
    mcp_state = _codegraph_mcp_registered(cli, workspace=None, home=home)
    return "wired" if mcp_state is True else "binary-only"


def _collect_codegraph_affected(local_path: Path, repo: str, number: int) -> list[str]:
    """Resolve the PR's base ref and pipe the diff into `codegraph affected`.

    Returns a deduplicated, stably-sorted list of paths codegraph reports
    as transitively dependent on the changed source. `codegraph
    affected`'s default `--filter` is auto-detect-tests, so this is
    *typically* a test-files list — but callers and prompt phrasing
    should treat the contract as "whatever codegraph's filter returns",
    not "test files guaranteed" (auto-detect can miss niche frameworks).
    Dedup lives here, not just in `format_codegraph_affected_tests`, so
    the user-visible count printed at the launch site matches what the
    agent actually sees in the prompt.

    Returns `[]` on any failure (missing base ref, git diff error,
    codegraph error, dropped binary, timeout) — the affected-tests block
    is a nice-to-have, so degradation is silent in the prompt; a `print`
    warning still surfaces in the suspended TUI output so a curious user
    can see why the section is absent.

    Caller must have already verified `.codegraph/` exists and
    `codegraph` is on PATH; this helper still defends against
    `shutil.which`/`Popen` racing (binary uninstalled mid-launch) via
    the `OSError` guard. `gh pr view --repo` is explicit (rather than
    relying on cwd repo-inference) to match `fetch_existing_review_comments`
    and stay robust for cross-fork PRs where `gh pr checkout` adds the
    head-repo remote.
    """
    try:
        base_r = run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "baseRefName",
                "--jq",
                ".baseRefName",
            ],
            cwd=local_path,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as e:
        print(f"warning: `gh pr view --repo {repo}` failed ({e}); skipping affected-tests block.")
        return []
    base = (base_r.stdout or "").strip()
    if base_r.returncode != 0 or not base:
        err = (base_r.stderr or base_r.stdout).strip() or f"exit {base_r.returncode}"
        print(
            f"warning: couldn't resolve PR #{number}'s base ref for codegraph affected ({err}); "
            "skipping affected-tests block."
        )
        return []

    try:
        diff_r = run(
            ["git", "diff", f"origin/{base}...HEAD", "--name-only"],
            cwd=local_path,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as e:
        print(
            f"warning: `git diff origin/{base}...HEAD` failed ({e}); skipping affected-tests block."
        )
        return []
    if diff_r.returncode != 0:
        err = (diff_r.stderr or diff_r.stdout).strip() or f"exit {diff_r.returncode}"
        print(
            f"warning: `git diff origin/{base}...HEAD` failed ({err}); "
            "skipping codegraph affected-tests block."
        )
        return []
    changed = [line for line in diff_r.stdout.splitlines() if line.strip()]
    if not changed:
        # Empty diff isn't an error — PRs can be empty mid-rebase, or the
        # base ref might be ahead. Just no block to render.
        return []

    try:
        aff = run(
            ["codegraph", "affected", "--stdin", "--quiet"],
            cwd=local_path,
            input="\n".join(changed),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as e:
        print(f"warning: `codegraph affected` failed ({e}); skipping affected-tests block.")
        return []
    if aff.returncode != 0:
        err = (aff.stderr or aff.stdout).strip() or f"exit {aff.returncode}"
        print(f"warning: `codegraph affected` failed ({err}); skipping affected-tests block.")
        return []
    # Dedup and sort here (not just in `format_codegraph_affected_tests`)
    # so any user-facing count printed at the call site matches the
    # number of bullets the prompt block will render.
    return sorted({line.strip() for line in aff.stdout.splitlines() if line.strip()})


def format_codegraph_affected_tests(paths: list[str]) -> str:
    """Render the affected-tests prompt block from `codegraph affected` output.

    Pure — `_launch_claude` does the shell-out (gh→git→codegraph) and
    feeds the resulting path list in here so the rendering decisions
    (dedup, sort, cap, header phrasing) stay unit-testable in isolation.

    Empty/whitespace paths are dropped before dedup; the input is
    deduplicated to guard against `codegraph affected` returning the
    same test twice when multiple changed files reach it. Capped at
    `CODEGRAPH_AFFECTED_TESTS_CAP` with an explicit overflow note so
    the agent knows the list was truncated, not authoritative.
    Returns `""` when no usable paths remain — the empty string sentinel
    means "skip this section" in `build_review_prompt`.
    """
    cleaned = sorted({p.strip() for p in paths if p and p.strip()})
    if not cleaned:
        return ""
    truncated = len(cleaned) > CODEGRAPH_AFFECTED_TESTS_CAP
    shown = cleaned[:CODEGRAPH_AFFECTED_TESTS_CAP]
    # Phrased as "files codegraph identifies as affected" rather than
    # asserting "test files": `codegraph affected`'s default `--filter`
    # auto-detects tests, but the auto-detect can miss niche frameworks.
    # The footer still names test-coverage as the intended scoping use
    # (that's what the filter targets), but the bullet list itself is
    # honest about provenance — if an oddball non-test slips through, the
    # agent can still reason about it.
    lines = [
        "Files identified by `codegraph affected` as transitively reached "
        "by this PR's diff (default filter auto-detects test files):"
    ]
    lines.extend(f"- {p}" for p in shown)
    if truncated:
        lines.append(
            f"(showing {CODEGRAPH_AFFECTED_TESTS_CAP} of {len(cleaned)} total — list truncated)"
        )
    lines.append(
        "Use this list to scope the test-coverage review: missing or weak "
        "coverage for any of these is a stronger signal than for tests not "
        "on this list. Tests outside this set are unlikely to exercise the "
        "diff and shouldn't drive review focus."
    )
    return "\n".join(lines)


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


def _approx_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for launch telemetry.

    Deliberately a cheap heuristic, not a real tokenizer: we never ship a
    tokenizer dependency, and the only consumer is *relative* comparison
    across launches (e.g. codegraph-present vs grep-only prompts), where a
    consistent 4-chars/token proxy is enough to spot trends. Absolute
    accuracy against any specific model's BPE is explicitly a non-goal.
    """
    return len(text) // 4


@dataclass(frozen=True)
class BuiltPrompt:
    """Result of `build_review_prompt` — prompt text plus banner metadata."""

    text: str
    rereview: bool
    existing_shown: int
    existing_total: int

    @property
    def approx_tokens(self) -> int:
        """~4-chars/token estimate of `text` for `review_telemetry`.

        A computed property rather than a stored field so it can never
        desync from `text`. A heuristic for trend-spotting, not a billing
        figure (see `_approx_tokens`).
        """
        return _approx_tokens(self.text)


def build_review_prompt(
    *,
    post_inline: bool,
    extra_prompt: str,
    existing: list[dict[str, Any]],
    fetch_ok: bool,
    my_login: str | None,
    author_login: str | None,
    cli: CliChoice = DEFAULT_CLI,
    codegraph_present: bool = False,
    codegraph_affected_tests: str = "",
    selected_agents: tuple[str, ...] | None = None,
) -> BuiltPrompt:
    """Assemble the user message for the selected coding-agent CLI. Pure — no I/O.

    Isolated from `_launch_claude` so the conditional `POST_INLINE_*`
    suffix matrix (locked by `tests/test_cc_pr_reviewer.py`) is
    unit-testable in isolation; that matrix is the most regression-prone
    part of the file.

    `cli` selects the base review prompt: `claude` uses the
    plugin-driven `REVIEW_PROMPT_CLAUDE`, while `codex` and `gemini`
    share `REVIEW_PROMPT_SKILL_BASED` (which references the six bundled
    skills by name; materialisation into `.agents/skills/` happens in
    `_launch_claude`). All `POST_INLINE_*` suffixes apply identically
    to every CLI — they're about `gh` CLI usage, not the reviewing
    agent. `cli` defaults to `"claude"` so existing callers and tests
    remain valid without churn.

    `codegraph_present` should be `True` only when the agent will
    actually have CodeGraph MCP tools available in-session — `_launch_claude`
    composes three signals: `.codegraph/` exists in the workspace, the
    `codegraph` binary is on PATH, AND the selected CLI's config
    (`~/.claude.json` / `~/.codex/config.toml` / `~/.gemini/settings.json`,
    plus workspace-local variants for claude and gemini — codex is
    global-only) contains a `codegraph` MCP entry (verified by
    `_codegraph_mcp_registered`). The kwarg name is preserved for
    back-compat, but the semantic is "tools available", not bare
    index-on-disk: a stray `.codegraph/`, an `npm i -g` install without
    `codegraph install`, or a CLI toggle to one the user hadn't wired
    up would otherwise emit a hint pointing at tools no session has
    loaded, and the agent — taking the prompt at face value — falls
    back to grep+Read while reporting that the tools weren't registered.
    Defaults to `False` so any caller that hasn't probed the workspace
    (tests, future integrations) keeps the existing prompt verbatim.

    `codegraph_affected_tests` is the pre-rendered block produced by
    `format_codegraph_affected_tests` from the output of `codegraph
    affected <changed-files>`. When non-empty, it lands after the
    CodeGraph hint (if present) and before existing-comments; the two
    CodeGraph sections are independently gated, so the hint may be off
    while the block is on (and vice versa). The unenforced combination
    is deliberate: a future caller (e.g. a CI driver that ships a
    pre-computed affected-tests block without configuring the MCP
    server) should be able to inject the list without claiming index
    presence. This intentionally diverges from the `fetch_ok`/`existing`
    invariant below — that pair has a strict producer contract
    (`fetch_existing_review_comments`), while these two are derived from
    independent shell-outs in `_launch_claude`. Empty string (the default)
    skips the section entirely — that covers both "no index" and "empty
    diff / no affected files" without callers needing to disambiguate.

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

    # Normalise the agent subset against REVIEW_SKILLS so the prompt's
    # agent enumeration order is stable regardless of the order/shape the
    # caller passed (set, list, tuple, mismatched case). `None` is the
    # back-compat default = all six.
    if selected_agents is None:
        normalised_agents: tuple[str, ...] = REVIEW_SKILLS
    else:
        wanted = set(selected_agents)
        normalised_agents = tuple(name for name in REVIEW_SKILLS if name in wanted)

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

    # Default subset → use the precomputed all-six constants (also what the
    # public `REVIEW_PROMPT_*` symbols document); any non-default subset
    # rebuilds from the same builders so the wording stays consistent.
    if normalised_agents == REVIEW_SKILLS:
        base = REVIEW_PROMPT_CLAUDE if cli == "claude" else REVIEW_PROMPT_SKILL_BASED
    elif cli == "claude":
        base = _build_claude_prompt(normalised_agents)
    else:
        base = _build_skill_based_prompt(normalised_agents)
    sections = [base]
    # Strip defensively — the current ConfirmResult dataclass already strips,
    # but `build_review_prompt` is now an API boundary and a future caller
    # (or test) passing whitespace-only `extra_prompt` would otherwise render
    # an empty "Additional instructions from reviewer:" header followed by
    # nothing.
    stripped_extra = extra_prompt.strip()
    if stripped_extra:
        sections.append(f"Additional instructions from reviewer:\n{stripped_extra}")
    # Placed AFTER the reviewer extras so a per-launch override stays adjacent
    # to the base prompt (highest visibility), but BEFORE existing-comments
    # and the post-inline output instructions — CodeGraph is workflow guidance
    # for how to gather evidence, so it belongs next to the "what to review"
    # blocks, not the "how to publish findings" block. The affected-tests
    # block follows the hint so the agent reads both CodeGraph sections
    # contiguously.
    if codegraph_present:
        hint = CODEGRAPH_HINT_SUFFIX
        if not codegraph_affected_tests:
            # No precomputed affected-files block, so keep the per-symbol
            # impact nudge. When the block IS present it already scopes the
            # blast radius, so drop the redundant nudge — saves prompt tokens
            # and avoids the agent re-deriving it via a fan-out of per-symbol
            # codegraph_impact calls whose results would re-bloat its context.
            hint = f"{hint} {CODEGRAPH_IMPACT_NUDGE}"
        sections.append(hint)
    if codegraph_affected_tests:
        sections.append(codegraph_affected_tests)
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


def new_review_pr_keys(previous: set[str], current_prs: list[dict[str, Any]]) -> set[str]:
    """PR keys among the current review-requested PRs that aren't in `previous`.

    `_mine=True` rows are excluded — only PRs actually requesting *your*
    review count as "new" for the auto-refresh notification, so flipping
    the `m` toggle can never manufacture a phantom new-PR alert. `previous`
    is the last-fetched snapshot of review-PR keys, so a PR that is closed
    and later re-requested legitimately re-notifies (it's a fresh request).
    """
    current = {_pr_key(p) for p in current_prs if not p.get("_mine")}
    return current - previous


def parse_refresh_interval(raw: str, default: int = _DEFAULT_REFRESH_SECS) -> int:
    """Parse a stored auto-refresh interval (seconds).

    Non-numeric input falls back to `default`; `0`/negative disables the
    timer (returns 0); positive values are floored at 60s so a hand-edited
    tiny value can't hammer `gh`.
    """
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return 0
    return max(v, 60)


def _refresh_interval_label(secs: int) -> str:
    """Human label for the footer/notify (`off` / `15m` / `1h`)."""
    if secs <= 0:
        return "off"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


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
    # Append-only per-launch telemetry — one row per CLI launch (success,
    # abort, AND crash), unlike `reviews` which is a durable per-PR UPSERT
    # audit record. It exists to answer cost/benefit questions about the
    # prompt we ship — e.g. "does a codegraph-present launch carry a smaller
    # prompt or run faster than a grep+Read one?" — so the codegraph token
    # thesis can be checked against data instead of guessed at.
    #
    # It captures ONLY what we control on this side of the subprocess
    # boundary: the prompt we built (size + composition) and the launch
    # outcome (exit code + wall-clock). It deliberately does NOT — and
    # cannot from here — record the agent's in-session tool calls (grep vs
    # `codegraph_*`) or its real token usage; those live in the CLI's own
    # transcript, which we never read. Data is local to this workspace DB
    # and never leaves the machine.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            pr_key TEXT NOT NULL,
            cli TEXT NOT NULL,
            codegraph_tools INTEGER NOT NULL,
            affected_paths INTEGER NOT NULL,
            existing_in_prompt INTEGER NOT NULL,
            post_inline INTEGER NOT NULL,
            rereview INTEGER NOT NULL,
            approx_prompt_tokens INTEGER NOT NULL,
            duration_seconds REAL NOT NULL,
            exit_code INTEGER NOT NULL
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


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a `Z` suffix.

    Centralises the `datetime.now(timezone.utc).isoformat().replace(...)`
    idiom used by the review/telemetry/in-progress writers so the stored
    timestamp format stays identical across all three tables.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_review(
    conn: sqlite3.Connection,
    pr_key: str,
    pr_updated_at: str,
    head_sha: str,
) -> dict[str, Any]:
    now = _utc_now_iso()
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


def _record_launch_telemetry(
    conn: sqlite3.Connection,
    *,
    pr_key: str,
    cli: CliChoice,
    codegraph_tools: bool,
    affected_paths: int,
    existing_in_prompt: int,
    post_inline: bool,
    rereview: bool,
    approx_prompt_tokens: int,
    duration_seconds: float,
    exit_code: int,
) -> None:
    """Append one best-effort row to `review_telemetry` (schema in `_open_review_db`).

    "Best-effort" means it never lets telemetry break a review: it runs after
    the CLI subprocess has already returned, so recording the outcome is
    strictly a side-benefit — a failed INSERT is caught and logged rather
    than propagated, so it can't crash the launch handler. Per the project's
    no-silent-failure rule the failure is NOT swallowed silently — a dropped
    row prints a diagnostic. On error we also roll back, because `conn` is the
    long-lived `self.review_db` shared with `_release_in_progress` (which
    writes next): leaving a half-open failed transaction could disrupt that
    follow-up write.
    """
    now = _utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO review_telemetry (
                recorded_at, pr_key, cli, codegraph_tools, affected_paths,
                existing_in_prompt, post_inline, rereview, approx_prompt_tokens,
                duration_seconds, exit_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                pr_key,
                cli,
                int(codegraph_tools),
                affected_paths,
                existing_in_prompt,
                int(post_inline),
                int(rereview),
                approx_prompt_tokens,
                duration_seconds,
                exit_code,
            ),
        )
        conn.commit()
    except sqlite3.Error as e:
        # Best-effort rollback so a failed INSERT doesn't leave a half-open
        # transaction on the shared connection for the next writer.
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        print(f"warning: failed to record launch telemetry for {pr_key}: {e}")


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
    started_at = _utc_now_iso()
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
    # Subset of REVIEW_SKILLS the reviewer wants to run for this launch.
    # Default = all six; the ConfirmScreen lets the user uncheck individual
    # agents (e.g. drop code-reviewer + pr-test-analyzer for a doc-only PR).
    # Order isn't load-bearing: `build_review_prompt` re-iterates
    # `REVIEW_SKILLS` and filters by membership so the prompt enumeration
    # is stable; `_materialise_skills` doesn't care about order (it just
    # writes one dir per name) but does reject unknown names up-front with
    # `ValueError`. Today every caller produces a canonical-order subset
    # (`ConfirmScreen._selected_agents` iterates `REVIEW_SKILLS`), but the
    # downstream contract doesn't depend on that.
    selected_agents: tuple[str, ...] = REVIEW_SKILLS

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
        # `ctrl+a` is intentionally NOT `priority=True`: TextArea binds it
        # to select-all, and a reviewer mid-typing who hits Ctrl+A almost
        # always means "select the prompt I just typed", not "flip every
        # agent off". With the binding scoped non-priority, focused
        # widgets get first refusal — TextArea consumes it for select-all,
        # and Checkbox (which doesn't bind Ctrl+A) lets it bubble up to
        # `action_toggle_all_agents`. The hint reflects this: Tab off the
        # textarea onto an agent checkbox first, then Ctrl+A flips them all.
        Binding("ctrl+a", "toggle_all_agents", "Toggle all agents"),
    ]

    def __init__(self, prompt: str, default_cli: CliChoice = DEFAULT_CLI):
        super().__init__()
        self.prompt = prompt
        self.post_inline = True
        self.cli: CliChoice = default_cli

    def compose(self) -> ComposeResult:
        hint = (
            "[b]Enter[/] / [b]Ctrl+Y[/] confirm • [b]Esc[/] / [b]Ctrl+N[/] cancel "
            "• [b]Ctrl+T[/] post-inline • [b]Ctrl+L[/] cycle CLI "
            "• [b]Tab[/]/[b]Space[/] agents • [b]Ctrl+A[/] (on agents) toggle all "
            "• [b]Shift+Enter[/] newline"
        )
        # Agent checkboxes: one per REVIEW_SKILLS entry, all checked by
        # default. Tab moves focus through them and Space toggles; bound
        # `Ctrl+A` flips them all at once. Numeric / single-letter keys
        # weren't an option because they collide with typing into the
        # auto-focused extra-prompt textarea — Checkbox widgets are the
        # idiomatic Textual way to expose per-item toggles without that
        # priority-binding conflict.
        agent_checkboxes = [
            Checkbox(
                REVIEW_SKILL_LABELS[name],
                value=True,
                id=self._agent_checkbox_id(name),
                classes="confirm-agent",
            )
            for name in REVIEW_SKILLS
        ]
        yield Vertical(
            Label(self.prompt, id="confirm-title", markup=False),
            Label(self._checkbox_text(), id="confirm-checkbox", markup=False),
            Label("Review agents:", id="confirm-agents-label"),
            *agent_checkboxes,
            Label(self._cli_text(), id="confirm-cli", markup=False),
            Label("Extra prompt (optional):", id="confirm-extra-label"),
            ExtraPromptTextArea(id="confirm-extra"),
            Label(hint, id="confirm-hint"),
            id="confirm-container",
        )

    @staticmethod
    def _agent_checkbox_id(name: str) -> str:
        # Prefix to namespace these ids alongside the modal's other named
        # widgets (`confirm-title`, `confirm-cli`, `confirm-extra`, …). The
        # raw `REVIEW_SKILLS` name is already a valid Textual id on its own
        # (letters / digits / `-` / `_`, no leading digit), but pairing it
        # with a sibling `confirm-*` cohort under a single `agent-*` prefix
        # keeps `query_one("#agent-…")` lookups unambiguous and makes the
        # ids self-describing in DOM dumps.
        return f"agent-{name}"

    def _checkbox_text(self) -> str:
        mark = "[x]" if self.post_inline else "[ ]"
        return f"{mark} Post findings as inline PR comments"

    def _cli_text(self) -> str:
        return f"CLI: {_CLI_DISPLAY[self.cli]}"

    def _selected_agents(self) -> tuple[str, ...]:
        """Read current Checkbox state into a stable REVIEW_SKILLS-ordered tuple."""
        return tuple(
            name
            for name in REVIEW_SKILLS
            if self.query_one(f"#{self._agent_checkbox_id(name)}", Checkbox).value
        )

    def action_confirm(self) -> None:
        selected = self._selected_agents()
        if not selected:
            # Confirming with no agents would degrade to a generic "review
            # the PR" prompt — possibly fine, but more likely the user
            # ticked them all off by accident. Refuse to dismiss and nudge
            # them; if they really want a no-agent review, the prompts
            # builder handles that branch and the user can opt back via
            # Ctrl+A.
            self.app.notify(
                "Select at least one review agent "
                "(Tab onto a checkbox and press Ctrl+A to re-enable all).",
                severity="warning",
                timeout=4,
            )
            return
        text = self.query_one("#confirm-extra", TextArea).text
        self.dismiss(
            ConfirmResult(
                post_inline=self.post_inline,
                extra_prompt=text,
                cli=self.cli,
                selected_agents=selected,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_post_inline(self) -> None:
        self.post_inline = not self.post_inline
        self.query_one("#confirm-checkbox", Label).update(self._checkbox_text())

    def action_cycle_cli(self) -> None:
        self.cli = _CLI_CYCLE[self.cli]
        self.query_one("#confirm-cli", Label).update(self._cli_text())

    def action_toggle_all_agents(self) -> None:
        # If any checkbox is off, turn everything on (the "I changed my
        # mind, give me everything" recovery); otherwise turn everything
        # off (the "quick wipe before re-selecting just one or two"
        # shortcut). One key handles both directions because guessing
        # intent from a partial state is what the user just did with
        # Tab+Space — we don't need to second-guess it.
        checkboxes = [
            self.query_one(f"#{self._agent_checkbox_id(name)}", Checkbox) for name in REVIEW_SKILLS
        ]
        target = any(not cb.value for cb in checkboxes)
        for cb in checkboxes:
            cb.value = target


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


@dataclass(frozen=True)
class SettingsResult:
    """Outcome of a saved SettingsScreen — distinct from cancel (None).

    A dataclass rather than a bare string so the screen can grow more
    settings without changing its dismiss contract. `slack_webhook_url` is
    normalised to a stripped string; empty means "notifications off".
    """

    slack_webhook_url: str


class SettingsScreen(ModalScreen[SettingsResult | None]):
    """Edit persisted app settings.

    Currently a single field: the Slack incoming-webhook URL used to announce
    completed reviews to a shared channel. Dismisses with a SettingsResult on
    save (Enter) or None on cancel (Esc).
    """

    AUTO_FOCUS = "#settings-slack"

    BINDINGS = [
        # `priority=True` so Enter/Esc win over the focused Input widget,
        # which would otherwise consume Enter (Input.Submitted) before our
        # save action runs.
        Binding("enter", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, slack_webhook_url: str):
        super().__init__()
        self.slack_webhook_url = slack_webhook_url

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Settings", id="settings-title"),
            Label(
                "Slack webhook URL for review notifications (blank = off):",
                id="settings-slack-label",
            ),
            Input(
                value=self.slack_webhook_url,
                placeholder="https://hooks.slack.com/services/…",
                id="settings-slack",
            ),
            Label("[b]Enter[/] save • [b]Esc[/] cancel", id="settings-hint"),
            id="settings-container",
        )

    def action_save(self) -> None:
        value = self.query_one("#settings-slack", Input).value.strip()
        self.dismiss(SettingsResult(slack_webhook_url=value))

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    #confirm-container, #filter-container, #inprogress-container,
    #settings-container {
        border: round $primary;
        padding: 1 2;
        margin: 4 8;
        background: $panel;
        height: auto;
    }
    #inprogress-container {
        border: round $warning;
    }
    #confirm-title, #filter-title, #inprogress-title, #settings-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #settings-slack-label {
        color: $text-muted;
    }
    #settings-hint {
        margin-top: 1;
        color: $text-muted;
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
    #confirm-agents-label {
        margin-top: 1;
        color: $text-muted;
    }
    .confirm-agent {
        height: 1;
        padding: 0 0 0 2;
        background: transparent;
        border: none;
    }
    .confirm-agent:focus {
        background: $boost;
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
        Binding("x", "toggle_codegraph", "CodeGraph"),
        Binding("a", "cycle_refresh", "Auto-refresh"),
        Binding("comma", "settings", "Settings"),
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
        # When on AND `.codegraph/` is missing in the workspace, `_launch_claude`
        # prompts the user once per launch to run `codegraph init --index`
        # before handing off to the agent. When off (default), missing
        # `.codegraph/` is silent — Tier 1's prompt suffix and Tier 2's sync
        # both gate on existence regardless. Lives in `settings` so a user
        # who flips it on stays in helper mode across sessions.
        self.codegraph_assist: bool = _get_setting(self.review_db, "codegraph_assist", "0") == "1"
        # Slack incoming-webhook URL for review-complete notifications. Empty
        # means the feature is off — `_launch_claude` skips all Slack work
        # (no extra `gh api` calls, no POST) when this is blank. Editable via
        # the Settings modal (`,`).
        self.slack_webhook_url: str = _get_setting(self.review_db, "slack_webhook_url", "")
        stored_cli = _get_setting(self.review_db, "cli", DEFAULT_CLI)
        persisted: CliChoice = stored_cli if stored_cli in _CLI_CYCLE else DEFAULT_CLI
        # If the persisted CLI isn't on PATH, fall back to whatever IS
        # installed (in cycle order from the persisted preference) so a
        # Codex-only user on a fresh install doesn't get stuck with the
        # `claude` default. The fallback is session-only — pressing `c`
        # is what persists a new choice. `check_prereqs` already
        # guaranteed at least one CLI is available, but a TOCTOU race
        # (every CLI uninstalled between prereq check and __init__) is
        # surfaced as `_no_cli_available=True` so `on_mount` can warn
        # rather than silently leaving the user with a broken `self.cli`
        # whose failure only appears at first Enter-to-review.
        self._cli_fallback_from: CliChoice | None = None
        self._no_cli_available: bool = False
        if shutil.which(persisted) is None:
            fallback = _first_available_cli(persisted)
            if fallback is None:
                # No supported CLI on PATH at all. Keep `self.cli` as
                # the persisted preference so the footer/status reflect
                # what the user *wanted*; on_mount fires a high-severity
                # toast and pre-flight in `_launch_claude` re-checks.
                self.cli: CliChoice = persisted
                self._no_cli_available = True
            else:
                self.cli = fallback
                if fallback != persisted:
                    self._cli_fallback_from = persisted
        else:
            self.cli = persisted
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
        # --- auto-refresh (issue #49) ---
        # Periodically refetch the PR list; on a tick that surfaces
        # review-requested PRs not seen before, notify the user.
        self._auto_refresh_secs: int = parse_refresh_interval(
            _get_setting(self.review_db, "refresh_interval", str(_DEFAULT_REFRESH_SECS))
        )
        self._auto_refresh_timer: Timer | None = None
        # Cumulative set of review-PR keys already shown, so a steady-state
        # tick doesn't re-notify and only genuinely-new PRs fire a toast.
        self._seen_review_keys: set[str] = set()
        # Suppress the new-PR notification on the very first populate — every
        # PR would otherwise look "new". Set True at the end of `_populate`.
        self._first_load_done: bool = False
        # Set around `_launch_claude`'s suspend window so an auto-refresh
        # tick doesn't rebuild the table while the agent owns the TTY.
        self._suspended_for_review: bool = False

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
        # Surface CLI-availability problems set in `__init__`. Two
        # distinct cases:
        #   * `_no_cli_available`: TOCTOU race — every supported CLI was
        #     uninstalled between `check_prereqs` and now. Show a
        #     persistent error toast so the user isn't blindsided by
        #     the pre-flight failure when they hit Enter.
        #   * `_cli_fallback_from`: persisted CLI is missing but
        #     another one is on PATH. Warning toast — the TUI still
        #     works, just not on the CLI they wanted.
        if self._no_cli_available:
            self.notify(
                "no review CLI on PATH — install one of `claude`, "
                "`codex`, or `gemini` before pressing Enter on a PR. "
                "(`check_prereqs` accepted startup; the binary disappeared "
                "between then and now.)",
                severity="error",
                timeout=15,
            )
        elif self._cli_fallback_from is not None:
            self.notify(
                f"`{self._cli_fallback_from}` not on PATH — "
                f"using {_CLI_DISPLAY[self.cli]} this session. "
                "Press `c` to switch, or install the missing CLI.",
                severity="warning",
                timeout=10,
            )
        # CodeGraph health check at startup, also re-run on each `c`
        # toggle (see `action_toggle_cli`). Early-warning when the binary
        # is installed but the active CLI isn't wired up; silent in the
        # other two states — "not-installed" is the default for users
        # who don't care, and "wired" doesn't need a toast. Per-launch
        # verification (which also probes the workspace-local config
        # and the `.codegraph/` index) is the source of truth and stays
        # untouched; this is an early-warning surface only.
        self._maybe_notify_codegraph_setup()
        self.action_refresh()
        # Cross-instance "in review" indicator. 3 s is fast enough to feel
        # live (another tab finishing/starting a review is reflected
        # within one tick) and cheap enough to be invisible — one small
        # SELECT plus a bounded scan over `_row_to_pr_idx` per tick.
        self.set_interval(3.0, self._poll_in_progress)
        # Periodic auto-refresh (issue #49). No-op when disabled (interval 0).
        self._start_auto_refresh_timer()
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
        self._set_footer_active("x", self.codegraph_assist)
        self._set_footer_active("a", self._auto_refresh_secs > 0)

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
    def _load_prs(self, auto: bool = False) -> None:
        repo = self.repo_filter
        try:
            data = fetch_review_prs(repo)
        except Exception as e:  # noqa: BLE001
            # A background auto-refresh tick fails silently: keep the last
            # good list, status, and count rather than clobbering the view
            # with an error the user didn't ask for. The next tick (or a
            # manual `r`) recovers.
            if auto:
                return
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
        # An auto tick populates quietly (suppress incidental warning
        # re-toasts) but still fires the explicit new-PR notification and
        # preserves the cursor — both keyed off `auto` in `_populate`.
        self.call_from_thread(self._populate, data, mine_error, mine_warning, quiet=auto, auto=auto)

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
        auto: bool = False,
    ) -> None:
        # Capture the cursor's PR identity before the rebuild so an auto
        # refresh (a background tick the user didn't trigger) doesn't park
        # the cursor back at row 0 mid-navigation. Read while `self.prs` /
        # `_row_to_pr_idx` still hold the OLD table state.
        prev_cursor_key = self._cursor_pr_key() if auto else None
        # New-PR detection + notification gating (issue #49). Extracted into
        # `_maybe_notify_new_prs` so the gating predicate is unit-testable
        # without driving the full widget-heavy `_populate`.
        self._maybe_notify_new_prs(data, auto)
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
        # Restore the cursor onto the PR it was on before an auto rebuild.
        # A no-op if the PR is gone (merged/closed) — cursor stays at the
        # default top row.
        if prev_cursor_key is not None:
            self._move_cursor_to_pr(prev_cursor_key)

    def _maybe_notify_new_prs(self, data: list[dict[str, Any]], auto: bool) -> bool:
        """Diff `data`'s review-requested PRs against the last-fetched
        snapshot, toast + bell for any genuinely-new ones, then refresh the
        snapshot. Notifies only on an auto tick after the first load — so
        manual `r`, the `m`/filter toggles, and the very first load all
        re-baseline silently (the first load would otherwise flag every PR
        as new). Returns whether a notification fired. No widget access, so
        it's exercisable with a light fake in tests.
        """
        new_keys = new_review_pr_keys(self._seen_review_keys, data)
        fire = auto and self._first_load_done and bool(new_keys)
        if fire:
            n = len(new_keys)
            self.notify(
                f"{n} new PR{'s' if n != 1 else ''} awaiting your review",
                title="New review requests",
                timeout=10,
            )
            self.bell()
        self._seen_review_keys = {_pr_key(p) for p in data if not p.get("_mine")}
        self._first_load_done = True
        return fire

    def _cursor_pr_key(self) -> tuple[str, int] | None:
        """Identity (repo, number) of the PR under the cursor, or None when
        the cursor is on a group header or the table is empty. Reads the
        current `self.prs` / `_row_to_pr_idx`, so call it BEFORE a rebuild."""
        table = self.query_one("#pr-table", DataTable)
        # `cursor_row` can be None even with rows present (see `_selected`),
        # so guard it explicitly rather than via `or 0` — the latter would
        # pass the bounds check and then `_row_to_pr_idx[None]` would raise.
        cursor_row = table.cursor_row
        if cursor_row is not None and 0 <= cursor_row < len(self._row_to_pr_idx):
            idx = self._row_to_pr_idx[cursor_row]
            if idx is not None:
                p = self.prs[idx]
                return (p["repository"]["nameWithOwner"], p["number"])
        return None

    def _move_cursor_to_pr(self, prev_key: tuple[str, int]) -> None:
        """Seek the cursor to the row whose PR matches `prev_key`, if present."""
        table = self.query_one("#pr-table", DataTable)
        for row, idx in enumerate(self._row_to_pr_idx):
            if idx is None:
                continue
            p = self.prs[idx]
            if (p["repository"]["nameWithOwner"], p["number"]) == prev_key:
                table.move_cursor(row=row)
                break

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
                        selected_agents=result.selected_agents,
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
        # Re-emit the CodeGraph health toast for the new CLI: a user
        # who has codegraph wired for claude but not codex would
        # otherwise toggle to codex with no warning and only learn at
        # launch time that MCP tools won't load. The `_maybe_` guard
        # suppresses the toast for "not-installed" / "wired" so the
        # common toggle case is still quiet.
        self._maybe_notify_codegraph_setup()

    def action_cycle_refresh(self) -> None:
        # Cycle the auto-refresh interval (off → 15m → 30m → 1h → off),
        # persist it, rebuild the timer, and update the footer indicator.
        # `.get` fallback covers a hand-edited stored value not on the
        # cycle (e.g. a clamped 120s) — land on the first enabled step.
        nxt = _REFRESH_CYCLE.get(self._auto_refresh_secs, _REFRESH_CYCLE[0])
        self._auto_refresh_secs = nxt
        try:
            _set_setting(self.review_db, "refresh_interval", str(nxt))
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist auto-refresh toggle: {e}", severity="warning")
        self.notify(f"Auto-refresh: {_refresh_interval_label(nxt)}", timeout=3)
        self._refresh_footer_indicators()
        self._start_auto_refresh_timer()

    def _start_auto_refresh_timer(self) -> None:
        """(Re)create the auto-refresh timer from `self._auto_refresh_secs`.

        Stops any existing timer first so cycling the interval doesn't leak
        overlapping timers; a non-positive interval leaves it disabled.
        """
        if self._auto_refresh_timer is not None:
            self._auto_refresh_timer.stop()
            self._auto_refresh_timer = None
        if self._auto_refresh_secs > 0:
            self._auto_refresh_timer = self.set_interval(
                self._auto_refresh_secs, self._auto_refresh_tick
            )

    def _auto_refresh_tick(self) -> None:
        # Skip while a review has the TUI suspended — the agent owns the
        # TTY, and a background rebuild would be invisible and could race
        # the post-review refresh. The next tick after the user returns
        # picks it up.
        if self._suspended_for_review:
            return
        # Skip while a modal (confirm / filter / diff) is on top so an auto
        # rebuild doesn't yank the cursor out from under a dialog. Manual
        # `r` refresh stays available regardless.
        if self.screen is not self.screen_stack[0]:
            return
        # Skip if a `_load_prs` is already in flight. It shares the
        # `exclusive=True` group, so spawning here would CANCEL that worker
        # — and `action_refresh` sets its "Refreshing…"/"…" placeholders
        # *before* spawning, so cancelling a manual refresh whose auto
        # replacement then fails (`auto=True` returns silently) would strand
        # the UI on those placeholders until the next tick. Skipping is
        # safe: the running load already refreshes the list. (`is_finished`
        # is True for SUCCESS/ERROR/CANCELLED, so `not is_finished` matches
        # only PENDING/RUNNING.)
        if any(w.name == "_load_prs" and not w.is_finished for w in self.workers):
            return
        # `auto=True` keeps the fetch silent (no status churn / error toast)
        # and drives the new-PR notification + cursor preservation in
        # `_populate`.
        self._load_prs(auto=True)

    def _maybe_notify_codegraph_setup(self) -> None:
        """Per-invocation: surface a warning if CodeGraph is installed but
        the active CLI isn't wired up. Silent for `not-installed` (don't
        nag non-users) and `wired` (happy path). Called once from
        `on_mount` and re-emitted on each `action_toggle_cli` — every CLI
        change surfaces the gap for the new selection. No internal dedup
        guard; the user toggling cycle → wired → binary-only → wired is
        meant to fire and clear repeatedly. The per-workspace deep check
        in `_launch_claude` remains the source of truth.
        """
        # Best-effort early-warning: never let a probe failure escape into
        # `on_mount` (no surrounding try) or `action_toggle_cli` (whose
        # only try guards `_set_setting`). The root fix in
        # `_codegraph_mcp_registered` already buckets `OSError` into the
        # parse-error path, but a future refactor could reintroduce a
        # raise path; degrading silently here is the cheap belt-and-
        # suspenders so a misconfigured filesystem can never crash the
        # TUI startup or a keypress.
        try:
            state = _check_codegraph_setup(self.cli)
        except Exception:  # noqa: BLE001
            return
        # Positive match — silent in the other two states. Phrased
        # positively (rather than `if state != "binary-only": return`)
        # so a future 4th `CodegraphSetupState` member defaults to
        # silent rather than mistakenly nagging the user.
        if state == "binary-only":
            self.notify(
                f"CodeGraph binary on PATH but not registered for "
                f"{_CLI_DISPLAY[self.cli]}. {_CODEGRAPH_INSTALL_HINT[self.cli]} "
                f"Until then, the MCP-tools prompt hint stays off for "
                f"{_CLI_DISPLAY[self.cli]} reviews.",
                severity="warning",
                timeout=12,
            )

    def action_toggle_codegraph(self) -> None:
        # Render-free like `action_toggle_cli` — the table doesn't depend
        # on this toggle. `_launch_claude` reads `self.codegraph_assist`
        # at launch time to decide whether to prompt the user about
        # initialising CodeGraph in workspaces that don't have an index.
        self.codegraph_assist = not self.codegraph_assist
        state = "on" if self.codegraph_assist else "off"
        try:
            _set_setting(
                self.review_db,
                "codegraph_assist",
                "1" if self.codegraph_assist else "0",
            )
        except sqlite3.Error as e:
            self.notify(f"Couldn't persist CodeGraph toggle: {e}", severity="warning")
        self.notify(f"CodeGraph assist: {state}", timeout=3)
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

    def action_settings(self) -> None:
        def _apply(result: SettingsResult | None) -> None:
            if result is None:
                return
            # Persist first so session and disk can't diverge on a write
            # failure; keep the prior value and warn if it fails.
            try:
                _set_setting(self.review_db, "slack_webhook_url", result.slack_webhook_url)
            except sqlite3.Error as e:
                self.notify(f"Couldn't save settings: {e}", severity="warning")
                return
            self.slack_webhook_url = result.slack_webhook_url
            state = "on" if result.slack_webhook_url else "off"
            self.notify(f"Slack review notifications {state}.", timeout=3)

        self.push_screen(SettingsScreen(self.slack_webhook_url), _apply)

    def _notify_review_to_slack(
        self,
        *,
        webhook_url: str,
        pr: dict[str, Any],
        repo: str,
        number: int,
        reviewer_login: str | None,
        pre_review: dict[str, Any] | None,
        pre_review_ok: bool,
    ) -> None:
        """Announce a just-completed review to the configured Slack channel.

        Called only on a clean exit with a webhook configured. Re-queries the
        PR's reviews and notifies only when a *new* review (an id different
        from the pre-session baseline) was submitted by the reviewer this
        session — so quitting the agent without submitting anything, or a
        re-review that added no new verdict, stays quiet. The whole path is
        best-effort/loud: every step that can fail prints a warning and
        returns without disturbing the launch flow.

        `pre_review_ok` is the baseline's reliability flag from the pre-session
        `fetch_my_latest_review`. When it's False the baseline couldn't be
        established (a transient API blip or unknown login), so we can't prove
        a review is *new* — we stay quiet rather than risk announcing a
        pre-existing review. Worst case is one missed notification after a
        transient failure, which is the safer direction than a false "reviewed".
        """
        if not pre_review_ok:
            return
        post, post_ok = fetch_my_latest_review(repo, number, reviewer_login or "")
        if not post_ok or post is None:
            # No review submitted by us this session, or the lookup failed
            # (already warned). Either way there's nothing to announce.
            return
        if pre_review is not None:
            if post.get("id") == pre_review.get("id"):
                # Same review that existed before the session — no new verdict.
                return
            # Guard the dismiss edge: `fetch_my_latest_review` hides DISMISSED
            # rows, so dismissing this session's newest verdict can make an
            # *older* surviving review become "latest" with a different id —
            # which the id check alone would mistake for a new submission. A
            # genuinely new review must also be newer in time. (ISO-8601 `Z`
            # timestamps compare correctly as plain strings.)
            pre_ts, post_ts = pre_review.get("submitted_at"), post.get("submitted_at")
            if pre_ts and post_ts and post_ts <= pre_ts:
                return
        payload = build_slack_payload(
            repo=repo,
            number=number,
            title=pr.get("title", ""),
            url=pr.get("url", ""),
            author_login=(pr.get("author") or {}).get("login"),
            reviewer_login=reviewer_login,
            state=post.get("state", ""),
        )
        _post_slack_webhook(webhook_url, payload)

    # --- launching claude ---

    def _launch_claude(
        self,
        pr: dict[str, Any],
        post_inline: bool,
        extra_prompt: str,
        cli: CliChoice = DEFAULT_CLI,
        selected_agents: tuple[str, ...] = REVIEW_SKILLS,
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
        # Gemini load the bundled skills from `.agents/skills/` instead
        # (materialised below per launch), so no extra check applies to
        # them. The plugin check is the slow one — it shells out to
        # `claude plugin list --json` — so it stays gated behind the
        # binary check above. `None` (undetectable) is surfaced
        # separately so a hung/crashed `claude plugin list` doesn't
        # silently pass-through into a review where the prompt refers
        # to agents the plugin can't load.
        if cli == "claude":
            plugin_state = _pr_review_toolkit_enabled()
            if plugin_state is False:
                self.notify(
                    "PR Review Toolkit plugin not enabled — run "
                    f"`claude plugin install {PR_REVIEW_TOOLKIT_PLUGIN}` "
                    "or pick a different CLI",
                    severity="error",
                    timeout=10,
                )
                return
            if plugin_state is None:
                self.notify(
                    "couldn't determine PR Review Toolkit plugin status "
                    "(`claude plugin list --json` failed or timed out) — "
                    "proceeding; if the review can't reach the toolkit "
                    "agents, run that command manually to diagnose",
                    severity="warning",
                    timeout=10,
                )

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

            # Captured here so the `finally` block can call `_cleanup_skills`
            # only if materialisation actually completed — early-return paths
            # below (clone-fail, checkout-fail) leave it `None` and skip the
            # restore.
            skills_manifest: _MaterialisedSkills | None = None
            try:
                # Pause auto-refresh for the suspended session so a background
                # tick can't rebuild the table while the agent owns the TTY.
                # Set as the FIRST statement of this try so the `finally`
                # below unconditionally clears it — keeping the flag scoped
                # to a finally-guarded block is what prevents a permanent
                # leak (a `mkdir`/`reserve` failure above never sets it, so
                # auto-refresh can't silently die for the session).
                self._suspended_for_review = True
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

                # Hoist the `codegraph` PATH probe once: the Tier 2/3/4
                # blocks below all share this gate, and three separate
                # `shutil.which` calls would also create a TOCTOU surface
                # where the binary could disappear between probes. The
                # `subprocess.call`/`run()` wrappers below still defend
                # against the probe-vs-launch race (binary uninstalled
                # mid-launch) via `OSError` guards.
                codegraph_on_path = shutil.which("codegraph") is not None
                codegraph_present = (local_path / ".codegraph").is_dir()

                # Tier 3: opt-in init helper. When `self.codegraph_assist`
                # is on (`x` toggle) AND the workspace doesn't yet have an
                # index AND the `codegraph` binary is on PATH, prompt the
                # user during the suspended terminal session to bootstrap
                # one. We never auto-init — `codegraph init --index` can
                # take ~30s on a large repo and adds files to the workspace,
                # so it's a conscious user step. The toggle's only purpose
                # is to make that step opt-in rather than nag-on-every-launch.
                if not codegraph_present and self.codegraph_assist and codegraph_on_path:
                    print(
                        f"\nNo CodeGraph index in {local_path}. "
                        "`codegraph init --index` takes ~30s on first run, then "
                        "amortises across every future review in this workspace."
                    )
                    # Match the EOFError pattern established at the upgrade
                    # path: non-interactive / piped stdin must not crash
                    # the launch; an EOF is a "no" and we move on.
                    answer = "n"
                    with contextlib.suppress(EOFError):
                        answer = input("Initialize CodeGraph now? [y/N]: ").strip().lower()
                    if answer in ("y", "yes"):
                        print("Running `codegraph init --index`…")
                        init_rc = 1
                        try:
                            init_rc = subprocess.call(
                                ["codegraph", "init", str(local_path), "--index"]
                            )
                        except OSError as e:
                            # Mirrors the `uv tool upgrade` path: ENOENT/
                            # permission errors from `subprocess.call`
                            # raise rather than returning non-zero, and
                            # an uncaught OSError post-`suspend()` drops
                            # the user into a half-restored terminal.
                            print(f"\nFailed to launch `codegraph init`: {e}")
                        if init_rc == 0:
                            # Re-probe — the sync block below now becomes a
                            # no-op (the just-built index is current), and
                            # `build_review_prompt` gets the right flag.
                            codegraph_present = (local_path / ".codegraph").is_dir()
                        else:
                            print(
                                f"warning: `codegraph init --index` exited with status {init_rc}; "
                                "continuing without an index."
                            )

                # If the user has wired CodeGraph into this workspace
                # (`codegraph init` writes `.codegraph/`), refresh the
                # index incrementally for the freshly-checked-out PR
                # branch before launching. CodeGraph's MCP server has its
                # own file watcher, but it only runs while the CLI is
                # alive — at this point the CLI hasn't started yet, so
                # the index can be stale (especially after `gh pr
                # checkout --force`, which can rewrite many files at
                # once). `codegraph sync` is incremental and idempotent;
                # no-op when the index is already current.
                if codegraph_present and codegraph_on_path:
                    print("Syncing CodeGraph index for the checked-out branch…")
                    sync_rc = 1
                    try:
                        sync_rc = subprocess.call(["codegraph", "sync", str(local_path)])
                    except OSError as e:
                        print(f"warning: failed to launch `codegraph sync`: {e}")
                    if sync_rc != 0:
                        # Sync failure is non-fatal: the agent can
                        # still launch, MCP queries just hit a stale
                        # index. Loud so the user knows their answers
                        # may lag the branch.
                        print(
                            f"warning: `codegraph sync` exited with status {sync_rc} — "
                            "agent will see a possibly stale CodeGraph index."
                        )
                elif codegraph_present:
                    print(
                        "warning: `.codegraph/` exists but `codegraph` binary not on PATH — "
                        "skipping index sync; MCP queries (if wired) will hit a stale index."
                    )

                # Tier 4: query codegraph for tests whose imports transitively
                # reach this PR's changed files, and inline the list into the
                # prompt as a scoping hint for the test-coverage review.
                # Runs AFTER the sync block so the index reflects the
                # checked-out branch. Gated on the same (index-present +
                # binary-on-PATH) precondition as sync — without both, the
                # affected-tests query can't run.
                codegraph_affected_block = ""
                affected_count = 0  # paths actually injected into the prompt; for telemetry
                if codegraph_present and codegraph_on_path:
                    print("Querying CodeGraph for tests affected by this PR's diff…")
                    affected = _collect_codegraph_affected(local_path, repo_full, number)
                    codegraph_affected_block = format_codegraph_affected_tests(affected)
                    # Record what the agent actually saw, not the raw query
                    # count: `format_codegraph_affected_tests` caps the block at
                    # CODEGRAPH_AFFECTED_TESTS_CAP, so storing the uncapped
                    # `len(affected)` would let `affected_paths` keep climbing
                    # while `approx_prompt_tokens` plateaus — a spurious
                    # decorrelation in the very cost analysis this column feeds.
                    affected_count = min(len(affected), CODEGRAPH_AFFECTED_TESTS_CAP)
                    if codegraph_affected_block:
                        # Visible count: `_collect_codegraph_affected` now
                        # returns dedup'd output, so `len(affected)` matches
                        # the number of bullets the agent sees in the
                        # prompt block — no pre/post-dedup mismatch.
                        print(f"CodeGraph reports {len(affected)} affected file(s) for the diff.")

                # Materialise the bundled review skills into the PR
                # workspace so the skill-based CLIs (see _SKILL_BASED_CLIS)
                # discover them at session start. Done AFTER `gh pr
                # checkout --force` (which would otherwise reset our
                # writes). The returned manifest snapshots any pre-existing
                # SKILL.md bytes so `_cleanup_skills` can restore them
                # exactly even if the reviewed PR ships its own colliding
                # skill of the same name. Cleanup runs in `finally` so
                # Ctrl-C, crashes, and clean exits all leave the workspace
                # byte-identical to its pre-launch state.
                if cli in _SKILL_BASED_CLIS:
                    print(f"Materialising review skills under {local_path}/.agents/skills/…")
                    skills_manifest = _materialise_skills(local_path, selected=selected_agents)

                print("Fetching existing review comments…")
                existing, fetch_ok = fetch_existing_review_comments(repo_full, number)

                # Capture once so the banner can disambiguate a structural
                # `rereview=False` (no prior comment from us) from a missing-data
                # `rereview=False` (login lookup failed, bar-raise silently dropped).
                # Without this seam the user can't tell the two apart, and the
                # `_current_gh_login` warning may have scrolled past during clone
                # or checkout output.
                my_login = _current_gh_login()

                # Slack review-notification baseline: when a webhook is
                # configured, snapshot our latest existing review (id +
                # timestamp) BEFORE handing off so we can tell afterwards
                # whether THIS session actually submitted a new review (vs a
                # no-op exit or a re-review that changed nothing). Skipped
                # entirely when no webhook is set, so the off case costs no
                # extra `gh api` call.
                slack_webhook = self.slack_webhook_url.strip()
                pre_review: dict[str, Any] | None = None
                pre_review_ok = False
                if slack_webhook:
                    pre_review, pre_review_ok = fetch_my_latest_review(
                        repo_full, number, my_login or ""
                    )

                # Compose every signal the agent's session actually needs
                # before promising MCP tools in the prompt suffix:
                #   1. `.codegraph/` index exists in the workspace
                #   2. `codegraph` binary is on PATH (server can spawn)
                #   3. The selected CLI's config has a `codegraph` MCP entry
                # Skipping (3) was the root cause of the original
                # "tools weren't registered in this session" report — the
                # binary-on-PATH check is a strong proxy but doesn't catch
                # `npm i -g` installs that never ran `codegraph install` to
                # write per-CLI MCP entries, nor the case where the user
                # switched CLIs via the `c` toggle to one they hadn't
                # wired up. We only probe the MCP config when (1) and (2)
                # already hold — otherwise the warning would be noise for
                # users who don't have CodeGraph at all.
                codegraph_tools_available = False
                if codegraph_present and codegraph_on_path:
                    mcp_state = _codegraph_mcp_registered(cli, local_path)
                    codegraph_tools_available = mcp_state is True
                    install_hint = _CODEGRAPH_INSTALL_HINT[cli]
                    if mcp_state is False:
                        print(
                            f"warning: codegraph MCP server is not registered for "
                            f"{_CLI_DISPLAY[cli]} (no `codegraph` entry under "
                            f"`mcpServers` / `[mcp_servers.codegraph]` in its config). "
                            f"Skipping the MCP-tools prompt hint — the agent's session "
                            f"won't have codegraph tools loaded. {install_hint}"
                        )
                    elif mcp_state is None:
                        # `mcp_state == None` means either NO config file
                        # at any standard location OR a file existed but
                        # we couldn't parse it (corrupt JSON, unreadable
                        # TOML, permission error, non-regular file at the
                        # config path). Phrasing it as "couldn't find or
                        # parse" covers both cases honestly — earlier
                        # prose said "couldn't find" only, which
                        # misdirected users with a corrupt config at the
                        # standard path.
                        print(
                            f"warning: couldn't find or parse a {_CLI_DISPLAY[cli]} "
                            f"config to verify codegraph MCP registration. Skipping "
                            f"the MCP-tools prompt hint. If your config lives at a "
                            f"non-standard path, the agent may still see the tools at "
                            f"runtime. {install_hint}"
                        )
                built = build_review_prompt(
                    post_inline=post_inline,
                    extra_prompt=extra_prompt,
                    existing=existing,
                    fetch_ok=fetch_ok,
                    my_login=my_login,
                    author_login=(pr.get("author") or {}).get("login"),
                    cli=cli,
                    codegraph_present=codegraph_tools_available,
                    codegraph_affected_tests=codegraph_affected_block,
                    selected_agents=selected_agents,
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
                # Only call out the agent subset when it diverges from the
                # default — the banner is already crowded, and "agents: all"
                # would be noise on every default-shape launch. Compare as
                # SETS rather than tuples: `build_review_prompt` normalises
                # order before assembling, so a reordered-but-full subset
                # produces the default prompt and shouldn't trip this gate.
                # The friendly labels mirror what the user just saw on the
                # modal checkboxes — raw kebab-case ids would diverge from
                # the modal's label text.
                if set(selected_agents) != set(REVIEW_SKILLS):
                    if not selected_agents:
                        parts.append("agents: none (generic review)")
                    elif len(selected_agents) <= 3:
                        parts.append(
                            "agents: " + ", ".join(REVIEW_SKILL_LABELS[n] for n in selected_agents)
                        )
                    else:
                        parts.append(f"agents: {len(selected_agents)}/{len(REVIEW_SKILLS)}")
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
                launch_start = time.monotonic()
                rc = subprocess.call(cmd, cwd=local_path)
                launch_duration = time.monotonic() - launch_start

                # Append-only launch telemetry, recorded for EVERY exit
                # (success AND rc != 0) so aborts/crashes stay visible in the
                # data. Recorded BEFORE `_record_review` so that a failure in
                # that UPSERT (which has no try/except of its own) can't drop
                # this row. Best-effort: `_record_launch_telemetry` logs and
                # swallows its own DB errors so it can't break the launch.
                _record_launch_telemetry(
                    self.review_db,
                    pr_key=key,
                    cli=cli,
                    codegraph_tools=codegraph_tools_available,
                    affected_paths=affected_count,
                    existing_in_prompt=built.existing_shown,
                    post_inline=post_inline,
                    rereview=built.rereview,
                    approx_prompt_tokens=built.approx_tokens,
                    duration_seconds=launch_duration,
                    exit_code=rc,
                )

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
                    # Announce to Slack only after a clean exit, and only if a
                    # webhook is configured. Runs here (still suspended) so any
                    # warning prints to the session terminal; it's best-effort
                    # and never blocks the return to the TUI.
                    if slack_webhook:
                        self._notify_review_to_slack(
                            webhook_url=slack_webhook,
                            pr=pr,
                            repo=repo_full,
                            number=number,
                            reviewer_login=my_login,
                            pre_review=pre_review,
                            pre_review_ok=pre_review_ok,
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
                # Restore the materialised skills before releasing the
                # reservation so the workspace is in its pre-launch state
                # by the time the slot is yielded. (The reservation is
                # per-PR, so it doesn't serialise peers reviewing
                # *different* PRs of the same repo — and `gh pr checkout
                # --force` already races on the shared worktree for those,
                # which is a pre-existing limitation orthogonal to skills.)
                # The `None` guard handles early-return paths above where
                # materialisation never ran — there's nothing to restore.
                if skills_manifest is not None:
                    _cleanup_skills(skills_manifest)
                _release_in_progress(self.review_db, key)
                # Re-enable auto-refresh ticks now the agent has handed the
                # TTY back. In the inner finally (still inside `suspend()`)
                # so it's cleared by the time the TUI resumes regardless of
                # how the session ended (clean exit, Ctrl-C, crash).
                self._suspended_for_review = False

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
