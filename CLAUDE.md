# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency/venv management, with `pyproject.toml` as the source of truth and `uv.lock` committed.

```sh
uv sync                       # create .venv and install deps (only runtime dep: textual>=0.60)
uv run cc-pr-reviewer         # run the TUI (console script defined in pyproject.toml)
uv run python cc_pr_reviewer.py   # equivalent direct invocation
uv add <pkg>                  # add a new dependency
uv run ruff check .           # lint
uv run ruff format .          # format
uv run pre-commit install     # install git hook (requires the repo to be a git repo)
uv run pre-commit run --all-files  # run all hooks ad-hoc
```

Ruff is configured in `pyproject.toml` (`[tool.ruff]`) as the sole linter/formatter; pre-commit is wired via `.pre-commit-config.yaml` using `astral-sh/ruff-pre-commit`. There is no test suite. The build backend is `hatchling`, configured to package `cc_pr_reviewer.py` as a single-module wheel.

## Architecture

Single-file Textual TUI (`cc_pr_reviewer.py`) that orchestrates external CLIs rather than talking to any API directly. Three layers matter:

1. **Data source — `gh` CLI.** `fetch_review_prs()` shells out to `gh search prs --review-requested=@me --state=open --json …` and parses the JSON. The app never uses the GitHub REST/GraphQL API directly; all auth, pagination, and rate-limiting piggyback on `gh`.

2. **TUI — Textual `App` + `ModalScreen`.** `PRReviewer` renders a `DataTable` of PRs and maintains `self.prs` as the source of truth. Row → PR mapping goes through `self._row_to_pr_idx: list[int | None]`, where each entry is a position in `self.prs` or `None` for a non-selectable group-header row; `_selected()` consults this map rather than indexing `self.prs[cursor_row]` directly, so any future code that mutates the row layout (grouping, sectioning) must keep the map in lockstep with `add_row` calls. Network work (`_load_prs`) runs in `@work(thread=True)` workers and marshals results back via `call_from_thread`. `DiffScreen` is a modal that shells out to `gh pr diff` on mount.

3. **Handoff to Claude Code — `App.suspend()` + `subprocess.call`.** The critical flow is `_launch_claude()`: it enters `self.suspend()` (releases the terminal), ensures the repo is cloned under `$GH_PR_WORKSPACE/<owner>/<repo>` (cloning on first use, `git fetch --all --prune` on subsequent reviews), runs `gh pr checkout <N> --force` in that directory, then `subprocess.call` on a `claude` command (always with `--permission-mode acceptEdits`, plus the post-inline prompt suffix when that toggle is on). Because `claude` inherits the suspended TTY, the user gets a full interactive Claude Code session in the PR's working tree. When `claude` exits, the TUI resumes and auto-refreshes the list (review state may have changed).

### Things to know when editing

- **`REVIEW_PROMPT`** (top of file) is the base string passed to `claude` as its initial user message. Changing it changes what the downstream [PR Review Toolkit](https://claude.com/plugins/pr-review-toolkit) plugin's sub-agents get asked to do. `POST_INLINE_PROMPT` is appended when the post-inline toggle is on and instructs Claude to publish findings as inline PR review comments via `gh api`; edit both together if you change the review shape.
- **Launch invariants.** `claude` is always invoked with `--permission-mode acceptEdits` so file-edit prompts don't interrupt the review. The one launch-time toggle lives on `ConfirmScreen` as a checkbox (bound to `p`, defaults to on each time the modal opens): when on, it appends `POST_INLINE_PROMPT` and tells Claude to publish findings as inline PR comments via `gh api`; toggle off for a local-only review. The chosen value is passed into `_launch_claude(pr, post_inline)` and echoed before launch. When adding a new per-launch toggle, extend `ConfirmScreen` (new `BINDINGS` entry, `action_toggle_*` handler, checkbox row) and thread the value through `action_review` into `_launch_claude`.
- **Workspace reuse.** The `$GH_PR_WORKSPACE/<owner>/<repo>` layout is deliberate: a second review of the same repo reuses the clone. Don't switch to per-PR worktrees without updating the clone-vs-fetch branch in `_launch_claude`.
- **Prereq checks** live in `check_prereqs()` and run before the TUI starts. If you add a new external dependency, add its check there so users get a clear error instead of a mid-flow crash.
- **External CLIs assumed on PATH:** `gh` (authenticated), `claude`, `git`. The PR Review Toolkit plugin must be installed and enabled inside Claude Code; `check_prereqs()` detects this via `claude plugin list --json` and treats a missing/disabled plugin as a startup error.
- **Review state DB.** `$GH_PR_WORKSPACE/.review_state.db` is a tiny SQLite DB (`reviews` table) tracking per-PR `count`, `last_reviewed_at`, `last_pr_updated_at`, and `last_head_sha`. It drives the "Reviews" column (`-` / `N` / `N stale`). Staleness = PR's current `updatedAt` differs from `last_pr_updated_at` captured at last review, so any PR activity (pushes, comments, label changes) flips it to stale. See `_open_review_db`, `_record_review`, `_review_cell`.
- **Persisted toggles & state-bearing footer keys.** Three keys carry persisted state: `f` (`repo_filter`), `m` (`include_mine`), and `g` (`group_by`). All three round-trip through the same `settings` table via `_get_setting` / `_set_setting`, and all three highlight via `_refresh_footer_indicators` → `_set_footer_active` (binary highlight). `group_by` is the only tri-state of the three: it cycles `"" → "repo" → "author" → ""` via `_GROUP_CYCLE` (the single source of truth — extending the cycle means editing only that dict). The footer key shows "active" for any non-empty value; the actual mode lives in the status line via `_group_desc()`. When grouping is active, group-header rows are inserted into the table and `_row_to_pr_idx` records `None` at their positions; `action_toggle_group` re-populates from `self.prs` (no refetch) and re-seeks the cursor to the previously selected PR so toggling doesn't strand the user on a header row.

## Pull requests

When opening a PR, follow `.github/PULL_REQUEST_TEMPLATE.md`: pass its contents as the `--body` to `gh pr create` (preserving every section heading) and fill each section based on the actual changes. Leave a section's bullet as `-` only when it genuinely does not apply (e.g. no DB migrations, no env config); never delete sections.
