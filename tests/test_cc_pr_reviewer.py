"""Tests for the pure helpers in cc_pr_reviewer.

Scope is deliberately narrow: only the I/O-free functions that gate
launch behavior. The TUI, subprocess flow, and gh-CLI shellouts are
intentionally out of scope here; they're better covered by integration
tests.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

import cc_pr_reviewer as _mod
from cc_pr_reviewer import (
    _APP_HOSTNAME,
    _REFRESH_CYCLE,
    _REFRESH_OPTIONS,
    _SKILL_COVERAGE,
    _THEME_OPTIONS,
    CLAUDE_THEME,
    CLAUDE_THEME_NAME,
    CODEGRAPH_AFFECTED_TESTS_CAP,
    CODEGRAPH_HINT_SUFFIX,
    CODEGRAPH_IMPACT_NUDGE,
    DEFAULT_THEME,
    EXISTING_COMMENT_BODY_CAP,
    EXISTING_COMMENT_LIST_CAP,
    POST_INLINE_APPROVE_SUFFIX,
    POST_INLINE_DEDUP_SUFFIX,
    POST_INLINE_FETCH_FAILED_SUFFIX,
    POST_INLINE_PROMPT,
    POST_INLINE_REREVIEW_APPROVE_SUFFIX,
    POST_INLINE_REREVIEW_RESOLVE_SUFFIX,
    POST_INLINE_REREVIEW_SUFFIX,
    PROMPT_SECTION_SEP,
    REVIEW_PROMPT_CLAUDE,
    REVIEW_PROMPT_SKILL_BASED,
    REVIEW_SKILL_LABELS,
    REVIEW_SKILLS,
    SKILL_FILE_NAME,
    WORKSPACE,
    InProgressHolder,
    ReviewInProgressError,
    SettingsScreen,
    _approx_tokens,
    _build_cli_command,
    _check_codegraph_setup,
    _cleanup_skills,
    _codegraph_mcp_registered,
    _first_available_cli,
    _get_setting,
    _in_progress_age_str,
    _is_newer,
    _join_agents,
    _load_in_progress,
    _materialise_skills,
    _open_review_db,
    _parse_semver,
    _pid_alive,
    _record_launch_telemetry,
    _refresh_interval_label,
    _release_in_progress,
    _reserve_in_progress,
    _resolve_theme,
    _review_cell,
    _seed_worktree_codegraph,
    _set_setting,
    _skills_dir,
    _worktree_path,
    build_review_prompt,
    build_slack_payload,
    check_prereqs,
    fetch_my_latest_review,
    format_codegraph_affected_tests,
    format_existing_comments,
    new_review_pr_keys,
    parse_refresh_interval,
)

# --- build_review_prompt ---------------------------------------------------


def _comment(login: str, **overrides: Any) -> dict[str, Any]:
    """Build a minimal review-comment dict (overridable per test)."""
    base: dict[str, Any] = {
        "user": {"login": login},
        "path": "a.py",
        "created_at": "2025-01-01T00:00:00Z",
        "body": "nit",
        "line": 1,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("cli", "expected_base"),
    [
        ("claude", REVIEW_PROMPT_CLAUDE),
        ("codex", REVIEW_PROMPT_SKILL_BASED),
        ("gemini", REVIEW_PROMPT_SKILL_BASED),
    ],
)
def test_plain_review_equals_base_prompt(cli: str, expected_base: str) -> None:
    """post_inline=False with no extras returns the CLI's base prompt verbatim.

    Claude uses the plugin-driven REVIEW_PROMPT_CLAUDE; codex and gemini
    share REVIEW_PROMPT_SKILL_BASED (they have no plugin marketplace, so
    they reference the bundled agent .md files instead).
    """
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
    )
    assert built.text == expected_base
    assert not built.rereview
    assert built.existing_shown == 0
    assert built.existing_total == 0


def test_post_inline_with_failed_fetch_appends_failed_suffix() -> None:
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=False,
        my_login="alice",
        author_login="bob",
    )
    assert POST_INLINE_PROMPT in built.text
    assert POST_INLINE_FETCH_FAILED_SUFFIX in built.text
    assert POST_INLINE_DEDUP_SUFFIX not in built.text
    assert POST_INLINE_REREVIEW_SUFFIX not in built.text


def test_post_inline_with_no_comments_and_ok_fetch_omits_both_dedup_suffixes() -> None:
    """fetch_ok=True with no usable comments means PR has none — no
    dedup hint, no FETCH_FAILED warning."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert POST_INLINE_PROMPT in built.text
    assert POST_INLINE_DEDUP_SUFFIX not in built.text
    assert POST_INLINE_FETCH_FAILED_SUFFIX not in built.text


def test_third_party_comments_trigger_dedup_but_not_rereview() -> None:
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[_comment("charlie")],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert POST_INLINE_DEDUP_SUFFIX in built.text
    assert POST_INLINE_REREVIEW_SUFFIX not in built.text
    assert not built.rereview


def test_existing_block_appears_on_plain_review() -> None:
    """Reviewer-context block is rendered even with post_inline=False.
    Existing inline comments are prior context, not a posting instruction
    — a future refactor that nests the existing-comments append under
    `if post_inline:` would silently drop that context on local-only
    reviews."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[_comment("charlie")],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert "Existing review comments" in built.text
    assert POST_INLINE_PROMPT not in built.text


def test_my_comment_on_others_pr_triggers_full_rereview_chain() -> None:
    """The full APPROVE+RESOLVE chain only fires when the gh user is NOT
    the PR author (GitHub 422 gate)."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[_comment("alice")],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert built.rereview
    assert POST_INLINE_REREVIEW_SUFFIX in built.text
    assert POST_INLINE_REREVIEW_APPROVE_SUFFIX in built.text
    assert POST_INLINE_REREVIEW_RESOLVE_SUFFIX in built.text


@pytest.mark.parametrize(
    ("cli", "expected_base"),
    [
        ("claude", REVIEW_PROMPT_CLAUDE),
        ("codex", REVIEW_PROMPT_SKILL_BASED),
        ("gemini", REVIEW_PROMPT_SKILL_BASED),
    ],
)
def test_full_chain_assembles_in_canonical_order(cli: str, expected_base: str) -> None:
    """Lock the canonical assembly for a representative non-trivial
    composition. The other tests use `in`/`not in` against `built.text`
    and would let through regressions that swap `PROMPT_SECTION_SEP`
    for `\\n`, duplicate a section, or reorder sections — exactly the
    kind of bug suffix-matrix changes are most likely to introduce.
    Parameterised over CLI so a future refactor can't accidentally
    branch the suffix matrix on CLI choice (the suffixes are gh-CLI
    instructions, not coding-agent-specific)."""
    existing = [_comment("alice")]
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="x",
        existing=existing,
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
    )
    expected = PROMPT_SECTION_SEP.join(
        [
            expected_base,
            "Additional instructions from reviewer:\nx",
            format_existing_comments(existing)[0],
            POST_INLINE_PROMPT
            + POST_INLINE_DEDUP_SUFFIX
            + POST_INLINE_REREVIEW_SUFFIX
            + POST_INLINE_REREVIEW_APPROVE_SUFFIX
            + POST_INLINE_REREVIEW_RESOLVE_SUFFIX,
        ]
    )
    assert built.text == expected


def test_my_comment_on_my_own_pr_raises_bar_but_skips_approve() -> None:
    """Self-author + rereview: REREVIEW_SUFFIX still applies (raise the
    bar), but APPROVE/RESOLVE are dropped because GitHub returns 422 on
    self-approve."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[_comment("alice")],
        fetch_ok=True,
        my_login="alice",
        author_login="alice",
    )
    assert built.rereview
    assert POST_INLINE_REREVIEW_SUFFIX in built.text
    assert POST_INLINE_REREVIEW_APPROVE_SUFFIX not in built.text
    assert POST_INLINE_REREVIEW_RESOLVE_SUFFIX not in built.text


def test_extra_prompt_is_inserted_verbatim_with_header() -> None:
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="watch the SQL injection on line 42",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login="bob",
    )
    assert "Additional instructions from reviewer:" in built.text
    assert "watch the SQL injection on line 42" in built.text


def test_rereview_uses_raw_list_not_formatted_block() -> None:
    """Comments missing path/body would be filtered out of the prompt
    block, but they still count toward `rereview` so we don't accidentally
    drop the bar-raising clause when the only evidence we have is in
    unrenderable entries."""
    unrenderable = _comment("alice", path="", body="", line=None)
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[unrenderable],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert built.rereview
    assert built.existing_shown == 0
    assert built.existing_total == 1


def test_my_login_none_never_triggers_rereview() -> None:
    """When `gh api user --jq .login` fails we get my_login=None — no
    way to know it's a re-review, so the gate must short-circuit. The
    paired APPROVE/RESOLVE asserts catch a bug that decoupled them from
    the REREVIEW gate (e.g. nesting them under a different condition)."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[_comment("alice")],
        fetch_ok=True,
        my_login=None,
        author_login="bob",
    )
    assert not built.rereview
    assert POST_INLINE_REREVIEW_SUFFIX not in built.text
    assert POST_INLINE_REREVIEW_APPROVE_SUFFIX not in built.text
    assert POST_INLINE_REREVIEW_RESOLVE_SUFFIX not in built.text


def test_first_review_other_pr_appends_approve_suffix() -> None:
    """A first review (no prior comment from us) of someone else's PR gets the
    approve-with-nits verdict instruction, but none of the re-review chain."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert not built.rereview
    assert POST_INLINE_APPROVE_SUFFIX in built.text
    assert POST_INLINE_REREVIEW_SUFFIX not in built.text
    assert POST_INLINE_REREVIEW_APPROVE_SUFFIX not in built.text


def test_first_review_self_authored_omits_approve_suffix() -> None:
    """We can't approve our own PR (GitHub 422), so a first review of our own
    PR keeps the plain COMMENT behavior."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="alice",
    )
    assert POST_INLINE_APPROVE_SUFFIX not in built.text


def test_first_review_my_login_none_omits_approve_suffix() -> None:
    """An unknown `my_login` can't prove the PR isn't ours, so stay
    conservative and don't emit the approve instruction."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login="bob",
    )
    assert POST_INLINE_APPROVE_SUFFIX not in built.text


def test_first_review_failed_fetch_omits_approve_suffix() -> None:
    """A failed existing-comments fetch can't confirm this is a first review
    (a re-review whose prior comments were unreachable looks identical), so
    the approve clause must stay off — fall through to plain COMMENT."""
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="",
        existing=[],
        fetch_ok=False,
        my_login="alice",
        author_login="bob",
    )
    assert POST_INLINE_FETCH_FAILED_SUFFIX in built.text
    assert POST_INLINE_APPROVE_SUFFIX not in built.text


@pytest.mark.parametrize(
    ("cli", "expected_base"),
    [
        ("claude", REVIEW_PROMPT_CLAUDE),
        ("codex", REVIEW_PROMPT_SKILL_BASED),
        ("gemini", REVIEW_PROMPT_SKILL_BASED),
    ],
)
def test_first_review_approve_assembles_in_canonical_order(cli: str, expected_base: str) -> None:
    """Lock the first-review approve-path assembly (parameterised over CLI so
    the suffix matrix can't drift into branching on coding-agent choice). A
    third-party comment triggers DEDUP but not re-review, so the approve
    suffix follows the dedup suffix."""
    existing = [_comment("charlie")]
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="x",
        existing=existing,
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
    )
    expected = PROMPT_SECTION_SEP.join(
        [
            expected_base,
            "Additional instructions from reviewer:\nx",
            format_existing_comments(existing)[0],
            POST_INLINE_PROMPT + POST_INLINE_DEDUP_SUFFIX + POST_INLINE_APPROVE_SUFFIX,
        ]
    )
    assert built.text == expected


# --- selected_agents subset (ConfirmScreen → build_review_prompt) ----------


def test_selected_agents_default_none_matches_default_constant() -> None:
    """`selected_agents=None` is the back-compat default, and must produce
    the same base prompt as the documented `REVIEW_PROMPT_CLAUDE` constant
    — otherwise any caller that hasn't migrated would see a silent
    prompt-shape drift on first launch after upgrade."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        selected_agents=None,
    )
    assert built.text == REVIEW_PROMPT_CLAUDE


def test_selected_agents_full_set_equals_default_constant() -> None:
    """Passing the full REVIEW_SKILLS tuple must be byte-identical to the
    default (None) — guards against the builder branching on subset vs
    default and producing two near-identical prompts that differ only
    in word choice."""
    built_default = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="claude",
    )
    built_full = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="claude",
        selected_agents=REVIEW_SKILLS,
    )
    assert built_default.text == built_full.text


def test_selected_agents_subset_drops_unselected_from_claude_prompt() -> None:
    """For Claude: the unselected agent's friendly label must NOT appear
    in the base prompt, and the selected ones MUST. Doc-only PRs are
    the motivating use case — dropping Code Reviewer + PR Test Analyzer
    so the plugin doesn't spend tokens on irrelevant dimensions."""
    selected = ("comment-analyzer", "code-simplifier")
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        selected_agents=selected,
    )
    for name in selected:
        assert REVIEW_SKILL_LABELS[name] in built.text
    dropped = tuple(n for n in REVIEW_SKILLS if n not in selected)
    for name in dropped:
        assert REVIEW_SKILL_LABELS[name] not in built.text


@pytest.mark.parametrize("cli", ["codex", "gemini"])
def test_selected_agents_subset_drops_unselected_from_skill_prompt(cli: str) -> None:
    """For codex/gemini: only the selected skills must appear as `$mentions`.
    Unselected skills must NOT — `_materialise_skills` doesn't write them
    to `.agents/skills/`, so a leftover `$name` mention in the prompt
    would tell the agent to activate a skill that isn't there."""
    selected = ("comment-analyzer", "code-simplifier")
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
        selected_agents=selected,
    )
    for name in selected:
        assert f"${name}" in built.text
    dropped = tuple(n for n in REVIEW_SKILLS if n not in selected)
    for name in dropped:
        assert f"${name}" not in built.text


def test_selected_agents_normalised_to_review_skills_order() -> None:
    """The builder must re-order any caller-supplied subset against
    REVIEW_SKILLS so the prompt's enumeration is deterministic — a
    future test or cache that compares prompt bytes shouldn't depend
    on the modal's checkbox-iteration order, and order-sensitive
    output also hurts prompt-cache reuse upstream."""
    # Pass in reverse REVIEW_SKILLS order; the prompt should still
    # enumerate in REVIEW_SKILLS order.
    reversed_subset = tuple(reversed(REVIEW_SKILLS))
    built_reversed = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="claude",
        selected_agents=reversed_subset,
    )
    built_natural = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="claude",
        selected_agents=REVIEW_SKILLS,
    )
    assert built_reversed.text == built_natural.text


def test_selected_agents_empty_set_produces_generic_review_prompt() -> None:
    """All-six-unchecked is a legitimate state (the user clicked through
    Ctrl+A by accident — refused at the ConfirmScreen — but a caller
    can still pass it directly to the builder). The prompt must degrade
    to a generic "review this PR" form with no skill / agent mentions,
    so codex/gemini don't fail-search for unmaterialised skills."""
    for cli in ("claude", "codex", "gemini"):
        built = build_review_prompt(
            post_inline=False,
            extra_prompt="",
            existing=[],
            fetch_ok=True,
            my_login=None,
            author_login=None,
            cli=cli,
            selected_agents=(),
        )
        # No `$<name>` mentions, no friendly labels.
        for name in REVIEW_SKILLS:
            assert f"${name}" not in built.text, f"cli={cli}: dropped skill leaked"
            assert REVIEW_SKILL_LABELS[name] not in built.text, (
                f"cli={cli}: dropped friendly label leaked"
            )
        # And for Claude specifically, the toolkit reference still stands
        # — we're just asking for a generic toolkit review with no agent
        # pinning. Codex/gemini omit the toolkit entirely.
        if cli == "claude":
            assert "PR Review Toolkit" in built.text
        else:
            assert "PR Review Toolkit" not in built.text


def test_selected_agents_singleton_grammar_for_skill_prompt() -> None:
    """Single-skill grammar matters: "one review skill" + the singular
    coverage clause. A naive f-string that hard-codes "six review
    skills" would silently lie to the agent in the singleton case."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="codex",
        selected_agents=("code-reviewer",),
    )
    assert "one review skill" in built.text
    assert "$code-reviewer" in built.text
    # No leftover plural agent count from a stale f-string.
    assert "six review skills" not in built.text


def test_selected_agents_materialise_writes_only_subset(tmp_path: Any) -> None:
    """`_materialise_skills(selected=...)` must drop SKILL.md files only
    for the selected names — not all six. Otherwise codex/gemini's
    auto-discovery would still see the unselected skills' metadata and
    might implicitly activate them, defeating the unchecking."""
    selected = ("comment-analyzer", "code-simplifier")
    _materialise_skills(tmp_path, selected=selected)
    for name in selected:
        assert (tmp_path / ".agents" / "skills" / name / SKILL_FILE_NAME).is_file()
    for name in REVIEW_SKILLS:
        if name not in selected:
            assert not (tmp_path / ".agents" / "skills" / name).exists(), (
                f"unselected skill {name!r} was materialised — auto-discovery would still see it"
            )


def test_selected_agents_materialise_default_writes_all_six(tmp_path: Any) -> None:
    """Back-compat: callers that don't pass `selected=` (every existing
    call site before this change) must still materialise every skill,
    matching the historical behaviour."""
    _materialise_skills(tmp_path)
    for name in REVIEW_SKILLS:
        assert (tmp_path / ".agents" / "skills" / name / SKILL_FILE_NAME).is_file()


def test_selected_agents_materialise_rejects_unknown_name(tmp_path: Any) -> None:
    """A typo'd skill name must surface as `ValueError` BEFORE any disk
    I/O — not as the `RuntimeError("failed to materialise skill …")`
    that the copy-block raises for environment failures (disk full,
    perms, read-only FS). Misclassifying a bad-input bug as an
    environment error would send a future debugger looking at the
    filesystem when the actual fault is a stale REVIEW_SKILLS reference.
    The error message must name the unknown key(s) so the caller can
    locate the typo."""
    with pytest.raises(ValueError, match="unknown skill name"):
        _materialise_skills(tmp_path, selected=("code-reviewer", "typo-skill"))
    # Nothing should have been written — validation runs before any
    # `mkdir` / `copy2`, so the workspace stays pristine.
    assert not (tmp_path / ".agents").exists()


def test_review_skill_labels_cover_every_skill() -> None:
    """Every REVIEW_SKILLS entry must have a friendly label — the Claude
    prompt builder and the ConfirmScreen checkboxes both index this
    dict by skill name. A missing entry would crash the prompt build
    at launch (KeyError) instead of surfacing as a clean test failure."""
    for name in REVIEW_SKILLS:
        assert name in REVIEW_SKILL_LABELS, f"REVIEW_SKILL_LABELS missing entry for {name!r}"


def test_skill_coverage_covers_every_skill() -> None:
    """Sibling guard for `_SKILL_COVERAGE`, indexed the same way by
    `_build_skill_based_prompt`. Without this, adding a 7th entry to
    REVIEW_SKILLS + REVIEW_SKILL_LABELS but forgetting `_SKILL_COVERAGE`
    would pass the labels test, then KeyError at codex/gemini prompt
    build — at import time for the full set, at launch time for any
    subset that happens to include the missing key."""
    for name in REVIEW_SKILLS:
        assert name in _SKILL_COVERAGE, f"_SKILL_COVERAGE missing entry for {name!r}"


# --- _join_agents (Oxford-comma helper) ------------------------------------


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["A"], "A"),
        (["A", "B"], "A and B"),
        (["A", "B", "C"], "A, B, and C"),
        (["A", "B", "C", "D"], "A, B, C, and D"),
    ],
)
def test_join_agents_oxford_grammar(items: list[str], expected: str) -> None:
    """Lock the exact wording each branch produces. The helper is only
    exercised transitively by subset tests that assert `$name in / not in`
    — a regression to "A B", a dropped Oxford comma, or the wrong
    separator in the 2-item branch would slip through those silently
    while the prompt still 'works'. The Oxford comma is the whole reason
    this helper exists; this test is what guarantees it stays."""
    assert _join_agents(items) == expected


# --- CodeGraph hint suffix (gated on `.codegraph/` presence) ---------------


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini"])
def test_codegraph_suffix_absent_by_default(cli: str) -> None:
    """Default `codegraph_present=False` must leave the prompt untouched.
    Guards against accidentally appending the hint on workspaces that
    don't have an index — the suffix references MCP tools that wouldn't
    resolve, which would just waste tokens."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
    )
    assert CODEGRAPH_HINT_SUFFIX not in built.text


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini"])
def test_codegraph_suffix_present_when_flag_set(cli: str) -> None:
    """When `_launch_claude` detects `.codegraph/` and sets the flag, the
    suffix must land in the prompt for every CLI — the MCP tool surface
    is identical across claude/codex/gemini, so the gate is presence of
    the index, not the CLI in use."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
        codegraph_present=True,
    )
    assert CODEGRAPH_HINT_SUFFIX in built.text


def test_codegraph_suffix_placed_after_extra_prompt_before_existing() -> None:
    """Lock the canonical section order when CodeGraph is present:
    base → reviewer extras → CodeGraph hint → existing comments →
    post-inline. Reviewer extras stay adjacent to the base (highest
    visibility for per-launch overrides); the hint sits with the
    "what to review" blocks rather than the "how to publish" block."""
    existing = [_comment("charlie")]
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="focus on the auth changes",
        existing=existing,
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        codegraph_present=True,
    )
    expected = PROMPT_SECTION_SEP.join(
        [
            REVIEW_PROMPT_CLAUDE,
            "Additional instructions from reviewer:\nfocus on the auth changes",
            # No precomputed affected block here, so the per-symbol impact
            # nudge rides along with the hint as one section.
            f"{CODEGRAPH_HINT_SUFFIX} {CODEGRAPH_IMPACT_NUDGE}",
            format_existing_comments(existing)[0],
            POST_INLINE_PROMPT + POST_INLINE_DEDUP_SUFFIX + POST_INLINE_APPROVE_SUFFIX,
        ]
    )
    assert built.text == expected


def test_codegraph_suffix_mentions_core_mcp_tools() -> None:
    """The hint's value is steering the agent to specific MCP tools by
    name. If a refactor decays it to generic "use CodeGraph", the agent
    falls back to grep+Read fan-out — defeats the integration. Lock the
    minimal set the prompt must call out."""
    for tool in (
        "codegraph_context",
        "codegraph_impact",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_trace",
        "codegraph_search",
    ):
        assert tool in CODEGRAPH_HINT_SUFFIX, f"hint missing `{tool}` mention"


def test_impact_nudge_dropped_when_affected_block_present() -> None:
    """The per-symbol `codegraph_impact` nudge is redundant once we've
    injected a precomputed `codegraph affected` block — the block already
    scopes the blast radius. Dropping it saves prompt tokens and avoids the
    agent re-deriving the same answer via a fan-out of per-symbol impact
    calls. Conversely, with no block the nudge must stay (it's the only
    blast-radius scoping signal). Lock both halves so a refactor can't
    silently always-include or always-drop it."""
    kwargs = dict(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        codegraph_present=True,
    )
    with_block = build_review_prompt(
        **kwargs, codegraph_affected_tests=format_codegraph_affected_tests(["tests/a.py"])
    )
    without_block = build_review_prompt(**kwargs, codegraph_affected_tests="")
    assert CODEGRAPH_IMPACT_NUDGE not in with_block.text
    assert CODEGRAPH_IMPACT_NUDGE in without_block.text
    # The base hint itself is present in both cases either way.
    assert CODEGRAPH_HINT_SUFFIX in with_block.text
    assert CODEGRAPH_HINT_SUFFIX in without_block.text


# --- approx_tokens / telemetry cost-side --------------------------------------


def test_approx_tokens_is_chars_over_four() -> None:
    """The estimate is a deliberate ~4-chars/token heuristic, not a real
    tokenizer. Lock the contract so a refactor doesn't silently swap in a
    different divisor (which would make historical telemetry rows
    incomparable to new ones)."""
    assert _approx_tokens("") == 0
    assert _approx_tokens("abc") == 0
    assert _approx_tokens("abcd") == 1
    assert _approx_tokens("x" * 400) == 100


def test_built_prompt_exposes_approx_tokens() -> None:
    """`build_review_prompt` must populate `approx_tokens` from the final
    assembled text so the launch path can log prompt size without
    re-deriving it (and so the figure always matches the bytes actually
    sent to the CLI)."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
    )
    assert built.approx_tokens == _approx_tokens(built.text)
    assert built.approx_tokens > 0


# --- format_codegraph_affected_tests ---------------------------------------


def test_affected_tests_empty_list_returns_empty_string() -> None:
    """Empty input → empty sentinel. `build_review_prompt` skips the
    section on falsy strings, so this is the path taken when the diff
    has no codegraph-traceable test impact."""
    assert format_codegraph_affected_tests([]) == ""


def test_affected_tests_whitespace_only_paths_drop_to_empty() -> None:
    """Pure-whitespace lines from `codegraph affected` stdout must not
    render as `- ` bullets — they'd waste tokens and confuse the agent."""
    assert format_codegraph_affected_tests(["", "   ", "\t"]) == ""


def test_affected_tests_dedups_and_sorts() -> None:
    """`codegraph affected` can report the same test twice when multiple
    changed files reach it. Dedup is on the cleaned path; sort gives
    stable prompt output across runs (otherwise diffing prompts becomes
    noisy and cache hits suffer)."""
    block = format_codegraph_affected_tests(
        ["tests/b.py", "tests/a.py", "tests/b.py", "  tests/a.py  "]
    )
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == ["- tests/a.py", "- tests/b.py"]


def test_affected_tests_caps_with_overflow_note() -> None:
    """Beyond the cap, the block must say so explicitly — otherwise a
    truncated 50-item list looks authoritative to the agent and it'll
    rely on a partial view of the impact surface."""
    paths = [f"tests/t_{i:03d}.py" for i in range(CODEGRAPH_AFFECTED_TESTS_CAP + 25)]
    block = format_codegraph_affected_tests(paths)
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(bullets) == CODEGRAPH_AFFECTED_TESTS_CAP
    assert f"showing {CODEGRAPH_AFFECTED_TESTS_CAP} of " in block
    assert "truncated" in block


def test_affected_tests_at_cap_does_not_show_overflow_note() -> None:
    """Exactly-at-cap is the boundary that's easy to off-by-one. No
    overflow note when the list fits exactly — saying "showing 50 of
    50, truncated" would lie about a complete list."""
    paths = [f"tests/t_{i:03d}.py" for i in range(CODEGRAPH_AFFECTED_TESTS_CAP)]
    block = format_codegraph_affected_tests(paths)
    assert "truncated" not in block


def test_affected_tests_one_over_cap_shows_overflow_note() -> None:
    """The other side of the at-cap boundary: CAP+1 is the smallest
    input that should trigger the overflow note. Pairs with the
    at-cap test to pin the `>` vs `>=` gate exactly. Catches a regression
    that flipped the inequality (would render `truncated` on a complete
    50-item list, or omit it on an actual 51-item one)."""
    paths = [f"tests/t_{i:03d}.py" for i in range(CODEGRAPH_AFFECTED_TESTS_CAP + 1)]
    block = format_codegraph_affected_tests(paths)
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(bullets) == CODEGRAPH_AFFECTED_TESTS_CAP
    assert "truncated" in block


def test_affected_tests_preserves_internal_whitespace_in_paths() -> None:
    """`p.strip()` should clean leading/trailing whitespace only; interior
    spaces (e.g. paths shipped from Windows test dirs, or rare repos with
    spaces in names) must round-trip verbatim. A regression that swapped
    `strip()` for `replace(" ", "")` or `split()` would silently corrupt
    these paths in the prompt — the agent then can't find the file."""
    block = format_codegraph_affected_tests(["  tests/has space.py  ", "tests/normal.py"])
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == ["- tests/has space.py", "- tests/normal.py"]


def test_affected_tests_mixed_case_sort_is_deterministic_ascii() -> None:
    """File paths are case-sensitive on POSIX; the sort key must be
    deterministic so the prompt's section bytes stay stable across runs
    (cache-hit stability). Default `sorted({...})` uses ASCII ordering,
    which puts uppercase before lowercase. Lock that behaviour here —
    if a future refactor adds `key=str.lower` the order changes silently
    and prompt-cache hit rates drop."""
    block = format_codegraph_affected_tests(["tests/a.py", "Tests/B.py", "tests/B.py"])
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == ["- Tests/B.py", "- tests/B.py", "- tests/a.py"]


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini"])
def test_affected_tests_block_lands_after_hint_before_existing(cli: str) -> None:
    """Lock the section order across CLIs: base → extras → CodeGraph hint
    → CodeGraph affected tests → existing comments → post-inline. The two
    CodeGraph sections must be contiguous so the agent reads them as one
    coherent block; placing the affected-tests block before the hint
    (or after existing-comments) would split the CodeGraph context."""
    existing = [_comment("charlie")]
    affected_block = format_codegraph_affected_tests(["tests/foo_test.py"])
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="extra",
        existing=existing,
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli=cli,
        codegraph_present=True,
        codegraph_affected_tests=affected_block,
    )
    base = REVIEW_PROMPT_CLAUDE if cli == "claude" else REVIEW_PROMPT_SKILL_BASED
    expected = PROMPT_SECTION_SEP.join(
        [
            base,
            "Additional instructions from reviewer:\nextra",
            CODEGRAPH_HINT_SUFFIX,
            affected_block,
            format_existing_comments(existing)[0],
            POST_INLINE_PROMPT + POST_INLINE_DEDUP_SUFFIX + POST_INLINE_APPROVE_SUFFIX,
        ]
    )
    assert built.text == expected


def test_affected_tests_block_absent_when_kwarg_empty() -> None:
    """The block-skipping default keeps existing test fixtures unchanged
    and matches the "no diff impact / no index" runtime path. Without
    this guard, an empty-string section would still render as an extra
    PROMPT_SECTION_SEP, polluting the assembled prompt."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        codegraph_present=True,
        codegraph_affected_tests="",
    )
    assert built.text == PROMPT_SECTION_SEP.join(
        [REVIEW_PROMPT_CLAUDE, f"{CODEGRAPH_HINT_SUFFIX} {CODEGRAPH_IMPACT_NUDGE}"]
    )


def test_affected_tests_block_can_render_without_codegraph_hint() -> None:
    """Edge case: `codegraph_present=False` with a non-empty affected
    block can't happen in normal `_launch_claude` flow (the orchestration
    gates both on the same precondition), but the prompt builder
    shouldn't enforce that coupling — each section is independently
    gated so a future caller (e.g. a CI driver that ships a pre-computed
    block without the MCP wiring) doesn't have to lie about
    `codegraph_present` to inject the list."""
    affected_block = format_codegraph_affected_tests(["tests/a.py"])
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
        cli="claude",
        codegraph_present=False,
        codegraph_affected_tests=affected_block,
    )
    assert CODEGRAPH_HINT_SUFFIX not in built.text
    assert affected_block in built.text


# --- _codegraph_mcp_registered --------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON config under a fake `home`, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_mcp_registered_returns_none_when_no_config_file_exists(tmp_path: Path) -> None:
    """No config file at any expected path → `None` (undetectable).
    Distinct from `False` so the caller's warning can differentiate
    "user hasn't run the installer at all for this CLI" from "user
    has the CLI configured but skipped codegraph"."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for cli in ("claude", "codex", "gemini"):
        assert _codegraph_mcp_registered(cli, workspace, home=home) is None


def test_mcp_registered_returns_true_for_claude_global_json(tmp_path: Path) -> None:
    """`~/.claude.json` with `mcpServers.codegraph` → True. This is the
    canonical install path written by `codegraph install --target=claude
    --location=global`."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "codegraph": {
                    "type": "stdio",
                    "command": "codegraph",
                    "args": ["serve", "--mcp"],
                }
            }
        },
    )
    assert _codegraph_mcp_registered("claude", workspace, home=home) is True


def test_mcp_registered_returns_true_for_claude_project_local_mcp_json(tmp_path: Path) -> None:
    """Project-local `<workspace>/.mcp.json` is also honoured — that's
    what `codegraph install --location=local` writes for Claude Code.
    Skipping this path would suppress the suffix on workspaces the user
    explicitly wired up."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _write_json(
        workspace / ".mcp.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    assert _codegraph_mcp_registered("claude", workspace, home=home) is True


def test_mcp_registered_returns_false_when_claude_config_lacks_entry(tmp_path: Path) -> None:
    """`~/.claude.json` exists with `mcpServers` but no `codegraph` key
    → False (confirmed not registered). User has Claude Code set up but
    didn't run `codegraph install` for it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _write_json(
        home / ".claude.json",
        {"mcpServers": {"other-server": {"command": "other"}}},
    )
    assert _codegraph_mcp_registered("claude", workspace, home=home) is False


def test_mcp_registered_returns_false_when_mcp_servers_key_missing(tmp_path: Path) -> None:
    """A claude config without an `mcpServers` key at all is still a
    "confirmed not registered" — the file exists, we read it, no entry."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _write_json(home / ".claude.json", {"theme": "dark"})
    assert _codegraph_mcp_registered("claude", workspace, home=home) is False


def test_mcp_registered_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    """File present but unparseable → None (undetectable). Conservative
    fall-through: better than guessing False (would suppress a working
    setup with a transient parse error) or True (would emit a hint for
    tools whose load state we can't verify)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    (home / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text("{not valid json", encoding="utf-8")
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None


def test_mcp_registered_returns_true_for_codex_toml_section_header(tmp_path: Path) -> None:
    """Codex stores MCP config in `~/.codex/config.toml` as a TOML
    section header `[mcp_servers.codegraph]` — the canonical form
    written by `codegraph install --target=codex`. We match the header
    via regex (sidesteps Python 3.10's lack of stdlib tomllib)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '[mcp_servers.codegraph]\ncommand = "codegraph"\nargs = ["serve", "--mcp"]\n',
        encoding="utf-8",
    )
    assert _codegraph_mcp_registered("codex", workspace, home=home) is True


def test_mcp_registered_returns_false_for_codex_toml_without_section(tmp_path: Path) -> None:
    """A codex config that has other MCP servers but not codegraph →
    False. Locks the regex's specificity — a bare `mcp_servers` mention
    or an `[mcp_servers.other]` section must not false-positive."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '# codegraph not configured here\n[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )
    assert _codegraph_mcp_registered("codex", workspace, home=home) is False


def test_mcp_registered_returns_true_for_gemini_workspace_local(tmp_path: Path) -> None:
    """Gemini supports both `~/.gemini/settings.json` and
    `<workspace>/.gemini/settings.json`. Project-local config in the
    PR's checked-out clone wins independently of the global config —
    needed for users who only ever wired Gemini per-project."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _write_json(
        workspace / ".gemini" / "settings.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    assert _codegraph_mcp_registered("gemini", workspace, home=home) is True


def test_mcp_registered_corrupt_global_does_not_mask_valid_workspace(
    tmp_path: Path,
) -> None:
    """Regression test for the early-return bug: when `~/.claude.json` is
    corrupt but `<workspace>/.mcp.json` has a valid `codegraph` entry, the
    helper must still return True. Pre-refactor the JSON-parse `except`
    returned None immediately, suppressing the suffix despite the agent
    having the tools available via the workspace-local config. Locks the
    "loop-don't-return on errors" behaviour the helper now implements."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # Corrupt global config.
    (home / ".claude.json").write_text("{not valid", encoding="utf-8")
    # Valid workspace-local config WITH codegraph.
    _write_json(
        workspace / ".mcp.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    assert _codegraph_mcp_registered("claude", workspace, home=home) is True


def test_mcp_registered_absent_global_plus_workspace_without_codegraph_is_false(
    tmp_path: Path,
) -> None:
    """Multi-candidate accumulation: with the global config absent and
    only the workspace-local present-without-codegraph, the helper must
    return False (one config was seen and lacked the entry), NOT None
    (which would imply no config was seen at all). The warning prose
    branches on this distinction — under-reporting as None would steer
    the user to "couldn't find a config" when their config exists and
    just lacks the entry."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _write_json(
        workspace / ".mcp.json",
        {"mcpServers": {"other-server": {"command": "other"}}},
    )
    assert _codegraph_mcp_registered("claude", workspace, home=home) is False


def test_mcp_registered_returns_none_on_corrupt_toml(tmp_path: Path) -> None:
    """Locks the codex TOML `except (OSError, UnicodeDecodeError)` path
    (currently the only way to exercise it is a non-UTF-8 byte sequence,
    since `read_text` doesn't raise on a syntactically-broken TOML body
    — that just causes the regex to not match and the helper returns
    False). A refactor that drops or narrows the except tuple would let
    OSError escape into `_launch_claude` and crash the launch
    post-suspend; this test pins the silent-degrade behaviour."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # Non-UTF-8 bytes — `read_text(encoding="utf-8")` raises
    # UnicodeDecodeError, which the helper's except catches and turns
    # into `had_parse_error=True` → returns None at the end.
    cfg.write_bytes(b"\xff\xfe not valid utf-8 \x80")
    assert _codegraph_mcp_registered("codex", workspace, home=home) is None


def test_mcp_registered_top_level_non_dict_is_none(tmp_path: Path) -> None:
    """A JSON file that parses but whose root isn't a dict (`[]`, `"x"`,
    `42`) is structurally unusable — we can't navigate to `mcpServers`.
    Must resolve to None ("couldn't find or parse"), not False ("exists
    but lacks the entry"): the latter routes the user to
    `codegraph install`, which won't fix a malformed config. Pre-fix
    this fell through to `return False`."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('["not", "a", "dict"]', encoding="utf-8")
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None


def test_mcp_registered_mcp_servers_non_dict_is_none(tmp_path: Path) -> None:
    """`mcpServers` present but not a dict (string, list, null,
    primitive) is structurally malformed and must resolve to None for
    the same reason as the top-level case — and also because
    `isinstance(mcp, dict)` is the ONLY thing keeping the substring
    membership tests (`"codegraph" in "codegraph"` → True;
    `"codegraph" in ["codegraph"]` → True) from false-positively
    returning True. Locking the dict-typed guard here means a future
    refactor that drops it (e.g. `if "codegraph" in mcp: return True`)
    fails this test loudly instead of quietly claiming registration
    based on a substring match."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # The pathological-but-realistic case: mcpServers is a string that
    # contains the substring "codegraph". Without the dict guard, this
    # would return True via `"codegraph" in "codegraph"`.
    _write_json(home / ".claude.json", {"mcpServers": "codegraph"})
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None
    # Also exercise the list-shape (membership check would pass on
    # `"codegraph" in ["codegraph"]` if the guard were dropped).
    _write_json(home / ".claude.json", {"mcpServers": ["codegraph"]})
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None
    # And null — `None.get("codegraph")` would AttributeError if the
    # guard were dropped; the helper must surface None cleanly.
    _write_json(home / ".claude.json", {"mcpServers": None})
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None


def test_mcp_registered_handles_non_regular_file_at_config_path(tmp_path: Path) -> None:
    """If the user has accidentally `mkdir ~/.claude.json` (or a dangling
    symlink at the config path), `is_file()` is False but `exists()` is
    True. Pre-refactor this was conflated with "no config" (return None),
    sending the user a "couldn't find" warning that doesn't fit the
    actual state. Now it's bucketed as a parse-failure: `any_file_seen`
    stays False but `had_parse_error` flips True, the return is still
    None, but the warning prose ("couldn't find or parse") now covers
    the case honestly."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    # Make a directory at the config path — `is_file()` returns False.
    (home / ".claude.json").mkdir(parents=True)
    assert _codegraph_mcp_registered("claude", workspace, home=home) is None


def test_mcp_registered_per_cli_isolation(tmp_path: Path) -> None:
    """A codegraph entry registered for Claude only must NOT be reported
    as registered for codex or gemini — the gate is per-launched-CLI,
    not "any CLI has codegraph". Otherwise toggling via `c` to a
    not-yet-wired CLI would still emit the misleading hint."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _write_json(
        home / ".claude.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    # Codex/gemini configs deliberately created without codegraph so the
    # result is False (confirmed-missing), not None (undetectable).
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text("# empty\n", encoding="utf-8")
    _write_json(home / ".gemini" / "settings.json", {"theme": "dark"})

    assert _codegraph_mcp_registered("claude", workspace, home=home) is True
    assert _codegraph_mcp_registered("codex", workspace, home=home) is False
    assert _codegraph_mcp_registered("gemini", workspace, home=home) is False


# --- workspace=None (startup) path for _codegraph_mcp_registered ----------


def test_mcp_registered_workspace_none_skips_project_local_probes(tmp_path: Path) -> None:
    """Passing `workspace=None` (the startup path — no PR selected yet)
    must skip workspace-local config probes. Locks the contract by
    creating an actively-positive `<workspace>/.mcp.json` and verifying
    the same call returns True with `workspace=workspace_path` but
    False with `workspace=None` — a future refactor that accidentally
    treated `None` as `Path(".")`, `home`, or `workspace_path` would
    flip the None-branch to True and fail this test loudly. The bare
    "asserts return values" version this replaces would have passed
    such a refactor silently."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Global config WITHOUT codegraph → without workspace probe, this
    # is the determinate False result.
    _write_json(home / ".claude.json", {"mcpServers": {"other": {}}})
    # Workspace-local config WITH codegraph — this is the positive
    # entry that workspace=None must NOT see.
    _write_json(
        workspace / ".mcp.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    # With workspace passed, the project-local entry resolves the probe.
    assert _codegraph_mcp_registered("claude", workspace=workspace, home=home) is True
    # With workspace=None, the same call must skip the project-local file
    # and fall through to False on the global config alone.
    assert _codegraph_mcp_registered("claude", workspace=None, home=home) is False
    # Gemini path follows the same contract — project-local entry must
    # not be visible without an explicit workspace.
    _write_json(
        workspace / ".gemini" / "settings.json",
        {"mcpServers": {"codegraph": {"command": "codegraph"}}},
    )
    assert _codegraph_mcp_registered("gemini", workspace=workspace, home=home) is True
    assert _codegraph_mcp_registered("gemini", workspace=None, home=home) is None


def test_mcp_registered_workspace_none_still_resolves_global_true(tmp_path: Path) -> None:
    """The global probe must work even without a workspace — that's the
    whole point of the startup health check. A global codegraph entry
    must return True regardless of `workspace` being passed."""
    home = tmp_path / "home"
    _write_json(home / ".claude.json", {"mcpServers": {"codegraph": {}}})
    assert _codegraph_mcp_registered("claude", workspace=None, home=home) is True


# --- _check_codegraph_setup (startup three-state diagnosis) ---------------


def test_check_codegraph_setup_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `codegraph` binary on PATH → "not-installed" — the silent
    state for users who don't have CodeGraph and don't want to be
    nagged. Parameterless because the binary check happens before any
    config probe, so per-CLI behaviour is identical."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed())
    for cli in ("claude", "codex", "gemini"):
        assert _check_codegraph_setup(cli) == "not-installed"


def test_check_codegraph_setup_binary_only_when_no_mcp_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary on PATH but no config file at any standard global path →
    "binary-only". This is the toast-worthy state: the user installed
    the binary (e.g. `npm i -g`) but skipped `codegraph install`. The
    one-shot warning at startup catches it before they hit Enter on
    a PR and discover the gap mid-review."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("codegraph"))
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    for cli in ("claude", "codex", "gemini"):
        assert _check_codegraph_setup(cli, home=empty_home) == "binary-only"


def test_check_codegraph_setup_binary_only_when_other_cli_wired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User wired CodeGraph for claude but toggled cc-reviewer to
    codex via `c`. Setup for codex is "binary-only" — and the toast
    re-emit in `action_toggle_cli` relies on this distinction so the
    user learns about the gap immediately instead of at launch time."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("codegraph"))
    home = tmp_path / "home"
    _write_json(home / ".claude.json", {"mcpServers": {"codegraph": {}}})
    assert _check_codegraph_setup("claude", home=home) == "wired"
    assert _check_codegraph_setup("codex", home=home) == "binary-only"
    assert _check_codegraph_setup("gemini", home=home) == "binary-only"


def test_check_codegraph_setup_wired_per_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary present AND global MCP entry exists for the queried CLI
    → "wired" — the happy path, no toast. Test each CLI independently
    so a wiring for one doesn't accidentally short-circuit the check
    for another (per-CLI isolation, mirroring the equivalent
    `_codegraph_mcp_registered` test)."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("codegraph"))
    home = tmp_path / "home"
    # Claude wired via canonical install.
    _write_json(home / ".claude.json", {"mcpServers": {"codegraph": {}}})
    # Codex wired via TOML section header.
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.codegraph]\ncommand = "codegraph"\n', encoding="utf-8"
    )
    # Gemini wired via hand-written settings.
    _write_json(home / ".gemini" / "settings.json", {"mcpServers": {"codegraph": {}}})
    for cli in ("claude", "codex", "gemini"):
        assert _check_codegraph_setup(cli, home=home) == "wired"


def test_check_codegraph_setup_folds_undetectable_into_binary_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_codegraph_mcp_registered` returns None when a config is
    unreadable/malformed. The startup check collapses both False and
    None into "binary-only" — at startup the precise reason doesn't
    matter, only "are tools going to be available for this CLI". A
    corrupt config and a missing config both produce the same
    actionable toast: run `codegraph install`."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("codegraph"))
    home = tmp_path / "home"
    (home).mkdir()
    # Corrupt JSON at the global config path → mcp probe returns None.
    (home / ".claude.json").write_text("{not valid", encoding="utf-8")
    assert _check_codegraph_setup("claude", home=home) == "binary-only"


# --- _maybe_notify_codegraph_setup (App-method behaviour, stub-driven) ----


class _NotifyRecorder:
    """Lightweight stand-in for `PRReviewer` that captures `notify` calls.

    Avoids spinning a Textual `Pilot` for what's fundamentally a small
    decision tree: `_check_codegraph_setup` → branch on three values.
    Mirrors only the attributes the method actually reads (`self.cli`)
    and the one method it calls (`self.notify`)."""

    def __init__(self, cli: str = "claude") -> None:
        self.cli = cli
        self.notify_calls: list[tuple[str, dict[str, Any]]] = []

    def notify(self, msg: str, **kwargs: Any) -> None:
        self.notify_calls.append((msg, kwargs))


def test_maybe_notify_codegraph_setup_fires_for_binary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`binary-only` is the only state that should produce a toast.
    Locks the sign of the comparison — a refactor that flipped it
    ("toast on wired") would invert the user-visible behaviour
    (nag the happy path, stay silent on the real problem)."""
    monkeypatch.setattr(_mod, "_check_codegraph_setup", lambda *_a, **_kw: "binary-only")
    recorder = _NotifyRecorder(cli="claude")
    _mod.PRReviewer._maybe_notify_codegraph_setup(recorder)
    assert len(recorder.notify_calls) == 1
    msg, kwargs = recorder.notify_calls[0]
    assert "Claude Code" in msg
    assert kwargs.get("severity") == "warning"


@pytest.mark.parametrize("state", ["wired", "not-installed"])
def test_maybe_notify_codegraph_setup_silent_for_non_binary_only(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """`wired` (happy path) and `not-installed` (user doesn't have
    CodeGraph) must both stay silent. Tested as separate parameter
    cases so a regression that nags one but not the other is named."""
    monkeypatch.setattr(_mod, "_check_codegraph_setup", lambda *_a, **_kw: state)
    recorder = _NotifyRecorder(cli="claude")
    _mod.PRReviewer._maybe_notify_codegraph_setup(recorder)
    assert recorder.notify_calls == []


def test_maybe_notify_codegraph_setup_swallows_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surface guard is what keeps `on_mount` / `action_toggle_cli`
    from crashing if a future refactor reintroduces an unguarded raise
    path (e.g. a new filesystem probe inside `_check_codegraph_setup`).
    A best-effort early-warning that takes down startup is worse than
    no warning at all."""

    def boom(*_a: Any, **_kw: Any) -> str:
        raise PermissionError("simulated EACCES on ~/.codex")

    monkeypatch.setattr(_mod, "_check_codegraph_setup", boom)
    recorder = _NotifyRecorder(cli="codex")
    # Must NOT raise; must NOT toast.
    _mod.PRReviewer._maybe_notify_codegraph_setup(recorder)
    assert recorder.notify_calls == []


def test_maybe_notify_codegraph_setup_uses_active_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The toast prose pulls from `_CODEGRAPH_INSTALL_HINT[self.cli]`.
    Verifies the gemini branch is reachable here (relevant because the
    gemini install hint diverges from claude/codex — codegraph's
    installer has no `--target=gemini`, so the gemini toast must point
    at the manual-setup path)."""
    monkeypatch.setattr(_mod, "_check_codegraph_setup", lambda *_a, **_kw: "binary-only")
    recorder = _NotifyRecorder(cli="gemini")
    _mod.PRReviewer._maybe_notify_codegraph_setup(recorder)
    assert len(recorder.notify_calls) == 1
    msg, _ = recorder.notify_calls[0]
    assert "Gemini" in msg
    # Gemini-specific install hint: manual edit of `~/.gemini/settings.json`
    # (codegraph's installer doesn't accept `--target=gemini`). The hint
    # prose explains this negation, so we don't substring-check the
    # `--target=gemini` literal — `settings.json` is the load-bearing
    # piece that proves the toast picked the gemini branch.
    assert "settings.json" in msg
    # The recommended action shouldn't be the installer command —
    # check that "codegraph install" doesn't appear as a directive
    # (it does NOT in the gemini hint; only the claude/codex hints
    # use the installer command).
    assert "codegraph install" not in msg


def test_codegraph_install_hint_covers_every_cli() -> None:
    """Lockstep invariant: every `CliChoice` listed in `_CLI_CYCLE` must
    have a matching `_CODEGRAPH_INSTALL_HINT` entry. A 4th CLI added to
    the cycle without a hint would `KeyError` in
    `_maybe_notify_codegraph_setup`'s toast (line under
    `_CODEGRAPH_INSTALL_HINT[self.cli]`) the first time a user toggles
    to it AND the new CLI lacks an MCP entry — a latent crash hidden
    behind a multi-step user action. Catching it at import time via
    this test is essentially free."""
    assert set(_mod._CODEGRAPH_INSTALL_HINT) == set(_mod._CLI_CYCLE)


# --- Skill-based prompt (codex / gemini share this) ------------------------


def test_skill_prompt_mentions_every_skill_with_dollar_prefix() -> None:
    """The codex/gemini prompt uses `$<skill-name>` mentions to force
    explicit activation of every review dimension (implicit activation
    is non-deterministic — the model might skip one). If a skill is
    added to REVIEW_SKILLS but `_build_skill_based_prompt` doesn't pick
    it up, codex/gemini would silently skip that dimension — catch the
    drift here."""
    for name in REVIEW_SKILLS:
        assert f"${name}" in REVIEW_PROMPT_SKILL_BASED, (
            f"skill prompt missing explicit `${name}` mention"
        )


def test_skill_prompt_does_not_name_pr_review_toolkit() -> None:
    """The PR Review Toolkit is a Claude-plugin concept. Codex and gemini
    have no plugin marketplace; they auto-discover skills under
    `.agents/skills/`. The prompt must NOT mention the toolkit — doing
    so would invite the CLI to fail-search for a plugin that doesn't
    exist in its environment."""
    assert "PR Review Toolkit" not in REVIEW_PROMPT_SKILL_BASED


def test_codex_and_gemini_share_byte_identical_base_prompt() -> None:
    """Codex and gemini both consume REVIEW_PROMPT_SKILL_BASED unchanged
    and share the `.agents/skills/` discovery convention. If they ever
    need to diverge, splitting them is a deliberate change — this test
    makes that intent explicit."""
    codex = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="codex",
    )
    gemini = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login=None,
        author_login=None,
        cli="gemini",
    )
    assert codex.text == gemini.text


def test_bundled_skills_exist_on_disk_with_frontmatter() -> None:
    """REVIEW_SKILLS is the manifest used by the codex/gemini prompt;
    every name listed there must have a corresponding SKILL.md on disk
    with the YAML frontmatter Codex/Gemini need for skill discovery
    (auto-discovery parses `name:` + `description:` from the frontmatter).
    Guards against typo drift in the manifest, packaging regressions
    that drop `skills/`, and frontmatter that decays to plain prose."""
    skills_dir = _skills_dir()
    for name in REVIEW_SKILLS:
        skill_md = skills_dir / name / SKILL_FILE_NAME
        assert skill_md.is_file(), f"missing bundled SKILL.md for skill: {name}"
        text = skill_md.read_text(encoding="utf-8")
        # The skill loader needs a fenced YAML block at the very top; if
        # the file starts with anything else (a stray BOM, a markdown
        # heading, blank lines) the frontmatter parse fails and the
        # skill won't be discoverable.
        assert text.startswith("---\n"), f"{name}: SKILL.md missing leading `---`"
        assert f"name: {name}" in text, f"{name}: frontmatter missing `name: {name}`"
        assert "description:" in text, f"{name}: frontmatter missing `description:`"


# --- _materialise_skills / _cleanup_skills ---------------------------------


def test_materialise_writes_every_skill_to_workspace(tmp_path: Any) -> None:
    """`_materialise_skills(workspace)` must populate exactly
    `<workspace>/.agents/skills/<name>/SKILL.md` for every name in
    REVIEW_SKILLS — that's the path codex and gemini auto-discover at."""
    _materialise_skills(tmp_path)
    for name in REVIEW_SKILLS:
        dst = tmp_path / ".agents" / "skills" / name / SKILL_FILE_NAME
        assert dst.is_file(), f"materialise didn't write {dst.relative_to(tmp_path)}"
        # Sanity-check the body landed (frontmatter present); a 0-byte
        # write would tell codex/gemini the skill exists but has no
        # instructions, which is worse than not materialising.
        assert dst.read_text(encoding="utf-8").startswith("---\n")


def test_cleanup_removes_only_materialised_paths(tmp_path: Any) -> None:
    """Cleanup must undo materialise — remove our SKILL.md files and any
    parent dirs we'd have created. After a clean materialise/cleanup
    cycle the workspace should look untouched."""
    manifest = _materialise_skills(tmp_path)
    _cleanup_skills(manifest)
    assert not (tmp_path / ".agents").exists(), (
        "cleanup left `.agents/` behind — should be removed if we created it empty"
    )


def test_cleanup_restores_preexisting_skill_md_exactly(tmp_path: Any) -> None:
    """The collision case the codex review caught: if the reviewed PR
    ships its own `.agents/skills/<our-name>/SKILL.md`, materialise
    overwrites it and cleanup must restore the original bytes exactly.
    Without snapshot-and-restore, the review leaves the worktree with
    either our content (if cleanup is skipped) or a missing file (if
    cleanup unlinks blindly) — both surface as `git status` noise on
    the next session and corrupt the PR's tracked content."""
    skill_dir = tmp_path / ".agents" / "skills" / REVIEW_SKILLS[0]
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / SKILL_FILE_NAME
    # Use bytes with a non-UTF-8-safe sequence to prove the snapshot
    # round-trips raw bytes, not text-decoded content.
    original_bytes = b"---\nname: user-own-version\n---\n\nOriginal \x80 prose.\n"
    skill_md.write_bytes(original_bytes)

    manifest = _materialise_skills(tmp_path)
    # Mid-flight, our content is in place (proves materialise actually
    # ran; otherwise the "restore" could be a no-op masquerading as success).
    assert skill_md.read_bytes() != original_bytes
    _cleanup_skills(manifest)

    assert skill_md.read_bytes() == original_bytes


def test_cleanup_preserves_user_content_inside_skill_dir(tmp_path: Any) -> None:
    """If a user has sibling files inside a skill dir (e.g. a
    `references/` subdir from a project-tracked skill that shares the
    name), cleanup must NOT remove them. The empty-dir `rmdir` is
    guarded by `skill_dir_existed` in the snapshot, so we only `rmdir`
    dirs we created — sibling content the user owns is safe."""
    user_file_path = tmp_path / ".agents" / "skills" / REVIEW_SKILLS[0] / "user_extra.txt"
    user_file_path.parent.mkdir(parents=True)
    user_file_path.write_text("user content", encoding="utf-8")

    manifest = _materialise_skills(tmp_path)
    _cleanup_skills(manifest)

    # Our SKILL.md is gone (no original snapshot for it), user's file survived.
    assert not (tmp_path / ".agents" / "skills" / REVIEW_SKILLS[0] / SKILL_FILE_NAME).exists()
    assert user_file_path.is_file()
    assert user_file_path.read_text(encoding="utf-8") == "user content"


def test_cleanup_can_round_trip_repeatedly(tmp_path: Any) -> None:
    """Sequential materialise/cleanup cycles (the common case across
    multiple PR reviews in the same workspace) must each leave the
    workspace pristine — no accumulating empty dirs, no stale files."""
    for _ in range(3):
        manifest = _materialise_skills(tmp_path)
        _cleanup_skills(manifest)
        assert not (tmp_path / ".agents").exists()


def test_cleanup_preserves_preexisting_agents_dir(tmp_path: Any) -> None:
    """If the user already has a populated `.agents/` for their own
    purposes (a sibling tool's skill, project-tracked config), our
    cleanup must leave it intact — the empty-parent-rmdir is gated
    on `agents_dir_existed=False` in the manifest, so a pre-existing
    `.agents/` is never touched."""
    user_skill = tmp_path / ".agents" / "skills" / "user-own-skill" / SKILL_FILE_NAME
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: user-own-skill\n---\n", encoding="utf-8")

    manifest = _materialise_skills(tmp_path)
    _cleanup_skills(manifest)

    # Our six skill dirs are gone; user's stays.
    for name in REVIEW_SKILLS:
        assert not (tmp_path / ".agents" / "skills" / name).exists()
    assert user_skill.is_file()


def test_materialise_raises_clear_error_when_copy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A partial-materialise failure (disk full, perms, read-only FS)
    must surface with the offending skill name in the message —
    a bare `shutil` traceback at line N gives the user no idea which
    of the six writes failed."""

    def fail_copy(*_a: Any, **_kw: Any) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(_mod.shutil, "copy2", fail_copy)
    with pytest.raises(RuntimeError, match=r"failed to materialise skill '.*'"):
        _materialise_skills(tmp_path)


# --- check_prereqs malformed-skill detection -------------------------------


def test_check_prereqs_flags_malformed_skill_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SKILL.md present-but-malformed (empty file, missing frontmatter,
    half-extracted wheel) would pass an `is_file()` check and only fail
    later as a 'skill not found' from codex/gemini mid-review. The
    head-bytes shape check in `check_prereqs` keeps that failure in the
    same `problems` flow as missing files."""
    _stub_prereq_deps(monkeypatch, installed=("codex",), persisted="codex")
    for name in REVIEW_SKILLS:
        (tmp_path / name).mkdir()
        if name == REVIEW_SKILLS[0]:
            # First skill: empty file — passes is_file() but has no frontmatter.
            (tmp_path / name / SKILL_FILE_NAME).write_text("", encoding="utf-8")
        else:
            (tmp_path / name / SKILL_FILE_NAME).write_text(
                f"---\nname: {name}\ndescription: x\n---\n\nbody\n",
                encoding="utf-8",
            )
    monkeypatch.setattr(_mod, "_skills_dir", lambda: tmp_path)

    problems = check_prereqs()
    assert any("malformed" in p and REVIEW_SKILLS[0] in p for p in problems), (
        f"expected malformed-skill problem mentioning {REVIEW_SKILLS[0]!r}, got {problems!r}"
    )


# --- _build_cli_command ----------------------------------------------------


def test_build_cli_command_claude_uses_auto_permission_mode() -> None:
    """Claude launches in `--permission-mode auto` so its classifier
    auto-approves the edits and `git`/`gh api …` bash the review needs,
    minimising mid-run permission prompts. Keep the test explicit so a
    future flag rename doesn't slip through."""
    cmd = _build_cli_command("claude", "prompt body")
    assert cmd == ["claude", "--permission-mode", "auto", "prompt body"]


def test_build_cli_command_codex_uses_sandbox_workspace_write() -> None:
    """Codex picks `--ask-for-approval never --sandbox workspace-write` plus
    `-c sandbox_workspace_write.network_access=true` — auto-approve edits
    inside the workspace and restore network so post-inline `gh api …`
    calls can reach GitHub. The more permissive `--yolo` is deliberately
    not used (would also remove the filesystem sandbox)."""
    cmd = _build_cli_command("codex", "prompt body")
    assert cmd == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "prompt body",
    ]


def test_build_cli_command_gemini_uses_auto_edit_approval_mode() -> None:
    """Gemini's `--approval-mode auto_edit` is the documented analogue of
    auto-approving edits while still gating risky operations. The
    deprecated `--yolo` / `-y` flag is intentionally avoided in favour of
    the modern equivalent."""
    cmd = _build_cli_command("gemini", "prompt body")
    assert cmd == ["gemini", "--approval-mode", "auto_edit", "prompt body"]


def test_build_cli_command_rejects_unknown_cli() -> None:
    """Defensive: CliChoice is a Literal, so this branch should never run
    in well-typed code. If someone widens the type without updating the
    switch, fail loud rather than emit a mystery argv."""
    with pytest.raises(ValueError, match="unknown CLI choice"):
        _build_cli_command("nonsense", "prompt body")  # type: ignore[arg-type]


# --- _first_available_cli --------------------------------------------------


def _only_installed(*installed: str):
    """Build a `shutil.which`-shaped stub that returns a path only for the
    listed binaries. Centralised so the test intent is the binary list,
    not the lambda shape, and so a future signature change (e.g. adding
    `mode=` / `path=`) lands in one place."""

    def fake_which(cmd: str, *_args, **_kwargs) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in installed else None

    return fake_which


def test_first_available_cli_returns_preferred_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("claude", "codex", "gemini"))
    assert _first_available_cli("claude") == "claude"
    assert _first_available_cli("codex") == "codex"
    assert _first_available_cli("gemini") == "gemini"


def test_first_available_cli_walks_cycle_when_preferred_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex-only user with `claude` persisted should land on codex —
    the next CLI in the cycle from `claude`."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("codex"))
    assert _first_available_cli("claude") == "codex"


def test_first_available_cli_skips_through_multiple_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted=claude, claude missing, codex missing, gemini installed
    → fall all the way through to gemini."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("gemini"))
    assert _first_available_cli("claude") == "gemini"


def test_first_available_cli_returns_none_when_all_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine 'no CLI installed' state — caller (`check_prereqs`) must
    treat this as a startup blocker."""
    monkeypatch.setattr(_mod.shutil, "which", _only_installed())
    assert _first_available_cli("claude") is None
    assert _first_available_cli("codex") is None
    assert _first_available_cli("gemini") is None


# --- check_prereqs ---------------------------------------------------------


class _RunOk:
    """Minimal stand-in for `subprocess.CompletedProcess` that satisfies
    `check_prereqs` (it only inspects `.returncode`). Lets us stub
    `cc_pr_reviewer.run` without dragging the full mock framework in."""

    returncode = 0


def _stub_prereq_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: tuple[str, ...],
    persisted: str = "claude",
) -> None:
    """Stub all the external-world calls `check_prereqs` makes so the
    test can pin the only inputs that matter (which binaries are on
    PATH, and which CLI is persisted)."""
    monkeypatch.setattr(
        _mod.shutil,
        "which",
        _only_installed("gh", "git", *installed),
    )
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunOk())
    monkeypatch.setattr(_mod, "_persisted_cli", lambda: persisted)


def test_check_prereqs_passes_with_only_codex_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug fix: a Codex-only user on a fresh install (persisted=claude
    by default) must be allowed to start the TUI. Previously the
    persisted=claude branch hard-failed on missing `claude`, leaving the
    user with no way to switch."""
    _stub_prereq_deps(monkeypatch, installed=("codex",), persisted="claude")
    assert check_prereqs() == []


def test_check_prereqs_passes_with_only_gemini_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric to the codex-only case — gemini-only install must also
    pass startup regardless of what's persisted."""
    _stub_prereq_deps(monkeypatch, installed=("gemini",), persisted="claude")
    assert check_prereqs() == []


def test_check_prereqs_fails_when_no_supported_cli_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh` and `git` present but none of the three review CLIs — the
    one case where startup genuinely can't proceed."""
    _stub_prereq_deps(monkeypatch, installed=(), persisted="claude")
    problems = check_prereqs()
    assert problems, "expected a startup blocker when no review CLI is installed"
    assert any("no supported review CLI" in p for p in problems)


def test_check_prereqs_fails_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh` is the data source — without it the TUI has nothing to show,
    so this stays a hard blocker independent of which CLI is selected."""
    # gh absent, codex present — CLI is fine, gh is the problem.
    monkeypatch.setattr(_mod.shutil, "which", _only_installed("git", "codex"))
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunOk())
    monkeypatch.setattr(_mod, "_persisted_cli", lambda: "codex")
    problems = check_prereqs()
    assert any("gh" in p and "not found" in p.lower() for p in problems)


# --- _parse_semver / _is_newer ---------------------------------------------


def test_parse_semver_concats_digits_within_each_segment() -> None:
    """Pragmatic, not PEP 440: the parser splits on '.' and scrapes
    digits within each segment, so a prerelease suffix is merged into
    the segment it sits on rather than producing a new tuple element.
    `'1.0.0a1'` → `(1, 0, 1)` (the 'a' is dropped, `0` and `1` join).
    Acceptable because PyPI's `info.version` is the latest stable
    release in practice; this just guards the parser shape so a future
    refactor doesn't silently change comparison results."""
    assert _parse_semver("1.2.3") == (1, 2, 3)
    assert _parse_semver("1.0.0a1") == (1, 0, 1)
    assert _parse_semver("0.10.0") == (0, 10, 0)


def test_is_newer_compares_segment_wise_not_lexically() -> None:
    """Regression guard: lexical compare would call '0.9.0' newer than
    '0.10.0'."""
    assert _is_newer("0.10.0", "0.9.0") is True
    assert _is_newer("0.9.0", "0.10.0") is False
    assert _is_newer("1.0.0", "1.0.0") is False


# --- _review_cell ----------------------------------------------------------


def _pr(repo: str, number: int, updated: str) -> dict[str, Any]:
    return {
        "repository": {"nameWithOwner": repo},
        "number": number,
        "updatedAt": updated,
    }


def test_review_cell_unreviewed_pr_renders_dash() -> None:
    cell = _review_cell(_pr("o/r", 1, "2025-01-01T00:00:00Z"), {})
    assert cell.plain == "-"


def test_review_cell_matching_updated_at_renders_count() -> None:
    state = {
        "o/r#1": {
            "count": 3,
            "last_pr_updated_at": "2025-01-01T00:00:00Z",
        }
    }
    cell = _review_cell(_pr("o/r", 1, "2025-01-01T00:00:00Z"), state)
    assert cell.plain == "3"


def test_review_cell_drifted_updated_at_marks_stale() -> None:
    """Any divergence in updatedAt — push, comment, label change — flips
    the cell to 'N stale'."""
    state = {
        "o/r#1": {
            "count": 3,
            "last_pr_updated_at": "2025-01-01T00:00:00Z",
        }
    }
    cell = _review_cell(_pr("o/r", 1, "2025-01-02T00:00:00Z"), state)
    assert cell.plain == "3 stale"
    assert "yellow" in str(cell.style)


def test_review_cell_missing_stored_updated_at_is_not_stale() -> None:
    """Defensive: if the row is missing the timestamp (legacy DB row, or
    empty string from a failed fetch), don't falsely flag stale."""
    state = {"o/r#1": {"count": 1, "last_pr_updated_at": ""}}
    cell = _review_cell(_pr("o/r", 1, "2025-01-02T00:00:00Z"), state)
    assert cell.plain == "1"


def test_review_cell_in_progress_overrides_count_with_glyph_and_style() -> None:
    """`in_progress=True` prepends the spinner glyph and styles the cell
    in bold yellow, while preserving the underlying count for context."""
    state = {
        "o/r#1": {
            "count": 3,
            "last_pr_updated_at": "2025-01-01T00:00:00Z",
        }
    }
    cell = _review_cell(_pr("o/r", 1, "2025-01-01T00:00:00Z"), state, in_progress=True)
    assert cell.plain.startswith("⟳ ")
    assert cell.plain.endswith("3")
    style = str(cell.style)
    assert "bold" in style
    assert "yellow" in style


def test_review_cell_in_progress_on_unreviewed_pr_keeps_dash() -> None:
    cell = _review_cell(_pr("o/r", 1, "2025-01-01T00:00:00Z"), {}, in_progress=True)
    assert cell.plain == "⟳ -"


def test_review_cell_returns_text_for_all_branches() -> None:
    """Regression guard for the str→Text return-type change."""
    from rich.text import Text

    pr = _pr("o/r", 1, "2025-01-01T00:00:00Z")
    state_match = {"o/r#1": {"count": 1, "last_pr_updated_at": "2025-01-01T00:00:00Z"}}
    state_stale = {"o/r#1": {"count": 1, "last_pr_updated_at": "2024-01-01T00:00:00Z"}}
    assert isinstance(_review_cell(pr, {}), Text)
    assert isinstance(_review_cell(pr, state_match), Text)
    assert isinstance(_review_cell(pr, state_stale), Text)
    assert isinstance(_review_cell(pr, {}, in_progress=True), Text)


# --- format_existing_comments ---------------------------------------------


def test_format_existing_comments_empty_returns_empty_block() -> None:
    assert format_existing_comments([]) == ("", 0)


def test_format_existing_comments_filters_unrenderable_entries() -> None:
    """Entries missing path/body/created_at can't render a useful dedup
    anchor and must be dropped silently."""
    bad = [
        _comment("alice", path=""),
        _comment("alice", body=""),
        _comment("alice", created_at=""),
    ]
    assert format_existing_comments(bad) == ("", 0)


def test_format_existing_comments_marks_outdated_when_line_is_null() -> None:
    """GitHub nulls `line` when a comment's anchor falls outside the
    current diff, but keeps `original_line`. We render with an
    '(outdated)' label so Claude doesn't suppress a legit new finding on
    a line that's been rewritten."""
    block, shown = format_existing_comments([_comment("alice", line=None, original_line=42)])
    assert shown == 1
    assert "a.py:42 (outdated)" in block


def test_format_existing_comments_truncates_long_body() -> None:
    """Bodies longer than EXISTING_COMMENT_BODY_CAP are sliced to
    `body[:CAP-1] + "…"`, so the rendered body is exactly CAP chars and
    ends with the marker. Asserting both protects against off-by-one
    regressions (dropping the `-1`, doubling the `…`, or accidentally
    truncating to CAP chars without the marker)."""
    long_body = "x" * (EXISTING_COMMENT_BODY_CAP * 3)
    block, _ = format_existing_comments([_comment("alice", body=long_body)])
    # The rendered line is `- @user on path:N — "<body>"`; pull the body
    # back out from between the surrounding quotes.
    m = re.search(r'"(.*)"', block)
    assert m is not None
    rendered_body = m.group(1)
    assert len(rendered_body) == EXISTING_COMMENT_BODY_CAP
    assert rendered_body.endswith("…")


def test_format_existing_comments_caps_list_at_recent_n() -> None:
    """Past EXISTING_COMMENT_LIST_CAP only the most-recent entries are
    kept, and a count footer is appended."""
    # Distribute timestamps across hours/minutes within a single valid
    # day so every `created_at` is parseable ISO 8601 regardless of how
    # the cap might grow in the future. Lexicographic sort still
    # descends correctly because all components are zero-padded.
    overflow = EXISTING_COMMENT_LIST_CAP + 5
    comments = [
        _comment(
            "alice",
            created_at=f"2025-01-01T{i // 60:02d}:{i % 60:02d}:00Z",
            body=f"c{i}",
        )
        for i in range(overflow)
    ]
    block, shown = format_existing_comments(comments)
    assert shown == EXISTING_COMMENT_LIST_CAP
    assert f"showing {EXISTING_COMMENT_LIST_CAP} most recent of {overflow} total" in block


# --- In-progress reservations ---------------------------------------------


@pytest.fixture
def review_db(tmp_path, monkeypatch):
    """Open a fresh review DB rooted at a tmp path. Patches
    `REVIEW_DB_PATH` so `_open_review_db` writes into the tmp dir, then
    closes the connection on teardown."""
    db_path = tmp_path / ".review_state.db"
    monkeypatch.setattr("cc_pr_reviewer.REVIEW_DB_PATH", db_path)
    conn = _open_review_db()
    try:
        yield conn
    finally:
        conn.close()


def _insert_holder(
    conn: sqlite3.Connection,
    pr_key: str,
    pid: int,
    hostname: str,
    started_at: str = "2025-01-01T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO reviews_in_progress (pr_key, pid, hostname, started_at) VALUES (?, ?, ?, ?)",
        (pr_key, pid, hostname, started_at),
    )
    conn.commit()


def test_record_launch_telemetry_inserts_row(review_db) -> None:
    """A launch appends exactly one fully-populated row, with bool flags
    coerced to 0/1 integers. This is the cost+outcome record the codegraph
    token thesis gets checked against, so the column values must round-trip
    faithfully."""
    _record_launch_telemetry(
        review_db,
        pr_key="o/r#7",
        cli="codex",
        codegraph_tools=True,
        affected_paths=12,
        existing_in_prompt=3,
        post_inline=True,
        rereview=False,
        approx_prompt_tokens=842,
        duration_seconds=4.5,
        exit_code=0,
    )
    rows = review_db.execute("SELECT * FROM review_telemetry").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["pr_key"] == "o/r#7"
    assert row["cli"] == "codex"
    assert row["codegraph_tools"] == 1  # bool coerced to int
    assert row["affected_paths"] == 12
    assert row["existing_in_prompt"] == 3
    assert row["post_inline"] == 1
    assert row["rereview"] == 0
    assert row["approx_prompt_tokens"] == 842
    assert row["duration_seconds"] == 4.5
    assert row["exit_code"] == 0
    assert row["recorded_at"].endswith("Z")


def test_record_launch_telemetry_records_nonzero_exit(review_db) -> None:
    """Aborts/crashes (rc != 0) must still land a row — otherwise the data
    over-represents clean runs and hides how often launches are
    interrupted. Two launches → two rows."""
    for rc in (0, 130):
        _record_launch_telemetry(
            review_db,
            pr_key="o/r#9",
            cli="claude",
            codegraph_tools=False,
            affected_paths=0,
            existing_in_prompt=0,
            post_inline=False,
            rereview=False,
            approx_prompt_tokens=100,
            duration_seconds=1.0,
            exit_code=rc,
        )
    exits = [r["exit_code"] for r in review_db.execute("SELECT exit_code FROM review_telemetry")]
    assert sorted(exits) == [0, 130]


def test_record_launch_telemetry_is_best_effort_on_db_error(capsys) -> None:
    """A telemetry insert failure must NOT raise — it runs after the review
    subprocess already finished, often inside the launch path's `finally`,
    so a propagated error would mask the real outcome. It must still be
    loud (a warning), not silent."""
    closed = sqlite3.connect(":memory:")
    closed.close()  # force "Cannot operate on a closed database"
    _record_launch_telemetry(
        closed,
        pr_key="o/r#1",
        cli="claude",
        codegraph_tools=False,
        affected_paths=0,
        existing_in_prompt=0,
        post_inline=False,
        rereview=False,
        approx_prompt_tokens=1,
        duration_seconds=0.1,
        exit_code=0,
    )
    assert "failed to record launch telemetry for o/r#1" in capsys.readouterr().out


def test_pid_alive_for_self_returns_true() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_for_unlikely_pid_returns_false() -> None:
    # 99999999 is well beyond Linux's default `/proc/sys/kernel/pid_max`
    # (typically 32768 or 4194304). Vanishingly unlikely to be live.
    # Skipped on Windows where _pid_alive is conservatively True for any
    # non-zero PID (no signal-0-style probe available there).
    if sys.platform == "win32":
        pytest.skip("Windows _pid_alive returns True for all positive PIDs by design")
    assert _pid_alive(99999999) is False


def test_pid_alive_for_invalid_pid_returns_false() -> None:
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_reserve_in_progress_inserts_row_when_empty(review_db) -> None:
    holder = _reserve_in_progress(review_db, "o/r#1")
    assert isinstance(holder, InProgressHolder)
    assert holder.pr_key == "o/r#1"
    assert holder.pid == os.getpid()
    assert holder.hostname == _APP_HOSTNAME
    assert _load_in_progress(review_db).keys() == {"o/r#1"}


def test_reserve_in_progress_raises_on_live_local_holder(review_db) -> None:
    _insert_holder(review_db, "o/r#1", os.getpid(), socket.gethostname())
    with pytest.raises(ReviewInProgressError) as exc_info:
        _reserve_in_progress(review_db, "o/r#1")
    err = exc_info.value
    assert err.holder.pid == os.getpid()
    assert err.holder.hostname == socket.gethostname()


def test_reserve_in_progress_raises_on_foreign_host(review_db) -> None:
    """Foreign-host rows are opaque (we can't probe a remote PID over
    NFS), so we must NOT treat them as stale even with an unlikely PID."""
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    with pytest.raises(ReviewInProgressError) as exc_info:
        _reserve_in_progress(review_db, "o/r#1")
    assert exc_info.value.holder.hostname == "some-other-host"


def test_reserve_in_progress_recovers_stale_own_host_row(review_db) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows _pid_alive can't detect dead PIDs (always True)")
    _insert_holder(review_db, "o/r#1", 99999999, _APP_HOSTNAME)
    holder = _reserve_in_progress(review_db, "o/r#1")
    assert holder.pid == os.getpid()
    live = _load_in_progress(review_db)
    assert live["o/r#1"].pid == os.getpid()


def test_reserve_in_progress_expected_holder_replaces_matching_holder(review_db) -> None:
    """The override path: user saw holder H in the warn modal and chose
    to review anyway. Reserve must replace H's row only if the row's
    identity still matches H — protecting against the modal-open →
    modal-confirm race."""
    _insert_holder(review_db, "o/r#1", 4242, "some-other-host", "2025-01-01T00:00:00Z")
    expected = InProgressHolder(
        pr_key="o/r#1", pid=4242, hostname="some-other-host", started_at="2025-01-01T00:00:00Z"
    )
    holder = _reserve_in_progress(review_db, "o/r#1", expected_holder=expected)
    assert holder.pid == os.getpid()
    assert holder.hostname == _APP_HOSTNAME


def test_reserve_in_progress_expected_holder_refuses_when_holder_changed(
    review_db,
) -> None:
    """If H finished and a different peer B reserved between modal-open
    and modal-confirm, the override must NOT silently evict B. It must
    raise so the caller can re-prompt against the new holder."""
    _insert_holder(review_db, "o/r#1", 5555, "another-host", "2025-01-02T00:00:00Z")
    stale_expected = InProgressHolder(
        pr_key="o/r#1", pid=4242, hostname="some-other-host", started_at="2025-01-01T00:00:00Z"
    )
    with pytest.raises(ReviewInProgressError) as exc_info:
        _reserve_in_progress(review_db, "o/r#1", expected_holder=stale_expected)
    # Surfaces the *current* (B's) identity, not the expected (H's).
    assert exc_info.value.holder.pid == 5555
    assert exc_info.value.holder.hostname == "another-host"


def test_release_in_progress_only_deletes_own_row(review_db) -> None:
    """A peer's row (different PID) must survive our release — the
    `(pid, hostname)` guards prevent ever deleting another tab's marker."""
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    _release_in_progress(review_db, "o/r#1")
    live = _load_in_progress(review_db)
    assert "o/r#1" in live
    assert live["o/r#1"].pid == 99999999


def test_release_in_progress_is_idempotent(review_db) -> None:
    """Releasing a row that doesn't exist (already-released, never-reserved)
    must not raise — release must never tank the post-review path."""
    _release_in_progress(review_db, "o/r#1")  # no-op, no row yet
    _reserve_in_progress(review_db, "o/r#1")
    _release_in_progress(review_db, "o/r#1")
    _release_in_progress(review_db, "o/r#1")  # second release is no-op


def test_release_in_progress_swallows_db_errors(tmp_path, monkeypatch, capsys) -> None:
    """Pin the `release-must-not-raise` contract against a closed
    connection. A failure logs to stderr (so orphaned reservations are
    diagnosable) but never propagates."""
    monkeypatch.setattr("cc_pr_reviewer.REVIEW_DB_PATH", tmp_path / ".review_state.db")
    conn = _open_review_db()
    conn.close()
    _release_in_progress(conn, "o/r#1")  # must not raise
    captured = capsys.readouterr()
    assert "failed to release in-progress reservation" in captured.err


def test_load_in_progress_sweeps_stale_own_host_rows(review_db) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows _pid_alive can't detect dead PIDs (always True)")
    _insert_holder(review_db, "o/r#1", 99999999, _APP_HOSTNAME)
    live = _load_in_progress(review_db)
    assert live == {}
    # Sweep is persistent: row should be gone from the table.
    rows = review_db.execute("SELECT * FROM reviews_in_progress").fetchall()
    assert rows == []


def test_load_in_progress_sweep_includes_pid_in_where(review_db) -> None:
    """Same-host crash-and-restart race: peer A (pid 99999999) crashes,
    then peer B has already swept-and-replaced with its own row (pid
    66666666). A subsequent sweep that built its DELETE list from the
    older snapshot must NOT match B's fresh row — `pid` is the
    discriminator that prevents the wipe."""
    if sys.platform == "win32":
        pytest.skip("Windows _pid_alive can't detect dead PIDs (always True)")
    fresh_pid = os.getpid()  # guaranteed live: ourselves
    _insert_holder(review_db, "o/r#1", fresh_pid, _APP_HOSTNAME)
    # The sweep would only consider the row stale if its pid is dead;
    # since it isn't, it must survive. We assert the SELECT contract,
    # not the executemany internals — but a regression that drops pid
    # from the DELETE WHERE would still make the sweep delete the live
    # row whenever any stale row coexists. Force that scenario:
    _insert_holder(review_db, "o/r#2", 99999999, _APP_HOSTNAME)
    live = _load_in_progress(review_db)
    assert "o/r#1" in live
    assert "o/r#2" not in live
    rows = {r["pr_key"] for r in review_db.execute("SELECT pr_key FROM reviews_in_progress")}
    assert rows == {"o/r#1"}


def test_load_in_progress_keeps_foreign_host_rows(review_db) -> None:
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    live = _load_in_progress(review_db)
    assert live.keys() == {"o/r#1"}


def test_concurrent_reserve_one_winner_one_raises(tmp_path, monkeypatch) -> None:
    """The central cross-instance invariant: two independent connections
    racing to reserve the same `pr_key` must produce exactly one winner
    and one `ReviewInProgressError`. Same-process; same-DB-file; two
    distinct connections — exactly the multi-tab topology in production.
    Without this test, a regression to `INSERT OR REPLACE` (which would
    silently let the second writer win) would pass every other test."""
    monkeypatch.setattr("cc_pr_reviewer.REVIEW_DB_PATH", tmp_path / ".review_state.db")
    a = _open_review_db()
    b = _open_review_db()
    try:
        _reserve_in_progress(a, "o/r#1")
        with pytest.raises(ReviewInProgressError):
            _reserve_in_progress(b, "o/r#1")
        # Second connection still sees A's row.
        assert _load_in_progress(b).keys() == {"o/r#1"}
    finally:
        a.close()
        b.close()


def test_open_review_db_creates_in_progress_table_on_legacy_db(tmp_path, monkeypatch) -> None:
    """0.10.x users have a DB with only `reviews` + `settings`. Opening
    it on this branch must create `reviews_in_progress` lazily — that's
    the only "migration" mechanism users see."""
    db_path = tmp_path / ".review_state.db"
    monkeypatch.setattr("cc_pr_reviewer.REVIEW_DB_PATH", db_path)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE reviews ("
        "pr_key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0,"
        "last_reviewed_at TEXT NOT NULL, last_pr_updated_at TEXT NOT NULL,"
        "last_head_sha TEXT)"
    )
    legacy.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    legacy.commit()
    legacy.close()

    conn = _open_review_db()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reviews_in_progress" in tables
        # Functional smoke: the new table is usable end-to-end.
        _reserve_in_progress(conn, "o/r#1")
        assert _load_in_progress(conn).keys() == {"o/r#1"}
    finally:
        conn.close()


def test_in_progress_age_str_formats_short_ages() -> None:
    """Coarse age formatting; covers the four magnitude buckets without
    being flaky on tiny clock jitter."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda d: (now - d).isoformat().replace("+00:00", "Z")  # noqa: E731
    assert _in_progress_age_str(iso(timedelta(seconds=10))).endswith("s ago")
    assert _in_progress_age_str(iso(timedelta(minutes=5))).endswith("m ago")
    assert _in_progress_age_str(iso(timedelta(hours=2))).endswith("h ago")
    assert _in_progress_age_str(iso(timedelta(days=3))).endswith("d ago")


def test_in_progress_age_str_clamps_future_timestamps_to_zero() -> None:
    """Foreign-host clock skew can produce a `started_at` in our future.
    The `max(secs, 0)` clamp keeps the renderer from emitting "-43s ago"
    (which would be both nonsensical and a UX bug)."""
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(seconds=42)).isoformat().replace("+00:00", "Z")
    assert _in_progress_age_str(future) == "0s ago"


def test_in_progress_age_str_falls_back_on_unparseable_input() -> None:
    assert _in_progress_age_str("not-an-iso-string") == "not-an-iso-string"


def test_handle_poll_error_preserves_snapshot_and_dedupes_toast() -> None:
    """A failed poll must NOT clear the in-memory snapshot. Clearing it
    would leave the `⟳` glyph painted on cells (the worker can't repaint
    without a working DB) while `action_review`'s gate read empty,
    silently letting the user launch a duplicate review against a peer
    we still believe holds the row. Keeping the snapshot intact means
    cell + gate stay consistent — both stale, but consistent — and the
    toast tells the user the snapshot is stale.

    Dedupe latch: a persistent failure must fire the toast at most once
    until the next successful poll clears the latch.
    """
    from cc_pr_reviewer import PRReviewer

    holder = InProgressHolder(
        pr_key="o/r#1", pid=100, hostname="peer-host", started_at="2025-01-01T00:00:00Z"
    )
    snapshot = {"o/r#1": holder}

    class Dummy:
        def __init__(self) -> None:
            self._in_progress = snapshot
            self._poll_error_shown = False
            self.notifies: list[str] = []

        def notify(self, message: str, **_kw: Any) -> None:
            self.notifies.append(message)

    d = Dummy()
    PRReviewer._handle_poll_error(d, "boom")  # type: ignore[arg-type]
    PRReviewer._handle_poll_error(d, "boom")  # type: ignore[arg-type]
    PRReviewer._handle_poll_error(d, "boom")  # type: ignore[arg-type]
    # Snapshot survived all three error ticks (same object identity).
    assert d._in_progress is snapshot
    assert d._in_progress == {"o/r#1": holder}
    # Toast fired exactly once thanks to the dedupe latch.
    assert d.notifies == ["In-progress poll failed: boom"]
    assert d._poll_error_shown is True


# --- auto-refresh (issue #49) ----------------------------------------------


def test_new_review_pr_keys_empty_previous_reports_all() -> None:
    """First-load scenario: with nothing seen yet, every review PR is 'new'.
    (The caller suppresses the toast on first load; the helper still reports
    them so the baseline can be seeded.)"""
    prs = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    assert new_review_pr_keys(set(), prs) == {"o/r#1", "o/r#2"}


def test_new_review_pr_keys_steady_state_reports_none() -> None:
    """A tick that surfaces only already-seen PRs returns nothing — no
    re-notify spam every interval."""
    prs = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    assert new_review_pr_keys({"o/r#1", "o/r#2"}, prs) == set()


def test_new_review_pr_keys_reports_only_the_addition() -> None:
    prs = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    assert new_review_pr_keys({"o/r#1"}, prs) == {"o/r#2"}


def test_new_review_pr_keys_excludes_mine_rows() -> None:
    """A newly-appearing `_mine=True` row is not a 'new review request' —
    flipping the `m` toggle must never manufacture a phantom alert."""
    mine = _pr("o/r", 9, "2025-01-03T00:00:00Z")
    mine["_mine"] = True
    prs = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), mine]
    assert new_review_pr_keys({"o/r#1"}, prs) == set()


def test_new_review_pr_keys_ignores_removals() -> None:
    """A PR present in `previous` but gone from the current list (merged/
    closed) is not reported — we only flag additions."""
    prs = [_pr("o/r", 1, "2025-01-01T00:00:00Z")]
    assert new_review_pr_keys({"o/r#1", "o/r#2"}, prs) == set()


def test_new_review_pr_keys_distinguishes_repos_with_same_number() -> None:
    prs = [_pr("o/a", 1, "2025-01-01T00:00:00Z"), _pr("o/b", 1, "2025-01-01T00:00:00Z")]
    assert new_review_pr_keys({"o/a#1"}, prs) == {"o/b#1"}


# --- _worktree_path --------------------------------------------------------


def test_worktree_path_layout() -> None:
    assert _worktree_path("o", "n", 42) == WORKSPACE / ".worktrees" / "o" / "n" / "42"


def test_worktree_path_distinct_per_number() -> None:
    """The core isolation invariant: each PR number maps to its own
    worktree, so two parallel reviews of different PRs never collide."""
    assert _worktree_path("o", "n", 1) != _worktree_path("o", "n", 2)


def test_worktree_path_separates_from_clone_namespace() -> None:
    """The worktree must live OUTSIDE the `owner/name` primary-clone path,
    so `primary_path.exists()` clone-vs-fetch detection can't be tripped by
    a worktree dir."""
    primary = WORKSPACE / "o" / "n"
    wt = _worktree_path("o", "n", 1)
    assert wt != primary
    assert primary not in wt.parents


# --- _seed_worktree_codegraph ----------------------------------------------


def _make_primary_index(primary: Path) -> None:
    """Stand up a minimal `.codegraph/` like `codegraph init` would."""
    cg = primary / ".codegraph"
    cg.mkdir(parents=True)
    (cg / "codegraph.db").write_bytes(b"DBDATA")
    (cg / "codegraph.db-wal").write_bytes(b"WALDATA")
    (cg / "codegraph.db-shm").write_bytes(b"SHMDATA")
    (cg / ".gitignore").write_text("*.db\n")
    (cg / "daemon.log").write_text("noise\n")


def test_seed_worktree_codegraph_missing_source_returns_false(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert _seed_worktree_codegraph(primary, worktree) is False
    assert not (worktree / ".codegraph").exists()


def test_seed_worktree_codegraph_copies_db_and_wal(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _make_primary_index(primary)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert _seed_worktree_codegraph(primary, worktree) is True
    dst = worktree / ".codegraph"
    assert (dst / "codegraph.db").read_bytes() == b"DBDATA"
    assert (dst / "codegraph.db-wal").read_bytes() == b"WALDATA"
    assert (dst / ".gitignore").read_text() == "*.db\n"
    # `-shm` is rebuilt by SQLite, and daemon state is per-process — neither
    # should be copied into the worktree.
    assert not (dst / "codegraph.db-shm").exists()
    assert not (dst / "daemon.log").exists()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3600", 3600),
        ("5400", 5400),
        ("", _mod._DEFAULT_REFRESH_SECS),
        ("abc", _mod._DEFAULT_REFRESH_SECS),
        ("0", 0),
        ("-5", 0),
        ("10", 60),  # clamped to the 60s floor
        ("60", 60),
    ],
)
def test_parse_refresh_interval(raw: str, expected: int) -> None:
    assert parse_refresh_interval(raw) == expected


def test_parse_refresh_interval_honours_custom_default() -> None:
    assert parse_refresh_interval("nonsense", default=900) == 900


@pytest.mark.parametrize(
    "secs,label",
    [(0, "off"), (-1, "off"), (900, "15m"), (1800, "30m"), (3600, "1h"), (90, "90s")],
)
def test_refresh_interval_label(secs: int, label: str) -> None:
    assert _refresh_interval_label(secs) == label


# --- format_title (header is title-only; no subtitle) ----------------------


def test_format_title_renders_title_only() -> None:
    """The header shows just `CC PR Reviewer [count]` — no subtitle text."""
    content = _mod.PRReviewer.format_title(None, "CC PR Reviewer [3]", "")  # type: ignore[arg-type]
    assert content.plain == "CC PR Reviewer [3]"


def test_format_title_ignores_any_subtitle() -> None:
    """Even a non-empty sub_title is dropped — locks the no-subtitle contract."""
    content = _mod.PRReviewer.format_title(  # type: ignore[arg-type]
        None, "CC PR Reviewer [3]", "Review Github PRs · mine"
    )
    assert content.plain == "CC PR Reviewer [3]"


# --- footer active-highlight predicate -------------------------------------


class _FooterStateFake:
    """Minimal stand-in carrying just the toggle state `_footer_action_active`
    reads — mirrors the unbound-method dummy-harness pattern used elsewhere."""

    def __init__(self, *, include_mine: bool, group_by: str, sort_by: str) -> None:
        self.include_mine = include_mine
        self.group_by = group_by
        self.sort_by = sort_by


@pytest.mark.parametrize(
    "state,action,expected",
    [
        # Each toggle lights up only when its own state is non-default.
        ({"include_mine": True, "group_by": "", "sort_by": ""}, "toggle_mine", True),
        ({"include_mine": False, "group_by": "", "sort_by": ""}, "toggle_mine", False),
        ({"include_mine": False, "group_by": "repo", "sort_by": ""}, "toggle_group", True),
        ({"include_mine": False, "group_by": "", "sort_by": ""}, "toggle_group", False),
        ({"include_mine": False, "group_by": "", "sort_by": "updated"}, "toggle_sort", True),
        ({"include_mine": False, "group_by": "", "sort_by": ""}, "toggle_sort", False),
        # Unrelated / non-toggle footer keys never highlight.
        ({"include_mine": True, "group_by": "repo", "sort_by": "updated"}, "refresh", False),
    ],
)
def test_footer_action_active(state: dict[str, Any], action: str, expected: bool) -> None:
    fake = _FooterStateFake(**state)
    assert _mod.PRReviewer._footer_action_active(fake, action) is expected  # type: ignore[arg-type]


# --- auto-refresh Settings dropdown (regression: PR #56 P0 crash) -----------


def test_refresh_options_match_cycle() -> None:
    """The dropdown's options are exactly the cycle's reachable values.

    Pins `_REFRESH_OPTIONS` so it can't silently drift from `_REFRESH_CYCLE`,
    which is the invariant the Settings `Select` depends on for its values.
    """
    assert _REFRESH_OPTIONS == (0, 900, 1800, 3600)
    assert set(_REFRESH_OPTIONS) == set(_REFRESH_CYCLE) | set(_REFRESH_CYCLE.values())


@pytest.mark.parametrize("interval", [300, 120, 7200, 60])
def test_settings_screen_mounts_with_offcycle_refresh(interval: int) -> None:
    """An off-cycle `refresh_interval` must not crash the Settings modal.

    `parse_refresh_interval` tolerates hand-edited/legacy values (floored at
    60s), so a persisted `300` is a legal session value — but feeding it
    straight into `Select(allow_blank=False)` raises `InvalidSelectValueError`
    on mount, taking the app down on `,` with no way back in to fix it. The
    modal snaps off-cycle values to a legal option; assert it mounts cleanly.
    """
    import asyncio

    from textual.app import App
    from textual.widgets import Select

    async def _run() -> int:
        app: App = App()
        async with app.run_test() as pilot:
            # Mounting the modal is the regression site: an off-cycle value
            # would raise InvalidSelectValueError here.
            await app.push_screen(
                SettingsScreen(
                    cli="claude",
                    codegraph_assist=False,
                    refresh_interval=interval,
                    slack_webhook_url="",
                    theme=DEFAULT_THEME,
                )
            )
            await pilot.pause()
            return app.screen.query_one("#settings-refresh", Select).value

    # No exception == the regression is fixed; the snapped value is legal.
    assert asyncio.run(_run()) in _REFRESH_OPTIONS


class _NotifyFake:
    """Minimal stand-in for `PRReviewer` exercising `_maybe_notify_new_prs`
    without any widget access — mirrors the dummy-harness pattern used by
    `test_handle_poll_error_preserves_snapshot_and_dedupes_toast`."""

    def __init__(self, seen: set[str] | None = None, first_load_done: bool = False) -> None:
        self._seen_review_keys: set[str] = seen if seen is not None else set()
        self._first_load_done = first_load_done
        self.notifies: list[str] = []
        self.bells = 0

    def notify(self, message: str, **_kw: Any) -> None:
        self.notifies.append(message)

    def bell(self) -> None:
        self.bells += 1


def _maybe_notify(fake: _NotifyFake, data: list[dict[str, Any]], auto: bool) -> bool:
    from cc_pr_reviewer import PRReviewer

    return PRReviewer._maybe_notify_new_prs(fake, data, auto)  # type: ignore[arg-type]


def test_maybe_notify_first_load_suppresses_and_seeds_baseline() -> None:
    """First populate never toasts (every PR would look new) but must seed
    the snapshot so the *next* tick has a baseline to diff against."""
    fake = _NotifyFake(first_load_done=False)
    data = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    fired = _maybe_notify(fake, data, auto=True)
    assert fired is False
    assert fake.notifies == []
    assert fake.bells == 0
    assert fake._seen_review_keys == {"o/r#1", "o/r#2"}
    assert fake._first_load_done is True


def test_maybe_notify_auto_tick_fires_once_for_new_pr() -> None:
    fake = _NotifyFake(seen={"o/r#1"}, first_load_done=True)
    data = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    fired = _maybe_notify(fake, data, auto=True)
    assert fired is True
    assert fake.notifies == ["1 new PR awaiting your review"]
    assert fake.bells == 1
    # Baseline now includes the freshly-seen PR…
    assert fake._seen_review_keys == {"o/r#1", "o/r#2"}
    # …so a steady-state re-tick with the same data is silent.
    fake.notifies.clear()
    fake.bells = 0
    assert _maybe_notify(fake, data, auto=True) is False
    assert fake.notifies == []
    assert fake.bells == 0


def test_maybe_notify_pluralises_multiple_new_prs() -> None:
    fake = _NotifyFake(seen=set(), first_load_done=True)
    data = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    assert _maybe_notify(fake, data, auto=True) is True
    assert fake.notifies == ["2 new PRs awaiting your review"]


def test_maybe_notify_manual_populate_is_silent_but_rebaselines() -> None:
    """A non-auto populate (manual `r`, `m`/filter toggle) never toasts, but
    still refreshes the snapshot so a later auto tick diffs against it."""
    fake = _NotifyFake(seen={"o/r#1"}, first_load_done=True)
    data = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), _pr("o/r", 2, "2025-01-02T00:00:00Z")]
    fired = _maybe_notify(fake, data, auto=False)
    assert fired is False
    assert fake.notifies == []
    assert fake.bells == 0
    assert fake._seen_review_keys == {"o/r#1", "o/r#2"}


def test_maybe_notify_ignores_mine_only_additions() -> None:
    fake = _NotifyFake(seen={"o/r#1"}, first_load_done=True)
    mine = _pr("o/r", 9, "2025-01-03T00:00:00Z")
    mine["_mine"] = True
    data = [_pr("o/r", 1, "2025-01-01T00:00:00Z"), mine]
    fired = _maybe_notify(fake, data, auto=True)
    assert fired is False
    assert fake.notifies == []
    # `_mine` rows are excluded from the baseline too.
    assert fake._seen_review_keys == {"o/r#1"}


# --- Slack review notifications --------------------------------------------


def test_build_slack_payload_approved_uses_handles_and_url() -> None:
    payload = build_slack_payload(
        repo="o/r",
        number=7,
        title="Add widget",
        url="https://github.com/o/r/pull/7",
        author_login="alice",
        reviewer_login="bob",
        state="APPROVED",
    )
    text = payload["text"]
    assert "@bob" in text and "approved" in text
    assert "o/r#7: Add widget" in text
    assert "@alice" in text
    assert "https://github.com/o/r/pull/7" in text


@pytest.mark.parametrize(
    ("state", "phrase"),
    [
        ("APPROVED", "approved"),
        ("CHANGES_REQUESTED", "requested changes on"),
        ("COMMENTED", "left comments on"),
    ],
)
def test_build_slack_payload_verdict_phrasing(state: str, phrase: str) -> None:
    payload = build_slack_payload(
        repo="o/r",
        number=1,
        title="t",
        url="u",
        author_login="a",
        reviewer_login="b",
        state=state,
    )
    assert phrase in payload["text"]


def test_build_slack_payload_unknown_state_still_notifies() -> None:
    payload = build_slack_payload(
        repo="o/r",
        number=1,
        title="t",
        url="u",
        author_login="a",
        reviewer_login="b",
        state="DISMISSED",
    )
    # Unknown states fall back to a generic line rather than being dropped.
    assert "reviewed (dismissed)" in payload["text"]


def test_build_slack_payload_missing_logins_degrade_gracefully() -> None:
    payload = build_slack_payload(
        repo="o/r",
        number=1,
        title="t",
        url="u",
        author_login=None,
        reviewer_login=None,
        state="APPROVED",
    )
    text = payload["text"]
    assert "A reviewer" in text
    assert "the author" in text
    assert "@" not in text


def test_slack_webhook_setting_round_trips_and_defaults_empty(review_db) -> None:
    # Unset → empty (feature off).
    assert _get_setting(review_db, "slack_webhook_url", "") == ""
    url = "https://hooks.slack.com/services/T/B/x"
    _set_setting(review_db, "slack_webhook_url", url)
    assert _get_setting(review_db, "slack_webhook_url", "") == url
    # Clearing it turns the feature back off.
    _set_setting(review_db, "slack_webhook_url", "")
    assert _get_setting(review_db, "slack_webhook_url", "") == ""


def test_theme_setting_round_trips_and_defaults(review_db) -> None:
    # Unset → the configured default.
    assert _get_setting(review_db, "theme", DEFAULT_THEME) == DEFAULT_THEME
    _set_setting(review_db, "theme", "nord")
    assert _get_setting(review_db, "theme", DEFAULT_THEME) == "nord"


def test_resolve_theme_passes_known_falls_back_on_unknown() -> None:
    # Every offered theme resolves to itself.
    for name in _THEME_OPTIONS:
        assert _resolve_theme(name) == name
    # The default is itself a known theme.
    assert DEFAULT_THEME in _THEME_OPTIONS
    # Unknown / hand-edited / blank → default (never raises).
    assert _resolve_theme("no-such-theme") == DEFAULT_THEME
    assert _resolve_theme("") == DEFAULT_THEME


def test_branded_theme_is_offered_and_default() -> None:
    # The custom theme's object name matches its constant, leads the picker,
    # and is the out-of-the-box default.
    assert CLAUDE_THEME.name == CLAUDE_THEME_NAME
    assert CLAUDE_THEME_NAME in _THEME_OPTIONS
    assert DEFAULT_THEME == CLAUDE_THEME_NAME


def test_build_slack_payload_escapes_mrkdwn_special_chars_in_title() -> None:
    payload = build_slack_payload(
        repo="o/r",
        number=1,
        title="Fix <Foo> & <Bar>",
        url="u",
        author_login="a",
        reviewer_login="b",
        state="APPROVED",
    )
    text = payload["text"]
    assert "Fix &lt;Foo&gt; &amp; &lt;Bar&gt;" in text
    # The raw forms must not survive.
    assert "<Foo>" not in text and " & " not in text


class _RunReviews:
    """Stub `cc_pr_reviewer.run` result carrying a crafted reviews payload."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _reviews_json(*rows: dict[str, Any]) -> str:
    return json.dumps(list(rows))


def test_fetch_my_latest_review_selects_last_matching_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _reviews_json(
        {"id": 1, "user": {"login": "me"}, "state": "COMMENTED", "submitted_at": "t1"},
        {"id": 2, "user": {"login": "other"}, "state": "APPROVED", "submitted_at": "t2"},
        {"id": 3, "user": {"login": "me"}, "state": "APPROVED", "submitted_at": "t3"},
    )
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunReviews(stdout=payload))
    got, ok = fetch_my_latest_review("o/r", 7, "me")
    # Last chronological row authored by "me" wins; "other" is excluded.
    assert ok is True
    assert got == {"id": 3, "state": "APPROVED", "submitted_at": "t3"}


def test_fetch_my_latest_review_ignores_non_verdict_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _reviews_json(
        {"id": 1, "user": {"login": "me"}, "state": "COMMENTED", "submitted_at": "t1"},
        {"id": 2, "user": {"login": "me"}, "state": "DISMISSED", "submitted_at": "t2"},
        {"id": 3, "user": {"login": "me"}, "state": "PENDING", "submitted_at": "t3"},
    )
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunReviews(stdout=payload))
    got, ok = fetch_my_latest_review("o/r", 7, "me")
    # DISMISSED/PENDING are skipped, so the COMMENTED row remains latest.
    assert ok is True
    assert got is not None and got["id"] == 1


def test_fetch_my_latest_review_no_match_is_ok_with_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _reviews_json(
        {"id": 1, "user": {"login": "other"}, "state": "APPROVED", "submitted_at": "t1"},
    )
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunReviews(stdout=payload))
    # Clean fetch, no review by us: ok=True so the caller treats it as a real
    # "no prior review" baseline, not "couldn't tell".
    assert fetch_my_latest_review("o/r", 7, "me") == (None, True)


def test_fetch_my_latest_review_empty_login_is_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_cmd: Any, **_kw: Any) -> Any:
        raise AssertionError("run() should not be called for an empty login")

    monkeypatch.setattr(_mod, "run", _boom)
    assert fetch_my_latest_review("o/r", 7, "") == (None, False)


def test_fetch_my_latest_review_api_failure_is_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_mod, "run", lambda _cmd, **_kw: _RunReviews(returncode=1, stderr="boom"))
    assert fetch_my_latest_review("o/r", 7, "me") == (None, False)


def test_fetch_my_latest_review_non_list_payload_is_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _mod, "run", lambda _cmd, **_kw: _RunReviews(stdout='{"message": "Not Found"}')
    )
    assert fetch_my_latest_review("o/r", 7, "me") == (None, False)


def _notify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    post: dict[str, Any] | None,
    post_ok: bool = True,
    pre_review: dict[str, Any] | None,
    pre_review_ok: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    """Invoke `_notify_review_to_slack` with stubbed I/O; return posted calls.

    `post`/`post_ok` are what the (stubbed) post-session `fetch_my_latest_review`
    returns; `pre_review`/`pre_review_ok` are the baseline passed in by the
    caller.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(_mod, "fetch_my_latest_review", lambda *_a, **_k: (post, post_ok))
    monkeypatch.setattr(
        _mod, "_post_slack_webhook", lambda url, payload: posted.append((url, payload))
    )
    pr = {"title": "t", "url": "u", "author": {"login": "alice"}}
    # The method touches no instance state, so a bare object stands in for self.
    _mod.PRReviewer._notify_review_to_slack(
        object(),
        webhook_url="https://hook",
        pr=pr,
        repo="o/r",
        number=7,
        reviewer_login="me",
        pre_review=pre_review,
        pre_review_ok=pre_review_ok,
    )
    return posted


def test_notify_posts_when_new_review_id_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _notify(
        monkeypatch,
        post={"id": 2, "state": "APPROVED", "submitted_at": "t2"},
        pre_review={"id": 1, "state": "COMMENTED", "submitted_at": "t1"},
    )
    assert len(posted) == 1
    assert "approved" in posted[0][1]["text"]


def test_notify_posts_when_no_prior_review(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _notify(
        monkeypatch,
        post={"id": 5, "state": "CHANGES_REQUESTED", "submitted_at": "t5"},
        pre_review=None,
    )
    assert len(posted) == 1


def test_notify_stays_quiet_when_id_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _notify(
        monkeypatch,
        post={"id": 1, "state": "APPROVED", "submitted_at": "t1"},
        pre_review={"id": 1, "state": "APPROVED", "submitted_at": "t1"},
    )
    assert posted == []


def test_notify_stays_quiet_when_no_review_found(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _notify(monkeypatch, post=None, pre_review=None)
    assert posted == []


def test_notify_stays_quiet_when_post_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Post-session lookup failed (post_ok=False): we can't confirm a verdict,
    # so nothing is announced even though a stale dict might be present.
    posted = _notify(
        monkeypatch,
        post=None,
        post_ok=False,
        pre_review=None,
    )
    assert posted == []


def test_notify_stays_quiet_when_baseline_unreliable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pre-session baseline couldn't be established (pre_review_ok=False),
    # e.g. a transient API blip. Even though a pre-existing review is found
    # post-session, we must NOT announce it as new.
    posted = _notify(
        monkeypatch,
        post={"id": 99, "state": "APPROVED", "submitted_at": "t99"},
        pre_review=None,
        pre_review_ok=False,
    )
    assert posted == []


def test_notify_stays_quiet_on_dismiss_revealing_older_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Baseline was the newest verdict (id=10, t10). It gets dismissed mid-session,
    # so an older surviving review (id=5, t5) becomes "latest" — different id but
    # an earlier timestamp, which must NOT trigger a spurious notification.
    posted = _notify(
        monkeypatch,
        post={"id": 5, "state": "COMMENTED", "submitted_at": "t05"},
        pre_review={"id": 10, "state": "APPROVED", "submitted_at": "t10"},
    )
    assert posted == []
