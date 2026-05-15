# cc-pr-reviewer

[![PyPI Downloads](https://static.pepy.tech/badge/cc-pr-reviewer)](https://pepy.tech/projects/cc-pr-reviewer)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A small Textual TUI that lists every open GitHub PR where you are a
requested reviewer, and hands the selected PR off to a coding-agent CLI
of your choice — **Claude Code**, **OpenAI Codex CLI**, or **Google
Gemini CLI** — to perform the review. Claude Code uses the
[PR Review Toolkit](https://claude.com/plugins/pr-review-toolkit) plugin;
Codex and Gemini load the same six review criteria as native **Skills**
that ship with this package (`cc_pr_reviewer/skills/<name>/SKILL.md`),
materialised into the checked-out PR workspace under `.agents/skills/`
for the duration of each review.

![cc-pr-reviewer screenshot](assets/cc-reviewer.png)

## What it does

- `gh search prs --review-requested=@me --state=open` fetches your review
  queue across every repo you have access to.
- Displays them in a scrollable table (repo, number, title, author, age,
  draft flag).
- Keyboard-driven: pick a PR and press **Enter** to open a confirmation
  modal; on **Enter/y** it clones the repo (if needed), checks out the PR
  branch via `gh pr checkout`, and launches the selected coding-agent CLI
  inside that working tree with a prompt that drives a six-dimension PR
  review (code-quality, silent-failure hunting, type design, test
  coverage, comment quality, simplification opportunities). See
  [Keybindings](#keybindings) for the full list.
- **Three CLI backends, switchable from the TUI.** Press `c` to cycle
  the global default (`claude → codex → gemini → claude`, persisted
  across sessions), or `Ctrl+L` inside the confirm modal to override
  for a single launch. Default is Claude Code.

## Prerequisites

All prerequisites are validated at startup via `check_prereqs()`; the TUI
refuses to launch until they're satisfied.

1. **GitHub CLI** — installed and logged in.
   ```sh
   gh auth login
   ```
2. **At least one coding-agent CLI** on your `PATH`:
   - **Claude Code** (`claude`) — additionally needs the **PR Review Toolkit**
     plugin installed and enabled. Install via `claude plugin install pr-review-toolkit`;
     details at <https://claude.com/plugins/pr-review-toolkit>. The plugin
     check fires at *launch* time (not startup), so you can still start
     the TUI even if the plugin is missing — you'd just see a clear toast
     when you try to review with Claude. Codex/Gemini are unaffected.
   - **OpenAI Codex CLI** (`codex`) — <https://github.com/openai/codex>
   - **Google Gemini CLI** (`gemini`) — <https://github.com/google-gemini/gemini-cli>

   Startup only requires *one* of the three to be installed. If the CLI you
   previously persisted (via `c`) isn't on this machine's PATH, the TUI
   transparently falls back to the next available one for the session and
   shows a warning toast. Switch the global default any time with `c`.
3. **git** — on your `PATH` (used for `git fetch` on repeat reviews).

## Install

### As a global CLI (recommended)

Install from PyPI with [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/):

```sh
uv tool install cc-pr-reviewer
# or
pipx install cc-pr-reviewer
```

Then run it from anywhere:

```sh
cc-pr-reviewer
```

### From source (for development)

```sh
uv sync
uv run cc-pr-reviewer
```

## Configuration

| Env var            | Default               | Meaning                                    |
| ------------------ | --------------------- | ------------------------------------------ |
| `GH_PR_WORKSPACE`  | `~/gh-pr-workspace`   | Where repos are cloned for local checkout. |

Clones are organised as `$GH_PR_WORKSPACE/<owner>/<repo>`, so a second
review of the same repo reuses the existing clone and just `git fetch`es
before checking out the PR.

## Keybindings

| Key           | Action                                                       |
| ------------- | ------------------------------------------------------------ |
| `↑` / `↓`     | Move through PRs                                             |
| `Enter`       | Confirm, then clone + checkout + launch the selected CLI     |
| `d`           | View full diff                                               |
| `o`           | Open PR in browser                                           |
| `m`           | Toggle inclusion of PRs you authored                         |
| `f`           | Filter the list by repo (picker)                             |
| `g`           | Cycle grouping: none → repo → author → none                  |
| `s`           | Toggle sort by most recently updated                         |
| `c`           | Cycle CLI: claude → codex → gemini → claude (persisted)      |
| `r` / `F5`    | Refresh the list                                             |
| `u`           | Upgrade `cc-pr-reviewer` via `uv tool upgrade`               |
| `q`           | Quit                                                         |

The `c` key highlights green in the footer whenever the active CLI is
non-default (i.e. anything other than Claude Code).

Inside the confirmation modal: `Enter` / `Ctrl+Y` to proceed, `Esc` /
`Ctrl+N` to cancel, `Ctrl+T` to toggle post-inline (instruct the CLI to
publish the findings as inline PR review comments via `gh api`, grouped
under one review), `Ctrl+L` to cycle the CLI for **this launch only**
(without changing the global default). The post-inline toggle defaults
to on each time the modal opens; the CLI defaults to whatever the
global `c` setting currently is. The chosen values are printed before
the CLI launches.

Inside the filter modal: arrow keys to move, `Enter` to apply the
highlighted repo (or pick **(any repo — clear filter)** to remove the
filter), `r` to re-fetch the unfiltered PR list and pick up repos that
appeared after boot, `Esc` to cancel. The repo list comes from the most
recent unfiltered fetch — applying a filter doesn't shrink the picker.
The active filter is persisted in `$GH_PR_WORKSPACE/.review_state.db`
and restored on the next launch.

## How the launch works

When you press **Enter** on a row, a confirmation modal shows the target
PR (`repo#N` + title) plus the current CLI selection. On **Enter/y** the
TUI suspends itself and runs, in order:

```sh
gh repo clone <owner>/<repo>                      # only if not already cloned
git fetch --all --prune                           # otherwise
gh pr checkout <N> --force
<selected-cli> <auto-approve flags> "<review prompt>"
```

The exact CLI invocation depends on which backend is active:

| CLI    | Command                                                                                                                |
| ------ | ---------------------------------------------------------------------------------------------------------------------- |
| Claude | `claude --permission-mode acceptEdits "<prompt>"`                                                                      |
| Codex  | `codex --ask-for-approval never --sandbox workspace-write -c sandbox_workspace_write.network_access=true "<prompt>"`   |
| Gemini | `gemini --approval-mode auto_edit "<prompt>"`                                                                          |

All three are launched in modes that auto-accept file edits inside the
cloned PR workspace, mirroring Claude's `acceptEdits` posture as
closely as each CLI permits. For Codex, `sandbox_workspace_write` blocks
network by default; the `-c sandbox_workspace_write.network_access=true`
override restores it so the post-inline review path can reach GitHub
via `gh api`.

The review prompt drives the same six review dimensions across all three
CLIs:

- **Claude Code** delegates to the PR Review Toolkit plugin's six
  sub-agents (Comment Analyzer, PR Test Analyzer, Silent Failure Hunter,
  Type Design Analyzer, Code Reviewer, Code Simplifier).
- **Codex** and **Gemini** load the same six review criteria as native
  Skills. The bundled `SKILL.md` files
  (`cc_pr_reviewer/skills/<name>/SKILL.md`) are an adapted fork of the
  upstream toolkit's agent prompts with Codex/Gemini Skills frontmatter
  prepended. At launch they're copied into
  `<workspace>/.agents/skills/<name>/SKILL.md` (the cross-tool interop
  path both CLIs are *documented* to scan at session start — refs:
  [Codex Skills](https://developers.openai.com/codex/skills),
  [Gemini Skills](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/skills.md))
  and removed on exit. Per those docs, only metadata is loaded
  eagerly — full instructions are pulled in on activation.

If the modal's **post-inline** toggle (`Ctrl+T`) is on, the prompt is
extended to ask the CLI to publish each finding as an inline review
comment via a single `POST /repos/{owner}/{repo}/pulls/{n}/reviews` call
through `gh api`, so they land grouped under one review. On a re-review
of someone else's PR where only NIT-level findings remain, the CLI is
also asked to resolve any of its own previously-posted threads that the
new code has addressed before submitting the APPROVE.

When you exit the CLI, press Enter and the TUI returns.

### Keeping the bundled review prompts in sync with upstream

The bundled `cc_pr_reviewer/skills/<name>/SKILL.md` files are a static
fork of the [PR Review Toolkit](https://claude.com/plugins/pr-review-toolkit)
plugin's agent prompts (with Codex/Gemini Skills frontmatter prepended).
To check for upstream changes:

```sh
uv run python scripts/sync_pr_review_agents.py --update-plugin
```

This runs `claude plugin update pr-review-toolkit@claude-plugins-official`
first, then diffs the current upstream against a committed baseline
snapshot in `scripts/upstream_baseline/`. Exit code is `0` if nothing
changed upstream, `1` if action is needed. After triaging upstream
changes into the bundled files, run `--save-baseline` to lock in the new
upstream state. See the script's `--help` for additional flags (`--diff`,
`--write`).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the release history.
