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

Ruff is the sole linter/formatter (configured in `pyproject.toml`); pre-commit runs it via `astral-sh/ruff-pre-commit`. Tests in `tests/` cover only the I/O-free helpers — prompt-suffix gating, semver compare, staleness rendering, comment formatting; TUI/subprocess/`gh` flows need integration testing. Build backend is `hatchling`, packaging the `cc_pr_reviewer/` directory (single module `__init__.py` plus the bundled `pr_review_agents/*.md` files).

## Architecture

Single-module Textual TUI (`cc_pr_reviewer/__init__.py`) that orchestrates external CLIs rather than talking to any API directly. Three layers matter:

1. **Data source — `gh` CLI.** `fetch_review_prs()` shells out to `gh search prs --review-requested=@me --state=open --json …`. With the `m` toggle on, `fetch_my_prs()` runs as a *second, independent* fetch merged into the same list (rows tagged `_mine=True`); a failure there surfaces as a separate warning rather than dropping the primary list. Auth/pagination/rate-limiting all piggyback on `gh api`/`gh api graphql` — the app never hits the GitHub API directly.

2. **TUI — Textual `App` + `ModalScreen`.** `PRReviewer` keeps `self.prs` as the source of truth and maps table rows via `self._row_to_pr_idx: list[int | None]` (`None` = non-selectable group-header row). `_selected()` consults the map rather than indexing `self.prs[cursor_row]`, so any code that mutates row layout (grouping, sectioning) must keep the map in lockstep with `add_row` calls. Network work runs in `@work(thread=True)` workers and marshals back via `call_from_thread`.

3. **Handoff to Claude Code — `App.suspend()` + `subprocess.call`.** `_launch_claude()` suspends the TUI, clones-or-fetches `$GH_PR_WORKSPACE/<owner>/<repo>`, runs `gh pr checkout <N> --force`, captures `HEAD`, fetches existing inline review comments, builds the prompt via `build_review_prompt()`, and `subprocess.call`s `claude --permission-mode acceptEdits <prompt>` so claude inherits the suspended TTY. Only `rc == 0` exits are recorded as reviews — Ctrl-C and crashes leave non-zero and must NOT inflate the review count or reset staleness.

### Things to know when editing

- **Layered prompt construction.** `build_review_prompt()` (pure, called from `_launch_claude`) is the single assembly point — `REVIEW_PROMPT` is the base string, then optional reviewer-extras / existing-comments / post-inline blocks join with `PROMPT_SECTION_SEP`. Two load-bearing facts the suffix matrix relies on:
  - The re-review APPROVE/RESOLVE chain is gated on `author_login != my_login` because GitHub returns 422 on self-approval. Keep that asymmetry — same-user self re-review still raises the bar (`POST_INLINE_REREVIEW_SUFFIX`) but skips approve/resolve.
  - The `*_RESOLVE_SUFFIX` GraphQL guardrails (paginate `pullRequestReviewThreads` with `first: 100`, treat `errors`/non-`isResolved` as failure, never fall back to the inline existing-comments list) come from past incidents — don't simplify them away. The full gating matrix is locked by `tests/test_cc_pr_reviewer.py`.
- **Launch invariants.** `claude` is always invoked with `--permission-mode acceptEdits` so file-edit prompts don't interrupt the review. Per-launch inputs flow `ConfirmScreen` → `ConfirmResult` → `_launch_claude(pr, post_inline, extra_prompt)`. The launch banner shows `extra_prompt` via `!r` capped at `EXTRA_PROMPT_BANNER_CAP` with a `(+N more chars)` suffix — without that suffix a 201-char paste renders identically to a clean 200-char one while the full text still flows into claude's argv, defeating the secret-paste preview. To add a new per-launch toggle: extend `ConfirmScreen` (binding + action + checkbox), add a field to `ConfirmResult`, thread it through `action_review` → `_launch_claude`.
- **Workspace reuse.** `$GH_PR_WORKSPACE/<owner>/<repo>` is reused across reviews of the same repo (clone first time, `git fetch` after). Don't switch to per-PR worktrees without updating the clone-vs-fetch branch in `_launch_claude`.
- **Prereq checks.** `check_prereqs()` runs before the TUI starts; validates `gh` (authenticated), `claude`, `git` on PATH, and the PR Review Toolkit plugin via `claude plugin list --json`. Add new external dependencies here so failures surface up-front instead of mid-flow.
- **Review state DB.** `$GH_PR_WORKSPACE/.review_state.db` is SQLite — `reviews` table holds per-PR `count`/`last_reviewed_at`/`last_pr_updated_at`/`last_head_sha`; `settings` table is K/V for persisted toggles. Staleness compares `pr.updatedAt` to stored `last_pr_updated_at`, so *any* PR activity (push, comment, label) flips the cell to "N stale" — that breadth is intentional.
- **Persisted toggles & state-bearing footer keys.** Four keys persist via the `settings` table: `f` (`repo_filter`), `m` (`include_mine`), `g` (`group_by`), `s` (`sort_by`). All four highlight via `_refresh_footer_indicators` → `_set_footer_active` (binary). Legal values for the cycled toggles live in `_GROUP_CYCLE` / `_SORT_CYCLE` — those dicts are the single source of truth (loader and toggle both consult them), so extending a cycle means editing only that dict. When grouping is active, group-header rows hold `None` in `_row_to_pr_idx`; `action_toggle_group` / `action_toggle_sort` re-populate from `self.prs` (no refetch) and re-seek the cursor to the previously selected PR. Sorting is render-time only — `self.prs` stays in fetch order so grouping/filtering compose cleanly.
- **Upgrade flow & PyPI worker.** `u` is gated on `update_check_state: UpdateCheckState` (`pending`/`current`/`available`/`failed`/`unavailable`). Source/editable installs (where `_installed_version()` returns `None`) skip the worker and force `unavailable` so `u` doesn't show a misleading "check failed". Any version string flowing into Rich (header label, badge) must be wrapped in `Text(...)` — PEP 440 local segments like `1.0.0+abc` otherwise break markup parsing during render.

## Pull requests

When opening a PR, follow `.github/PULL_REQUEST_TEMPLATE.md`: pass its contents as the `--body` to `gh pr create` (preserving every section heading) and fill each section based on the actual changes. Leave a section's bullet as `-` only when it genuinely does not apply (e.g. no DB migrations, no env config); never delete sections.
