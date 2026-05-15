# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] — 2026-05-15

### Added
- **Multi-CLI support: Codex and Gemini alongside Claude Code.** The TUI
  now drives three coding-agent CLIs interchangeably. A new `c`
  keybinding cycles `claude → codex → gemini → claude` globally
  (persisted via the `settings` table), and `Ctrl+L` inside the
  confirm modal lets you override the launcher for a single review
  without mutating the persisted default. Each CLI gets its own argv
  surface localised in `_build_cli_command`:
  `claude --permission-mode acceptEdits`,
  `codex --ask-for-approval never --sandbox workspace-write -c sandbox_workspace_write.network_access=true`,
  `gemini --approval-mode auto_edit`. The `CliChoice` Literal +
  `_CLI_CYCLE` / `_CLI_DISPLAY` dicts are the single source of truth
  consulted by every selector.
- **Native Skills format for Codex/Gemini review agents.** The six
  bundled review-agent prompts moved from a single ~500-line
  file-based prompt under `cc_pr_reviewer/pr_review_agents/*.md` to
  native Codex/Gemini Skills under
  `cc_pr_reviewer/skills/<name>/SKILL.md` with hand-written YAML
  frontmatter for auto-discovery. Each launch materialises them into
  `<workspace>/.agents/skills/<name>/SKILL.md`; the model now loads
  only ~100 words of skill metadata up front and pulls in the full
  body only when it activates a skill via `$<name>` mention.
- **Upstream-sync workflow** (`scripts/sync_pr_review_agents.py`). The
  upstream `pr-review-toolkit` plugin reports its version as
  `unknown`, so drift is detected by content diff. Committed baseline
  snapshots under `scripts/upstream_baseline/<name>.md` capture
  normalised upstream at the last sync; the script flags
  `UPSTREAM CHANGED` and exits non-zero when upstream diverges from
  the baseline. New flags: `--save-baseline` to lock in a new
  reference, `--update-plugin` to run the upstream plugin update
  first, and `--write` to overwrite bundled SKILL.md bodies with
  normalised upstream (preserving our hand-written frontmatter via
  `compose_with_existing_frontmatter`). The `scripts/` directory is
  outside the wheel, so baselines stay a maintainer-only artifact.

### Changed
- **Prereq checks no longer gate on the specific persisted CLI.**
  `check_prereqs` now requires `gh` (authenticated), `git`, and AT
  LEAST ONE of `claude`/`codex`/`gemini` on PATH — so a Codex-only or
  Gemini-only user on a fresh install can start the TUI without
  installing Claude Code first. The toolkit-plugin check moved out of
  startup into `_launch_claude`'s pre-flight (only fires when
  `cli=claude` is the chosen launcher). `PRReviewer.__init__` falls
  back via `_first_available_cli(preferred)` in memory when the
  persisted CLI isn't on PATH; the fallback is surfaced as a warning
  toast during `on_mount`.
- **Prompt construction takes a `cli` kwarg.** `build_review_prompt`
  selects `REVIEW_PROMPT_CLAUDE` (plugin-driven) for `claude` and the
  shared `REVIEW_PROMPT_SKILL_BASED` (mentions the six bundled skills
  via `$<name>` for explicit activation) for `codex` and `gemini`.
  All `POST_INLINE_*` suffix logic stays shared and is verified
  across all three CLIs via parameterised tests.
- Renamed `codex_agents/` to `pr_review_agents/` (and then on to
  `skills/`) — the directory is shared by every file-based CLI, so
  the codex-specific name was misleading. The single-module
  `cc_pr_reviewer.py` is now a package (`cc_pr_reviewer/__init__.py`)
  so the bundled agent files ship with the wheel.

### Fixed
- **Snapshot/restore the worktree before materialising skills.**
  `_materialise_skills` previously overwrote any pre-existing
  `.agents/skills/<our-name>/SKILL.md` in the PR's worktree, and
  `_cleanup_skills` then unlinked it — so a PR that ships its own
  competing skill of the same name would end the review with that
  tracked file deleted or modified. Now we snapshot existing bytes
  and parent-dir presence into a `_MaterialisedSkills` manifest and
  restore byte-for-byte in `finally`. Parent dirs are only `rmdir`'d
  if we created them, so sibling user content and pre-existing
  `.agents/` trees stay intact.
- **Codex network access restored for post-inline reviews.** Codex's
  `--sandbox workspace-write` blocks network by default, so the
  post-inline review path's `gh api …` calls were silently failing
  to publish inline comments. The `-c sandbox_workspace_write.network_access=true`
  override keeps the filesystem sandbox but restores network.
  `--yolo` is rejected because it removes the filesystem sandbox too.
- **Bundled SKILL.md frontmatter validated at startup.**
  `check_prereqs` reads the first 512 bytes of each bundled SKILL.md
  and validates frontmatter shape — a half-extracted wheel or bad
  merge produces a clear startup blocker instead of a mid-review
  "skill not found" from codex/gemini.
- `_materialise_skills` wraps `shutil.copy2` with a `RuntimeError`
  naming the offending skill; bare shutil tracebacks gave no clue
  which of six writes failed.
- `_cleanup_skills` uses explicit `suppress(FileNotFoundError)`
  instead of `missing_ok=True`, and a new `_rmdir_if_empty` helper
  narrows the previous broad `suppress(OSError)` to ENOTEMPTY-only
  so other OSError subtypes propagate from `finally` instead of
  masking the originating launch exception.
- `strip_bundled_frontmatter` raises when a bundled SKILL.md is
  missing our frontmatter (was a silent no-op that would let a
  defective file pass the upstream-drift check as "in sync").
- **Explicit `encoding="utf-8"` on all `read_text` / `write_text`
  calls** in `scripts/sync_pr_review_agents.py` — the bundled agent
  files contain em-dashes and Unicode bullets that Windows would
  otherwise mis-decode under the locale codepage.
- **`_pr_review_toolkit_enabled()` no longer freezes the TUI.** Added
  `timeout=5` plus `FileNotFoundError`/`TimeoutExpired` handling
  returning `None`, and the call site in `_launch_claude` now
  surfaces `None` distinctly from `False` (warning toast: "couldn't
  determine plugin status — proceeding") instead of silently
  bypassing the toolkit-prompt check.
- **`_persisted_cli` exception coverage matches its docstring.** The
  whole DB sequence (including `mkdir(parents=True)` inside
  `_open_review_db` and `_get_setting()`) is now wrapped to catch
  both `sqlite3.Error` and `OSError` — previously a read-only
  workspace crashed startup before the TUI mounted.
- **No-CLI-available TOCTOU race.** If every supported CLI is
  uninstalled between `check_prereqs` and `PRReviewer.__init__`,
  `on_mount` now surfaces a high-severity persistent toast instead
  of silently waiting for the user to discover the problem at first
  Enter-to-review.

## [0.11.1] — 2026-05-08

### Changed
- **Header style refresh.** Folded the standalone PR-count and version
  `Static` widgets into existing header pieces — the count now rides on
  `app.title` as a bold-accent `[N to review]` suffix, and the version
  joins the Release Notes link as `(vX.Y.Z)`. A new `App.format_title`
  override two-tones the title prefix (`bold $primary`) and the bracketed
  count (`bold $accent`), preserving the previous Static's emphasis
  without the docked-widget overlap workarounds. The title is renamed
  from "GitHub PR Reviewer" to "CC PR Reviewer" and the subtitle now
  prepends "Github", so the header reads "CC PR Reviewer [N to review] —
  Review Github PRs with Claude Code  📝 Release Notes (vX.Y.Z)".

## [0.11.0] — 2026-05-08

### Added
- **Cross-instance "in-progress" indicator.** PRs currently being
  reviewed by another `cc-pr-reviewer` tab or host show a bold-yellow
  `⟳` glyph in the Reviews cell. Each instance reserves a row in a
  new `reviews_in_progress` SQLite table for the duration of its
  `claude` subprocess; peers poll the table every 3 s. Pressing Enter
  on a held PR opens a warn modal naming the holding PID/host with an
  explicit "review anyway" override; on confirm the override is gated
  on the holder identity captured at modal-open, so a peer that
  reserved between modal-open and confirm re-prompts instead of being
  silently evicted. Stale rows from crashed peers self-heal via a PID
  liveness check on read.
- **Header "N to review" count** next to the version label, so the
  current workload is visible at a glance without scanning the status
  bar. Counts only review-requested PRs (excludes `_mine=True` rows
  when the `m` toggle is on); renders "…" during fetch and "?" on
  fetch error so a stale value never lingers. When `m` is on and
  authored PRs are visible, a `(+N mine)` suffix is appended so the
  header matches the visible row count.

### Changed
- New `InProgressHolder(pr_key, pid, hostname, started_at)` dataclass
  replaces the `dict[str, Any]` cast-on-every-read pattern at all the
  in-progress call sites; `ReviewInProgressError` and
  `InProgressWarnScreen` carry the holder directly.

### Fixed
- Reserve scope now wraps the entire `App.suspend()` block (was only
  around `subprocess.call`), so two tabs can't race past `gh pr
  checkout --force` before either reserves — the very race this
  feature exists to prevent.
- Stale-row sweep `DELETE` adds `pid` to the `WHERE` clause so a
  same-host crash-and-restart race can't wipe a fresh holder's row.
- `_pid_alive` returns `True` on Windows (POSIX `os.kill(_, 0)` raises
  there for every PID); without this the feature silently degraded to
  per-instance.
- Hostname is captured once at module import (`_APP_HOSTNAME`) so a
  DHCP/`hostnamectl`/`scutil` rename mid-session can't leak a
  permanent orphan reservation.
- `_poll_in_progress` runs on a worker thread to avoid up to 5 s UI
  freeze on contended or NFS-hosted DBs; the worker connection opens
  with `check_same_thread=False` (safe — WAL + `busy_timeout` serialise
  writers, Python's sqlite3 takes a per-connection mutex around
  execute/commit, and no helper holds an open transaction across
  threads). Poll errors surface via `self.notify` with a dedupe latch
  instead of clobbering the keybinding cheatsheet.
- On a poll error the cached `self._in_progress` snapshot is preserved
  rather than cleared, so the `⟳` cell glyph and `action_review` gate
  stay in sync; a stale toast still tells the user the snapshot is
  stale.
- `action_review` consults the cached snapshot instead of issuing a
  synchronous `_load_in_progress` on Enter, which had reintroduced the
  same up-to-5 s freeze the worker thread was meant to remove. The
  `_launch_claude` reserve is the actual safety boundary; the cache is
  a UX optimisation to show the warn modal early.
- Dropped `contextlib.suppress(sqlite3.Error)` from the
  `_load_in_progress` sweep `DELETE`; both callers already wrap the
  call in `try/except sqlite3.Error` and route the failure (abort +
  toast in `action_review`, deduped warning in `_poll_in_progress`),
  so the suppress was hiding signals rather than handling them.
- `_release_in_progress` now logs to stderr on failure rather than
  silently swallowing — matches the no-silent-fallback policy.
- `_load_prs`'s exception path resets the header count to "?" so the
  "…" placeholder set by `action_refresh` and `action_toggle_mine`
  doesn't linger forever after a network failure.

## [0.10.1] — 2026-05-05

### Changed
- **Prompt assembly extracted from `_launch_claude`.** The conditional
  layering of POST_INLINE suffixes had grown to five suffixes gated on
  three binary inputs (`post_inline`, `fetch_ok`, rereview, rereview-can-
  approve), making it the most regression-prone block in the file. It
  now lives in a pure `build_review_prompt(...)` helper returning a
  `BuiltPrompt` dataclass, shrinking the call site from ~37 lines to ~8.
  No user-visible change to the assembled prompt.

### Added
- Pytest suite under `tests/` covering the four pure helpers:
  `build_review_prompt` (9 cases over the full suffix matrix, including
  the rereview-from-raw-list path and the GitHub-422 self-approval
  gate), `_is_newer`/`_parse_semver` (regression guard against lexical
  compare on `0.10.0` vs `0.9.0`), `_review_cell`, and
  `format_existing_comments`. `pytest>=8.0` is wired into dev deps and
  `testpaths = ["tests"]` is set in `pyproject.toml`.

### Fixed
- When `gh api user` fails (network glitch, auth blip), the launch
  banner now explicitly says rereview detection is unavailable instead
  of reading identically to a clean first-review launch — the prior
  path silently dropped bar-raising on someone else's PR with the
  warning easily lost in clone/checkout output.
- Whitespace-only `extra_prompt` can no longer render an empty
  "Additional instructions from reviewer:" header followed by nothing;
  `build_review_prompt` strips defensively in addition to the existing
  `ConfirmResult.__post_init__` strip.
- A contradictory `(fetch_ok=False, existing=non-empty)` input to
  `build_review_prompt` now asserts at the boundary, so a future caller
  or test stub can't silently swap DEDUP_SUFFIX for the FETCH_FAILED
  warning.

## [0.10.0] — 2026-05-05

### Added
- **Auto-resolve own addressed threads before APPROVE.** When the
  post-inline rereview path auto-submits an APPROVE on someone else's
  PR (only NIT-level findings remain), the prompt now instructs Claude
  to first resolve our own previously-posted review threads via the
  GraphQL `resolveReviewThread` mutation, so the approval doesn't
  visually contradict still-open threads. Scope is deliberately narrow:
  only threads whose root comment author is the current `gh` user, and
  only the ones the current PR code has actually addressed.

### Fixed
- Hardened the GraphQL plumbing the auto-resolve suffix drives:
  `pullRequestReviewThreads` is paginated with `first: 100` plus
  `pageInfo` (the default page would silently drop threads on
  long-lived PRs); query failures fall through to "skip resolve, note
  in summary, still APPROVE" rather than abort/retry or fall back to
  the inline existing-comments list; `resolveReviewThread` mutations
  count as success only when the response has no top-level `errors`
  AND `thread.isResolved == true`; an audit-trail summary
  (resolved / judged-not-addressed / failed) is required so partial
  failures aren't silent.

## [0.9.0] — 2026-05-04

### Added
- **Header version label**: the installed version is now shown on the
  right side of the TUI header, next to the Release Notes link and
  clock.
- **Toast feedback for the upgrade key**: pressing `u` now surfaces
  "Checking…", "Already up to date (vX.Y.Z).", or an error pointing at
  the releases URL — matching the toast pattern used by `g`/`s` —
  instead of conflating those states under "No update available (or
  check is still in progress)."

### Fixed
- Source installs (running from a checkout) get a dedicated
  "unavailable" state, so pressing `u` no longer shows a misleading
  "check failed" toast; the PyPI worker is also skipped on mount in
  that case.
- Header version label and update-available badge are wrapped in
  `Text(...)` so a PEP 440 local segment or untrusted PyPI version
  string can't break Rich markup parsing during compose/render.
- A late PyPI worker callback firing after teardown can no longer raise
  `NoMatches` on `#version-badge`.

## [0.8.0] — 2026-04-29

### Added
- **Group PRs by repo or author**: new `g` keybinding cycles through
  `none → repo → author → none`, inserting non-selectable group-header
  rows into the table. The cursor seeks back to the previously selected
  PR when toggling, so you don't get stranded on a header row. The
  chosen mode is persisted across restarts and reflected by the footer
  highlight on `g`.
- **Sort by updated time**: new `s` keybinding toggles a "most recently
  updated first" ordering at render time. The underlying `self.prs`
  list is kept in fetch order; sorting only affects how rows are laid
  out, so grouping and filtering remain composable. Also persisted.
- **Colourised diff modal**: the `d` diff view now mirrors `git diff`
  styling — file headers, hunk headers, and added/removed lines are
  syntax-highlighted for faster scanning.

### Fixed
- Tightened the diff file-header regex so content lines that happen to
  start with `diff `, `+++`, or `---` are no longer mis-coloured as
  headers.

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

[0.12.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.12.0
[0.11.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.11.1
[0.11.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.11.0
[0.10.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.10.1
[0.10.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.10.0
[0.9.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.9.0
[0.8.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.8.0
[0.7.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.7.0
[0.6.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.6.0
[0.5.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.5.0
[0.4.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.4.0
[0.3.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.3.0
[0.2.2]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.2
[0.2.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.1
[0.2.0]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.2.0
[0.1.1]: https://github.com/jasmedia/cc-reviewer/releases/tag/v0.1.1
