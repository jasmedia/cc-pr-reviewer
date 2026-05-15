# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency/venv management, with `pyproject.toml` as the source of truth and `uv.lock` committed.

```sh
uv sync                       # install deps (only runtime dep: textual>=0.60)
uv run cc-pr-reviewer         # run the TUI
uv add <pkg>                  # add a new dependency
uv run ruff check .           # lint
uv run ruff format .          # format
uv run pytest                 # run unit tests (pytest -k <name> for one)
uv run pre-commit install     # install git hook
uv run pre-commit run --all-files  # run hooks ad-hoc
uv run python scripts/sync_pr_review_agents.py    # diff bundled review agents vs upstream Claude plugin
```

Ruff is the sole linter/formatter (configured in `pyproject.toml`); pre-commit runs it via `astral-sh/ruff-pre-commit`. Tests in `tests/` cover only the I/O-free helpers — prompt-suffix gating, semver compare, staleness rendering, comment formatting, skill materialise/cleanup; TUI/subprocess/`gh` flows need integration testing. Build backend is `hatchling`, packaging the `cc_pr_reviewer/` directory (single module `__init__.py` plus the bundled `skills/<name>/SKILL.md` files).

## Architecture

Single-module Textual TUI (`cc_pr_reviewer/__init__.py`) that orchestrates external coding-agent CLIs rather than talking to any API directly. Three layers matter:

1. **Data source — `gh` CLI.** `fetch_review_prs()` shells out to `gh search prs --review-requested=@me --state=open --json …`. With the `m` toggle on, `fetch_my_prs()` runs as a *second, independent* fetch merged into the same list (rows tagged `_mine=True`); a failure there surfaces as a separate warning rather than dropping the primary list. Auth/pagination/rate-limiting all piggyback on `gh api`/`gh api graphql` — the app never hits the GitHub API directly.

2. **TUI — Textual `App` + `ModalScreen`.** `PRReviewer` keeps `self.prs` as the source of truth and maps table rows via `self._row_to_pr_idx: list[int | None]` (`None` = non-selectable group-header row). `_selected()` consults the map rather than indexing `self.prs[cursor_row]`, so any code that mutates row layout (grouping, sectioning) must keep the map in lockstep with `add_row` calls. Network work runs in `@work(thread=True)` workers and marshals back via `call_from_thread`.

3. **Handoff to a coding-agent CLI — `App.suspend()` + `subprocess.call`.** `_launch_claude()` (named historically; CLI-agnostic now) suspends the TUI, clones-or-fetches `$GH_PR_WORKSPACE/<owner>/<repo>`, runs `gh pr checkout <N> --force`, captures `HEAD`, materialises the six bundled skills into `<workspace>/.agents/skills/` (codex/gemini only), fetches existing inline review comments, builds the prompt via `build_review_prompt(cli=…)`, builds the argv via `_build_cli_command(cli, prompt)`, and `subprocess.call`s the chosen CLI so it inherits the suspended TTY. The `finally` block always runs `_cleanup_skills` (codex/gemini only) before releasing the in-progress reservation. Only `rc == 0` exits are recorded as reviews — Ctrl-C and crashes leave non-zero and must NOT inflate the review count or reset staleness.

### Multi-CLI selection

Three coding-agent CLIs are supported and switchable from the TUI: `claude` (Claude Code), `codex` (OpenAI Codex CLI), and `gemini` (Google Gemini CLI). The `CliChoice` literal type + `_CLI_CYCLE` dict are the single source of truth — every selector (the `c` toggle, the `Ctrl+L` modal override, the prereq fallback in `__init__`) consults `_CLI_CYCLE`. Adding a fourth CLI would mean: extending the Literal, the cycle, `_CLI_DISPLAY`, and adding a branch in `_build_cli_command` (the only place flag surfaces live).

**Prompt selection.** Claude reads `REVIEW_PROMPT_CLAUDE` which invokes the PR Review Toolkit plugin's six sub-agents by name. Codex and Gemini share `REVIEW_PROMPT_SKILL_BASED`, a short prompt that mentions the six bundled skills via `$<name>` for explicit activation. The skills themselves ship as `cc_pr_reviewer/skills/<name>/SKILL.md` (YAML frontmatter with `name:` + `description:` so codex/gemini auto-discover them, plus the adapted prose body) and are materialised into `<workspace>/.agents/skills/<name>/SKILL.md` per launch via `_materialise_skills` / `_cleanup_skills`. The prose bodies are an adapted fork of the upstream toolkit's agent prompts (upstream YAML frontmatter and `## When to invoke` sections stripped, plus CLAUDE.md→AGENTS.md broadening and PR-focused scope phrasing); the skill frontmatter we inject is hand-written for Codex/Gemini discovery. Keep the prose in sync with `scripts/sync_pr_review_agents.py` (see below).

**Skill materialisation & cleanup.** `_materialise_skills(workspace)` copies each bundled SKILL.md to `<workspace>/.agents/skills/<name>/SKILL.md` after `gh pr checkout` (so the checkout doesn't reset our writes). `_cleanup_skills(workspace)` runs in `finally` regardless of exit path and uses `unlink` + `rmdir` (not `rmtree`) so unrelated content inside `.agents/skills/<name>/` is preserved if a collision happens; the parent-dir cleanup is best-effort. Both helpers are idempotent and safe on a pristine workspace.

**Launch flag surfaces** (verified against current upstream docs; localised to `_build_cli_command` so flag churn in any single CLI is a one-function change):

| CLI    | argv                                                                                                                       |
| ------ | -------------------------------------------------------------------------------------------------------------------------- |
| claude | `claude --permission-mode acceptEdits "<prompt>"`                                                                          |
| codex  | `codex --ask-for-approval never --sandbox workspace-write -c sandbox_workspace_write.network_access=true "<prompt>"`       |
| gemini | `gemini --approval-mode auto_edit "<prompt>"`                                                                              |

All three auto-approve edits inside the workspace. For codex, `--yolo` is **rejected** because it removes the filesystem sandbox too; instead we override `sandbox_workspace_write.network_access=true` to restore network (the post-inline review path needs `gh api …`, which codex's workspace-write sandbox blocks by default). For gemini, the older `--yolo`/`-y` is **rejected** in favour of the modern `--approval-mode=yolo` (and we pick `auto_edit`, the documented analogue of Claude's `acceptEdits`).

### Things to know when editing

- **Layered prompt construction.** `build_review_prompt(cli=…)` (pure, called from `_launch_claude`) is the single assembly point. It picks `REVIEW_PROMPT_CLAUDE` or `REVIEW_PROMPT_SKILL_BASED` for the base, then optional reviewer-extras / existing-comments / post-inline blocks join with `PROMPT_SECTION_SEP`. All `POST_INLINE_*` suffixes apply to every CLI — they're about `gh` CLI usage, not the reviewing agent. Two load-bearing facts the suffix matrix relies on:
  - The re-review APPROVE/RESOLVE chain is gated on `author_login != my_login` because GitHub returns 422 on self-approval. Keep that asymmetry — same-user self re-review still raises the bar (`POST_INLINE_REREVIEW_SUFFIX`) but skips approve/resolve.
  - The `*_RESOLVE_SUFFIX` GraphQL guardrails (paginate `pullRequestReviewThreads` with `first: 100`, treat `errors`/non-`isResolved` as failure, never fall back to the inline existing-comments list) come from past incidents — don't simplify them away. The full gating matrix is locked by `tests/test_cc_pr_reviewer.py` and parameterised across all three CLIs to catch any future drift that branches the suffix logic on CLI choice.
- **Launch invariants.** Per-launch inputs flow `ConfirmScreen` → `ConfirmResult` → `_launch_claude(pr, post_inline, extra_prompt, cli)`. The `cli` arg defaults to the global `self.cli` but can be overridden via `Ctrl+L` in the modal — that override is one-shot and does NOT mutate the persisted setting. The launch banner shows `extra_prompt` via `!r` capped at `EXTRA_PROMPT_BANNER_CAP` with a `(+N more chars)` suffix — without that suffix a 201-char paste renders identically to a clean 200-char one while the full text still flows into argv, defeating the secret-paste preview. To add a new per-launch toggle: extend `ConfirmScreen` (binding + action + checkbox), add a field to `ConfirmResult`, thread it through `action_review` → `_launch_claude`.
- **Workspace reuse.** `$GH_PR_WORKSPACE/<owner>/<repo>` is reused across reviews of the same repo (clone first time, `git fetch` after). Don't switch to per-PR worktrees without updating the clone-vs-fetch branch in `_launch_claude`.
- **Prereq checks & startup fallback.** `check_prereqs()` runs before the TUI starts; validates `gh` (authenticated), `git`, AND **at least one** of `claude`/`codex`/`gemini` on PATH (via `_first_available_cli`). It does *not* gate on the specific persisted CLI — a Codex-only user shouldn't be blocked by `claude` being missing. `PRReviewer.__init__` reads the persisted CLI, and if it isn't on PATH, falls back to the first available CLI in cycle order (in-memory only, doesn't overwrite the persisted setting); `on_mount` surfaces the fallback as a warning toast. The toolkit-plugin check moved *out* of startup and into `_launch_claude`'s pre-flight (only fires when cli=claude is the chosen launcher).
- **Review state DB.** `$GH_PR_WORKSPACE/.review_state.db` is SQLite — `reviews` table holds per-PR `count`/`last_reviewed_at`/`last_pr_updated_at`/`last_head_sha`; `settings` table is K/V for persisted toggles. Staleness compares `pr.updatedAt` to stored `last_pr_updated_at`, so *any* PR activity (push, comment, label) flips the cell to "N stale" — that breadth is intentional.
- **Persisted toggles & state-bearing footer keys.** Five keys persist via the `settings` table: `f` (`repo_filter`), `m` (`include_mine`), `g` (`group_by`), `s` (`sort_by`), `c` (`cli`). All five highlight via `_refresh_footer_indicators` → `_set_footer_active` (binary). Legal values for the cycled toggles live in `_GROUP_CYCLE` / `_SORT_CYCLE` / `_CLI_CYCLE` — those dicts are the single source of truth (loader and toggle both consult them), so extending a cycle means editing only that dict. When grouping is active, group-header rows hold `None` in `_row_to_pr_idx`; `action_toggle_group` / `action_toggle_sort` re-populate from `self.prs` (no refetch) and re-seek the cursor to the previously selected PR. Sorting is render-time only — `self.prs` stays in fetch order so grouping/filtering compose cleanly. The `c` toggle is render-free (table contents don't depend on CLI choice).
- **Upgrade flow & PyPI worker.** `u` is gated on `update_check_state: UpdateCheckState` (`pending`/`current`/`available`/`failed`/`unavailable`). Source/editable installs (where `_installed_version()` returns `None`) skip the worker and force `unavailable` so `u` doesn't show a misleading "check failed". Any version string flowing into Rich (header label, badge) must be wrapped in `Text(...)` — PEP 440 local segments like `1.0.0+abc` otherwise break markup parsing during render.
- **Upstream sync workflow.** `cc_pr_reviewer/skills/<name>/SKILL.md` is a static fork of the Claude `pr-review-toolkit` plugin's agent files (with hand-written Codex/Gemini Skills frontmatter prepended). The plugin reports its version as `unknown`, so drift is detected by content diff. `scripts/sync_pr_review_agents.py` normalises current upstream (strips upstream frontmatter + `## When to invoke`) and diffs it against a committed baseline snapshot at `scripts/upstream_baseline/<name>.md`; for the bundled-vs-upstream "drift" check it ALSO strips our injected Codex/Gemini frontmatter via `strip_bundled_frontmatter` so the comparison is prose-vs-prose. When upstream diverges from the baseline, the script prints `UPSTREAM CHANGED` and exits non-zero. After triaging upstream changes into the bundled SKILL.md bodies, run `--save-baseline` to lock in the new reference. `--write` mode preserves our hand-written frontmatter (extracts the leading `---…---` block from the existing SKILL.md and prepends it to the normalised upstream body). `scripts/upstream_baseline/` is outside the wheel (pyproject's `packages = ["cc_pr_reviewer"]` excludes `scripts/`), so it stays a maintainer-only artifact.

## Pull requests

When opening a PR, follow `.github/PULL_REQUEST_TEMPLATE.md`: pass its contents as the `--body` to `gh pr create` (preserving every section heading) and fill each section based on the actual changes. Leave a section's bullet as `-` only when it genuinely does not apply (e.g. no DB migrations, no env config); never delete sections.
