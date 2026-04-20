# cc-pr-reviewer

A small Textual TUI that lists every open GitHub PR where you are a
requested reviewer, and hands the selected PR off to Claude Code with the
[PR Review Toolkit](https://claude.com/plugins/pr-review-toolkit) plugin
driving the review.

## What it does

- `gh search prs --review-requested=@me --state=open` fetches your review
  queue across every repo you have access to.
- Displays them in a scrollable table (repo, number, title, author, age,
  draft flag).
- Keyboard-driven: pick a PR and either
  - **Enter / c** — clones the repo (if needed), checks out the PR branch
    via `gh pr checkout`, and launches `claude` inside that working tree
    with a prompt that invokes the PR Review Toolkit agents.
  - **d** — full-screen `gh pr diff` viewer.
  - **o** — open the PR in your browser.
  - **m** — include PRs you authored in the list.
  - **a** — toggle auto-accept (pass `--permission-mode acceptEdits` to
    Claude so file-edit prompts don't interrupt the review).
  - **p** — toggle post-inline (instruct Claude to publish the findings
    as inline PR review comments via `gh api`, grouped under one review).
  - **r / F5** — refresh.
  - **q** — quit.

## Prerequisites

All prerequisites are validated at startup via `check_prereqs()`; the TUI
refuses to launch until they're satisfied.

1. **GitHub CLI** — installed and logged in.
   ```sh
   gh auth login
   ```
2. **Claude Code** — the `claude` CLI must be on your `PATH`.
3. **PR Review Toolkit plugin** — installed and enabled inside Claude Code
   (detected via `claude plugin list --json`):
   ```sh
   claude plugin install pr-review-toolkit
   ```
   Details: <https://claude.com/plugins/pr-review-toolkit>
4. **git** — on your `PATH` (used for `git fetch` on repeat reviews).

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
# or
uv run python cc_pr_reviewer.py
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
| `Enter` / `c` | Clone + checkout + launch Claude Code review                 |
| `d`           | View full diff                                               |
| `o`           | Open PR in browser                                           |
| `m`           | Toggle inclusion of PRs you authored                         |
| `a`           | Toggle auto-accept (`--permission-mode acceptEdits`)         |
| `p`           | Toggle post-inline (publish findings as inline PR comments)  |
| `r` / `F5`    | Refresh the list                                             |
| `q`           | Quit                                                         |

The current state of the `a` and `p` toggles is shown in the status bar
and is also printed before Claude launches.

## How the Claude launch works

When you press **Enter** on a row, the TUI suspends itself and runs, in order:

```sh
gh repo clone <owner>/<repo>                      # only if not already cloned
git fetch --all --prune                           # otherwise
gh pr checkout <N> --force
claude [--permission-mode acceptEdits] "<review prompt>"
```

The review prompt asks the PR Review Toolkit to run its six sub-agents
(Comment Analyzer, PR Test Analyzer, Silent Failure Hunter, Type Design
Analyzer, Code Reviewer, Code Simplifier). Because Claude Code starts in
the PR's working tree, it has full file-level context.

If **auto-accept** (`a`) is on, `--permission-mode acceptEdits` is passed
so file-edit prompts don't interrupt the review — the same mode you get
with shift+tab inside a Claude session.

If **post-inline** (`p`) is on, the prompt is extended to ask Claude to
publish each finding as an inline review comment via a single
`POST /repos/{owner}/{repo}/pulls/{n}/reviews` call through `gh api`, so
they land grouped under one review.

When you `/exit` Claude, press Enter and the TUI returns.

## Extending

A few natural next steps if you want to go further:

- Add filters (by org, by repo, by author) — `gh search prs` already
  accepts `--owner`, `--repo`, `--author`.
- Cache the PR list to disk with a TTL so startup is instant.
- Add a "my authored PRs" tab (`--author=@me`) to re-use the same UI for
  tracking your own open PRs.
- Swap the hard-coded `REVIEW_PROMPT` for a couple of presets bound to
  different keys (e.g. `t` for tests-only, `s` for simplify-only).

## Releasing

Releases are automated via two GitHub Actions workflows:

1. **Release** (`.github/workflows/release.yml`) — `workflow_dispatch` with a
   `bump` input (`patch` / `minor` / `major`). It runs `uv version --bump`,
   commits `pyproject.toml` + `uv.lock` as `chore(release): X.Y.Z`, tags
   `vX.Y.Z`, pushes both to `main`, and creates a GitHub release with
   auto-generated notes. Requires a `RELEASE_TOKEN` secret (PAT with
   Contents: read/write, allowlisted on branch protection so the bot can
   push to `main`).
2. **Publish to PyPI** (`.github/workflows/publish.yml`) — triggered by
   `release: published`. Builds sdist + wheel with `uv build`, validates
   with `twine check`, and publishes via `uv publish --trusted-publishing
   always`. No token is stored; PyPI's Trusted Publisher config (pypi.org
   → Manage → Publishing) authorises this repo/workflow/environment
   (`pypi`) via OIDC.

To cut a release: go to Actions → **Release** → Run workflow, pick the
bump type. When it finishes, the publish workflow runs automatically on
the resulting GitHub release. Verify from a clean environment:

```sh
uv tool install cc-pr-reviewer
cc-pr-reviewer
```

Manual fallback (if you ever need to publish locally): `uv build && uvx
twine check dist/* && uv publish` with `UV_PUBLISH_TOKEN` set to a PyPI
API token.
