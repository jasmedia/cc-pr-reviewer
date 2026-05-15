# Upstream baseline snapshots

Committed copies of the **normalised** upstream PR Review Toolkit agent
prompts (`~/.claude/plugins/marketplaces/claude-plugins-official/plugins/pr-review-toolkit/agents/*.md`)
as of the last sync. Used by `scripts/sync_pr_review_agents.py` to
detect upstream changes:

* **current upstream == baseline** → nothing changed upstream; no
  action needed.
* **current upstream != baseline** → upstream gained content; the
  script prints `UPSTREAM CHANGED` and exits 1 so the maintainer
  triages the diff into `cc_pr_reviewer/skills/<name>/SKILL.md`.

After triaging, run `uv run python scripts/sync_pr_review_agents.py --save-baseline`
to lock in the new upstream state as the new reference.

The "normalisation" step strips two structural pieces we always remove
when bundling: the YAML frontmatter at the top of each upstream file,
and the `## When to invoke` section. Both describe Claude's sub-agent
dispatch model, which doesn't apply to Codex/Gemini's skill-based
invocation.

Filenames here mirror upstream's flat `<agent-name>.md` layout, not
our bundled `skills/<name>/SKILL.md` layout — the baseline is a copy
of upstream's content as we'd want it (sans frontmatter / WTI), so
the contributor can compare like-for-like.

These files are **not** shipped in the wheel — `pyproject.toml` scopes
the build to the `cc_pr_reviewer/` package directory, which excludes
everything under `scripts/`.
