"""Tests for the pure helpers in cc_pr_reviewer.

Scope is deliberately narrow: only the I/O-free functions that gate
launch behavior. The TUI, subprocess flow, and gh-CLI shellouts are
intentionally out of scope here; they're better covered by integration
tests.
"""

from __future__ import annotations

import os
import re
import socket
import sqlite3
from typing import Any

import pytest

from cc_pr_reviewer import (
    EXISTING_COMMENT_BODY_CAP,
    EXISTING_COMMENT_LIST_CAP,
    POST_INLINE_DEDUP_SUFFIX,
    POST_INLINE_FETCH_FAILED_SUFFIX,
    POST_INLINE_PROMPT,
    POST_INLINE_REREVIEW_APPROVE_SUFFIX,
    POST_INLINE_REREVIEW_RESOLVE_SUFFIX,
    POST_INLINE_REREVIEW_SUFFIX,
    PROMPT_SECTION_SEP,
    REVIEW_PROMPT,
    ReviewInProgressError,
    _in_progress_age_str,
    _is_newer,
    _load_in_progress,
    _open_review_db,
    _parse_semver,
    _pid_alive,
    _release_in_progress,
    _reserve_in_progress,
    _review_cell,
    build_review_prompt,
    format_existing_comments,
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


def test_plain_review_equals_base_prompt() -> None:
    """post_inline=False with no extras returns REVIEW_PROMPT verbatim."""
    built = build_review_prompt(
        post_inline=False,
        extra_prompt="",
        existing=[],
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    assert built.text == REVIEW_PROMPT
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


def test_full_chain_assembles_in_canonical_order() -> None:
    """Lock the canonical assembly for a representative non-trivial
    composition. The other tests use `in`/`not in` against `built.text`
    and would let through regressions that swap `PROMPT_SECTION_SEP`
    for `\\n`, duplicate a section, or reorder sections — exactly the
    kind of bug suffix-matrix changes are most likely to introduce."""
    existing = [_comment("alice")]
    built = build_review_prompt(
        post_inline=True,
        extra_prompt="x",
        existing=existing,
        fetch_ok=True,
        my_login="alice",
        author_login="bob",
    )
    expected = PROMPT_SECTION_SEP.join(
        [
            REVIEW_PROMPT,
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


def test_pid_alive_for_self_returns_true() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_for_unlikely_pid_returns_false() -> None:
    # 99999999 is well beyond Linux's default `/proc/sys/kernel/pid_max`
    # (typically 32768 or 4194304). Vanishingly unlikely to be live.
    assert _pid_alive(99999999) is False


def test_pid_alive_for_invalid_pid_returns_false() -> None:
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_reserve_in_progress_inserts_row_when_empty(review_db) -> None:
    row = _reserve_in_progress(review_db, "o/r#1")
    assert row["pr_key"] == "o/r#1"
    assert row["pid"] == os.getpid()
    assert row["hostname"] == socket.gethostname()
    assert _load_in_progress(review_db).keys() == {"o/r#1"}


def test_reserve_in_progress_raises_on_live_local_holder(review_db) -> None:
    _insert_holder(review_db, "o/r#1", os.getpid(), socket.gethostname())
    with pytest.raises(ReviewInProgressError) as exc_info:
        _reserve_in_progress(review_db, "o/r#1")
    err = exc_info.value
    assert err.pid == os.getpid()
    assert err.hostname == socket.gethostname()


def test_reserve_in_progress_raises_on_foreign_host(review_db) -> None:
    """Foreign-host rows are opaque (we can't probe a remote PID over
    NFS), so we must NOT treat them as stale even with an unlikely PID."""
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    with pytest.raises(ReviewInProgressError) as exc_info:
        _reserve_in_progress(review_db, "o/r#1")
    assert exc_info.value.hostname == "some-other-host"


def test_reserve_in_progress_recovers_stale_own_host_row(review_db) -> None:
    _insert_holder(review_db, "o/r#1", 99999999, socket.gethostname())
    row = _reserve_in_progress(review_db, "o/r#1")
    assert row["pid"] == os.getpid()
    live = _load_in_progress(review_db)
    assert live["o/r#1"]["pid"] == os.getpid()


def test_reserve_in_progress_force_replaces_any_holder(review_db) -> None:
    """`force=True` is the user-confirmed override path. It must
    succeed even against a live local PID — that's the whole point."""
    _insert_holder(review_db, "o/r#1", os.getpid(), socket.gethostname())
    row = _reserve_in_progress(review_db, "o/r#1", force=True)
    assert row["pid"] == os.getpid()


def test_release_in_progress_only_deletes_own_row(review_db) -> None:
    """A peer's row (different PID) must survive our release — the
    `(pid, hostname)` guards prevent ever deleting another tab's marker."""
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    _release_in_progress(review_db, "o/r#1")
    live = _load_in_progress(review_db)
    assert "o/r#1" in live
    assert live["o/r#1"]["pid"] == 99999999


def test_release_in_progress_is_idempotent(review_db) -> None:
    """Releasing a row that doesn't exist (already-released, never-reserved)
    must not raise — release must never tank the post-review path."""
    _release_in_progress(review_db, "o/r#1")  # no-op, no row yet
    _reserve_in_progress(review_db, "o/r#1")
    _release_in_progress(review_db, "o/r#1")
    _release_in_progress(review_db, "o/r#1")  # second release is no-op


def test_load_in_progress_sweeps_stale_own_host_rows(review_db) -> None:
    _insert_holder(review_db, "o/r#1", 99999999, socket.gethostname())
    live = _load_in_progress(review_db)
    assert live == {}
    # Sweep is persistent: row should be gone from the table.
    rows = review_db.execute("SELECT * FROM reviews_in_progress").fetchall()
    assert rows == []


def test_load_in_progress_keeps_foreign_host_rows(review_db) -> None:
    _insert_holder(review_db, "o/r#1", 99999999, "some-other-host")
    live = _load_in_progress(review_db)
    assert live.keys() == {"o/r#1"}


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


def test_in_progress_age_str_falls_back_on_unparseable_input() -> None:
    assert _in_progress_age_str("not-an-iso-string") == "not-an-iso-string"
