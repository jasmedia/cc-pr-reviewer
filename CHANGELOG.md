# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-04-29

### Added
- **Release Notes link** pinned to the top-right of the TUI header (left of
  the clock) that opens `CHANGELOG.md` on GitHub in your browser.

### Fixed
- Diff modal was truncating long, multi-file diffs to the viewport with no
  way to scroll. The body is now wrapped in a `VerticalScroll` and focused
  on mount, so arrow keys, PgUp/PgDn, Home/End, and the mouse wheel all
  scroll the diff. `q` and `Esc` still dismiss the modal.

## [0.6.0] — 2026-04-29

### Added
- **Extra-prompt textbox** in the review confirmation modal: append
  free-form context (e.g. "focus on security", "this is a hotfix") to the
  prompt sent to Claude Code without editing `REVIEW_PROMPT`.
- Confirm-modal keyboard ergonomics: textbox stays unfocused on open so the
  y/n/p/Enter fast path still works; Ctrl+Y / Ctrl+N / Ctrl+P confirm,
  cancel, and toggle post-inline from inside the textbox; Shift+Enter
  inserts a newline.

### Changed
- Command palette moved from `ctrl+p` to bare `p` so the confirm modal's
  `Ctrl+P` (toggle post-inline) is no longer shadowed by Textual's
  App-level priority binding. Typing `p` inside the textbox still works
  because priority bindings on printable keys yield to focused inputs.

### Fixed
- Diff modal no longer crashes when a diff line contains characters Rich
  parses as markup (e.g. `[x]`); markup parsing is disabled on the body.
- Failed diff loads now surface the error in the modal instead of leaving
  it stalled on "Loading…".

## [0.5.0] — 2026-04-27

### Added
- Raise the review bar when re-reviewing a PR: subsequent reviews append a
  stricter prompt suffix so Claude scrutinises the diff harder than on the
  first pass.
- Improvements to the "my PRs" toggle: clearer visibility of which mode is
  active and more reliable switching between review-requested PRs and
  authored PRs.

### Changed
- On self-authored PRs, the rereview suffix is skipped (you don't re-review
  your own work) and the raised bar applies only when it makes sense.
- The rereview banner is no longer shown when the post-inline toggle is off,
  so you don't get a misleading "rereview" claim for local-only reviews.
- Documented the expectation that Claude follows
  `.github/PULL_REQUEST_TEMPLATE.md` when opening PRs.

### Fixed
- PR titles in the confirm modal no longer break when they contain Rich
  markup characters (e.g. square brackets).
- Hardened `fetch_my_prs` and the table populate path against edge cases
  raised in PR review.
- Silenced the empty-workdir warning in the publish CI job.

## [0.4.0] — 2026-04-25

### Added
- **Repo filter**: filter the PR list to a single repository via a modal
  selector backed by a cached repo list. Press `r` inside the modal to
  refresh the cache.
- The chosen repo filter is persisted across restarts via a new `settings`
  table.
- **In-app upgrade**: trigger `uv tool upgrade` from inside the TUI to pull
  the latest release without leaving the app.
- README now includes a screenshot plus PyPI downloads, uv, and ruff badges.
- Added a pull request template under `.github/`.

### Changed
- Dropped the top-level "clear filter" binding; clearing the filter now lives
  inside the filter modal itself.

### Fixed
- The `[repo=…]` indicator on the status bar now renders literally instead
  of being parsed as Rich markup.
- Addressed review feedback on both the repo filter and the in-app upgrade
  action.

## [0.3.0] — 2026-04-24

### Added
- Moved the **post-inline toggle** into the review confirmation modal, so
  you choose whether Claude should publish findings as inline PR comments
  on a per-launch basis (defaults to on each time the modal opens).

### Changed
- `claude` is now **always** invoked with `--permission-mode acceptEdits`;
  the previous `a` toggle has been removed. File-edit prompts no longer
  interrupt a review.
- `ConfirmScreen` dismiss value widened to a `ConfirmResult` dataclass to
  carry the toggle state cleanly.
- README pruned: release and extending sections removed, keybindings
  deduplicated.

### Fixed
- Checkbox labels in the modal now render `[x]` literally instead of being
  parsed as Rich markup.

## [0.2.2] — 2026-04-24

### Added
- A confirmation modal now appears before launching a Claude review, so an
  accidental keypress no longer kicks off a session.

### Changed
- Reviews are triggered exclusively by pressing **Enter** on the PR table —
  mouse clicks no longer launch a review.
- Bumped GitHub Actions past Node 20 runtimes.

### Fixed
- Fetching existing PR review comments now uses `GET` instead of the prior
  incorrect method.

## [0.2.1] — 2026-04-23

### Added
- **Update-available badge**: the TUI now checks PyPI on startup and tells
  you when a newer version is available.

### Changed
- Bumped GitHub Actions to Node 24 runtimes and silenced cache warnings in
  the publish job.

## [0.2.0] — 2026-04-23

### Added
- Existing PR review comments are fetched and **injected into the prompt**
  so Claude avoids restating findings that have already been raised.
- Archived repositories are excluded from the PR search.

### Changed
- Refactored existing-comment injection: better prompt formatting, more
  robust sort, and tightened fetch bounds.

### Fixed
- Failed dedup fetches are now distinguished from empty results, and a
  review that fails is no longer recorded as completed in the local state
  DB.

### Documentation
- Updated the release section of the README to cover GitHub Actions plus
  PyPI trusted publishing.

## [0.1.1] — 2026-04-20

### Added
- GitHub Actions release workflow with a semver bump input.
- PyPI publish workflow using `uv` and trusted publishing.

### Changed
- Hardened the publish workflow against silent failures (addressed PR
  review feedback).

## [0.1.0] — 2026-04-20

Initial release.

### Added
- Single-file Textual TUI (`cc_pr_reviewer.py`) that lists PRs awaiting your
  review via `gh search prs --review-requested=@me`.
- Modal diff viewer backed by `gh pr diff`.
- One-keypress handoff to Claude Code: suspends the TTY, ensures the repo
  is cloned under `$GH_PR_WORKSPACE/<owner>/<repo>` (clone on first use,
  `git fetch` on subsequent reviews), runs `gh pr checkout`, and launches
  `claude` in the PR's working tree.
- Tracks per-PR review history in a SQLite DB at
  `$GH_PR_WORKSPACE/.review_state.db`, surfacing review count and a
  staleness indicator in the table.
- Prereq checks for `gh`, `claude`, `git`, and the **PR Review Toolkit**
  Claude Code plugin.

[0.7.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.7.0
[0.6.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.6.0
[0.5.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.5.0
[0.4.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.4.0
[0.3.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.3.0
[0.2.2]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.2
[0.2.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.1
[0.2.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.0
[0.1.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.1.1
