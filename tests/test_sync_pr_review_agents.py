"""Tests for `scripts/sync_pr_review_agents.py` (maintainer-only tool).

Scope: pure helpers only — the regex-based normaliser and the
prereq-free building blocks. The subprocess/filesystem flow is covered
by manual smoke tests (the script literally runs `claude plugin update`
and reads/writes the workspace), not by these unit tests.

The script is imported via the `pythonpath = ["scripts"]` setting in
`pyproject.toml`, so it isn't part of the wheel.
"""

from __future__ import annotations

import pytest
import sync_pr_review_agents as sync
from sync_pr_review_agents import (
    compose_with_existing_frontmatter,
    normalise_upstream,
    strip_bundled_frontmatter,
)

# --- normalise_upstream ----------------------------------------------------


def test_normalise_strips_yaml_frontmatter() -> None:
    """Upstream agents lead with a `---name: …---` block describing the
    Claude sub-agent. Codex/Gemini have no concept of named sub-agents,
    so the block has to be stripped or it becomes garbage prose at the
    top of the prompt."""
    text = (
        "---\n"
        "name: code-reviewer\n"
        "description: review code\n"
        "model: opus\n"
        "---\n"
        "\n"
        "Body content here.\n"
    )
    out = normalise_upstream(text)
    assert "---" not in out
    assert "name: code-reviewer" not in out
    assert out.startswith("Body content here.")


def test_normalise_strips_when_to_invoke_section() -> None:
    """The `## When to invoke` section is Claude-sub-agent-dispatch
    framing — irrelevant to flat-invocation Codex/Gemini and confusing
    if left in (it tells the CLI to "spawn" agents that don't exist)."""
    text = (
        "Some intro paragraph.\n"
        "\n"
        "## When to invoke\n"
        "\n"
        "Three representative scenarios:\n"
        "\n"
        "- Scenario A.\n"
        "- Scenario B.\n"
        "\n"
        "## Review Scope\n"
        "\n"
        "Body continues.\n"
    )
    out = normalise_upstream(text)
    assert "When to invoke" not in out
    assert "Scenario A" not in out
    # The next H2 must survive — the regex stops at the next `##`, not at
    # an arbitrary heading level, so this guards against an over-greedy
    # rewrite that swallows everything past the section.
    assert "## Review Scope" in out
    assert "Body continues." in out


def test_normalise_when_to_invoke_at_end_of_file_handled() -> None:
    """If `## When to invoke` is the LAST section in the file there's no
    `next H2` to anchor against — the regex must instead stop at EOF.
    Common shape in the upstream agents that have minimal "Core Mission"
    framing after the section."""
    text = "Intro paragraph.\n\n## When to invoke\n\n- Scenario A.\n- Scenario B.\n"
    out = normalise_upstream(text)
    assert "When to invoke" not in out
    assert "Scenario A" not in out
    assert out.startswith("Intro paragraph.")


def test_normalise_does_not_strip_other_h2_headings() -> None:
    """`## Output Format`, `## Review Scope`, etc. are NOT stripped —
    only the specific `## When to invoke` section. A regression here
    would silently mute large chunks of the prompt for codex/gemini."""
    text = (
        "Intro.\n"
        "\n"
        "## Output Format\n"
        "\n"
        "Some output guidance.\n"
        "\n"
        "## Review Scope\n"
        "\n"
        "Some scope guidance.\n"
    )
    out = normalise_upstream(text)
    assert out == text


def test_normalise_strips_both_frontmatter_and_when_to_invoke() -> None:
    """End-to-end: a representative upstream file with both structural
    pieces should be reduced to plain prose. The combined-strip case
    matters because the regex order needs to be frontmatter-then-WTI;
    if the WTI regex ran first it could match across a frontmatter that
    happens to contain `## When to invoke` in its description string."""
    text = (
        "---\n"
        "name: code-reviewer\n"
        'description: See "When to invoke" in the body.\n'
        "---\n"
        "\n"
        "You are an expert.\n"
        "\n"
        "## When to invoke\n"
        "\n"
        "Use this agent when ...\n"
        "\n"
        "## Review Scope\n"
        "\n"
        "Scope text.\n"
    )
    out = normalise_upstream(text)
    assert "name: code-reviewer" not in out
    assert "When to invoke" not in out
    assert "Use this agent when" not in out
    assert "You are an expert." in out
    assert "## Review Scope" in out
    assert "Scope text." in out


def test_normalise_passes_through_text_without_frontmatter_or_when_to_invoke() -> None:
    """An already-adapted file (or a future upstream agent that drops
    the legacy framing) must round-trip unchanged."""
    text = "# Title\n\nBody paragraph.\n\n## Output Format\n\nMore body.\n"
    assert normalise_upstream(text) == text


# --- module constants sanity ----------------------------------------------


def test_baseline_dir_lives_next_to_script() -> None:
    """The baseline dir is the load-bearing reference point for upstream
    drift detection; if it ever resolves to a path that ships in the
    wheel (i.e. inside `cc_pr_reviewer/`), drift state would leak into
    user installs and the maintainer tool would compare against
    whichever baseline shipped last release. Pin the invariant here."""
    assert sync.BASELINE_DIR.name == "upstream_baseline"
    assert sync.BASELINE_DIR.parent.name == "scripts"


def test_plugin_id_matches_marketplace_format() -> None:
    """`claude plugin update` rejects bare plugin names — it needs the
    `<plugin>@<marketplace>` form. Pin the format so a future rename
    that drops the marketplace suffix is caught at test time, not when
    `--update-plugin` silently no-ops in CI."""
    assert "@" in sync.PLUGIN_ID
    name, marketplace = sync.PLUGIN_ID.split("@", 1)
    assert name == "pr-review-toolkit"
    assert marketplace


# --- strip_bundled_frontmatter ---------------------------------------------


def test_strip_bundled_frontmatter_removes_skills_frontmatter() -> None:
    """Bundled SKILL.md files have hand-written Codex/Gemini Skills
    frontmatter (`name:` + `description:`) we inject when adapting from
    upstream. The sync script's prose-vs-prose comparison strips this
    so it doesn't show up as drift on every run."""
    text = (
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews PR diffs for project-guideline violations.\n"
        "---\n"
        "\n"
        "# Code Reviewer\n"
        "\n"
        "Prose body.\n"
    )
    out = strip_bundled_frontmatter(text)
    assert "name: code-reviewer" not in out
    assert "description:" not in out
    assert out.startswith("# Code Reviewer")


def test_strip_bundled_frontmatter_raises_when_absent() -> None:
    """A bundled SKILL.md without our injected frontmatter is a defect
    (bad merge, accidental overwrite, `--write` reset that lost the
    block). Silently returning the body unchanged would let the
    upstream-drift check report 'in sync' on a SKILL.md that Codex/
    Gemini can't discover at runtime — fail loud instead."""
    text = "# Code Reviewer\n\nProse body.\n"
    with pytest.raises(ValueError, match="missing the required Codex/Gemini"):
        strip_bundled_frontmatter(text)


# --- compose_with_existing_frontmatter -------------------------------------


def test_compose_preserves_local_frontmatter_with_fresh_body() -> None:
    """`--write` extracts the hand-written `name:`/`description:` block
    from the existing SKILL.md and prepends it to the normalised
    upstream body. Lock the composition so a refactor can't silently
    emit a SKILL.md with no frontmatter."""
    local = "---\nname: code-reviewer\ndescription: Reviews PR diffs.\n---\n\nOld body.\n"
    new_body = "# Fresh\n\nNew body from normalised upstream.\n"
    out = compose_with_existing_frontmatter(local, new_body)
    assert out.startswith("---\nname: code-reviewer\ndescription: Reviews PR diffs.\n---\n")
    assert "Old body." not in out
    assert "New body from normalised upstream." in out


def test_compose_raises_when_local_has_no_frontmatter() -> None:
    """If the existing SKILL.md is already defective (no frontmatter),
    `--write` has nothing to preserve. Refuse rather than silently
    emit a frontmatter-less SKILL.md."""
    with pytest.raises(ValueError, match="cannot preserve a block that isn't there"):
        compose_with_existing_frontmatter("# No frontmatter\n", "body\n")
