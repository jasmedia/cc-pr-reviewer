"""Compare bundled review-agent prompts against the upstream Claude plugin.

The bundled `.md` files in `cc_pr_reviewer/pr_review_agents/` are a static
fork of the `pr-review-toolkit` Claude plugin's agent prompts, adapted
for Codex and Gemini (which have no plugin marketplace, so they read the
prompts as files).

`claude plugin update pr-review-toolkit@claude-plugins-official` reports
the plugin version as `unknown`, so the only way to detect upstream
drift is by content diff. This script normalises each upstream file
(strips the YAML frontmatter and any `## When to invoke` section — the
two structural strips we always apply when bundling), then diffs the
normalised upstream against the bundled version.

What's left in the diff is a mix of:

  * our prose adaptations (CLAUDE.md → CLAUDE.md/AGENTS.md, PR-focused
    scope phrasing, project-specific logging refs softened, etc.) —
    present every run, and
  * any genuine upstream changes — what you actually want to triage and
    bring across.

The persistent-adaptation diff acts as a baseline; when its line-count
goes up between runs, that's the signal upstream changed.

Usage:

    uv run python scripts/sync_pr_review_agents.py           # one-line per file
    uv run python scripts/sync_pr_review_agents.py --diff    # full unified diff
    uv run python scripts/sync_pr_review_agents.py --write   # overwrite bundled
                                                             # with normalised
                                                             # upstream (destructive
                                                             # — review via git diff)
    uv run python scripts/sync_pr_review_agents.py --upstream <dir>
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from cc_pr_reviewer import REVIEW_AGENT_FILES, _review_agents_dir

DEFAULT_UPSTREAM = (
    Path.home()
    / ".claude/plugins/marketplaces/claude-plugins-official"
    / "plugins/pr-review-toolkit/agents"
)

# YAML frontmatter at the very top: `---` line, body (non-greedy), `---`
# line, trailing newlines. Anchored to the file start so a stray `---`
# hrule in the middle of the body can't accidentally match.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n+", re.DOTALL)

# "## When to invoke" section: the H2 heading and everything until the
# next H2 (or EOF). MULTILINE so `^` matches at line starts; DOTALL so
# `.` spans newlines. Stops at the next H2 rather than any heading, so a
# nested H3 inside the section is correctly swept along.
_WHEN_TO_INVOKE_RE = re.compile(
    r"^## When to invoke.*?(?=^## |\Z)",
    re.DOTALL | re.MULTILINE,
)


def normalise_upstream(text: str) -> str:
    """Apply the structural strips we always apply when bundling.

    Drops:
      * the YAML frontmatter block at the top (Claude's per-agent
        declaration — irrelevant outside Claude Code), and
      * the `## When to invoke` section (describes sub-agent-dispatch
        scenarios — doesn't apply to Codex/Gemini's flat invocation).

    Prose adaptations (CLAUDE.md → CLAUDE.md/AGENTS.md, scope phrasing,
    project-specific logging refs) are judgment-call edits and stay
    visible in the diff so the maintainer sees them.
    """
    text = _FRONTMATTER_RE.sub("", text)
    text = _WHEN_TO_INVOKE_RE.sub("", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        default=DEFAULT_UPSTREAM,
        help=f"path to the upstream plugin's agents/ dir (default: {DEFAULT_UPSTREAM})",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="print the full unified diff for each drifted file",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "DESTRUCTIVE: overwrite each drifted bundled file with the "
            "normalised upstream content, discarding our prose adaptations. "
            "Intended as a 'reset and re-adapt' escape hatch for big "
            "upstream rewrites — review with `git diff` and re-apply "
            "adaptations manually before committing."
        ),
    )
    args = parser.parse_args()

    if not args.upstream.is_dir():
        print(f"error: upstream agents dir not found: {args.upstream}", file=sys.stderr)
        print(
            "       run `claude plugin update "
            "pr-review-toolkit@claude-plugins-official` or pass --upstream <dir>",
            file=sys.stderr,
        )
        return 2

    local_dir = _review_agents_dir()
    in_sync = drifted = rewrote = missing = 0

    for name in REVIEW_AGENT_FILES:
        upstream_path = args.upstream / name
        local_path = local_dir / name

        if not upstream_path.is_file():
            print(f"  MISSING UPSTREAM  {name}")
            missing += 1
            continue
        if not local_path.is_file():
            print(f"  MISSING BUNDLED   {name}")
            missing += 1
            continue

        normalised = normalise_upstream(upstream_path.read_text())
        local = local_path.read_text()

        if normalised == local:
            print(f"  in sync           {name}")
            in_sync += 1
            continue

        # `--write` mode overwrites with the normalised upstream and skips
        # the inline diff (the user reviews via `git diff`, which gives
        # better tooling — coloured output, per-hunk staging, undo via
        # `git checkout --`). Counts as "rewrote", not "drift", so the
        # final summary reflects post-write state.
        if args.write:
            local_path.write_text(normalised)
            print(f"  rewrote           {name}")
            rewrote += 1
            continue

        diff_lines = list(
            difflib.unified_diff(
                normalised.splitlines(keepends=True),
                local.splitlines(keepends=True),
                fromfile=f"upstream(normalised)/{name}",
                tofile=f"bundled/{name}",
                n=3,
            )
        )
        # Count only +/- payload lines (skip the +++/--- file headers)
        # as a quick "how much drift" gauge that's stable across runs.
        delta = sum(
            1
            for ln in diff_lines
            if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
        )
        print(f"  drift ({delta:>3} lines) {name}")
        drifted += 1

        if args.diff:
            sys.stdout.writelines(diff_lines)
            print()

    # Surface any upstream files that aren't in our manifest — they'd
    # represent a new agent the toolkit added since we forked, which is
    # worth a human deciding whether to bundle.
    bundled_names = set(REVIEW_AGENT_FILES)
    extras = sorted(p.name for p in args.upstream.glob("*.md") if p.name not in bundled_names)
    if extras:
        print()
        print("upstream has agents not in our manifest (new since fork?):")
        for name in extras:
            print(f"  + {name}")

    print()
    parts = [f"{in_sync} in sync", f"{drifted} drifted", f"{missing} missing"]
    if args.write:
        parts.insert(2, f"{rewrote} rewrote")
    print(f"summary: {', '.join(parts)}")
    if drifted and not args.diff:
        print("re-run with --diff to see the changes")
    if rewrote:
        print(
            "review the overwrites with `git diff cc_pr_reviewer/pr_review_agents/` "
            "and re-apply adaptations before committing"
        )
    return 1 if (drifted or missing or extras) else 0


if __name__ == "__main__":
    raise SystemExit(main())
