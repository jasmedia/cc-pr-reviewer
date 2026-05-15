"""Compare bundled review-agent prompts against the upstream Claude plugin.

The bundled `.md` files in `cc_pr_reviewer/pr_review_agents/` are a static
fork of the `pr-review-toolkit` Claude plugin's agent prompts, adapted
for Codex and Gemini (which have no plugin marketplace, so they read the
prompts as files).

`claude plugin update pr-review-toolkit@claude-plugins-official` reports
the plugin version as `unknown`, so the only way to detect upstream
drift is by content diff. This script normalises each upstream file
(strips the YAML frontmatter and any `## When to invoke` section — the
two structural strips we always apply when bundling), then compares
against two reference points:

  1. The **baseline snapshot** (`scripts/upstream_baseline/*.md`) — a
     committed copy of the normalised upstream as of the last sync.
     Diffing current upstream against this catches **upstream changes**
     in isolation (no adaptation noise).
  2. The **bundled file** (`cc_pr_reviewer/pr_review_agents/*.md`) —
     diffing current upstream against this shows the full gap
     (upstream changes + our prose adaptations).

The baseline is the load-bearing piece: it converts a per-run "did the
line counts grow?" eyeball check into a zero-effort "UPSTREAM CHANGED"
signal that's reliable across machines and contributors.

Typical workflows:

    # Daily / weekly drift check (auto-runs the plugin updater first).
    uv run python scripts/sync_pr_review_agents.py --update-plugin

    # If upstream changed, inspect what's new.
    uv run python scripts/sync_pr_review_agents.py --diff

    # Triage upstream changes into the bundled files, then lock in the
    # new upstream state as the new baseline.
    uv run python scripts/sync_pr_review_agents.py --save-baseline

Reset-and-re-adapt escape hatch (rare):

    # Overwrite bundled with normalised upstream, discarding our
    # adaptations. Review via `git diff` and re-apply manually.
    uv run python scripts/sync_pr_review_agents.py --write
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

from cc_pr_reviewer import REVIEW_AGENT_FILES, _review_agents_dir

DEFAULT_UPSTREAM = (
    Path.home()
    / ".claude/plugins/marketplaces/claude-plugins-official"
    / "plugins/pr-review-toolkit/agents"
)

# Baseline lives next to this script (outside the wheel package — see
# `pyproject.toml`'s `packages = ["cc_pr_reviewer"]`, which scopes
# distribution to that dir). Committing the baseline means a fresh
# clone already has a reference point — contributors don't need to
# bootstrap one per machine.
BASELINE_DIR = Path(__file__).resolve().parent / "upstream_baseline"

# Plugin ID format the marketplace uses for `claude plugin update`.
# Surfaces as a single constant so the docstring, the error message,
# and the actual subprocess call can't drift apart.
PLUGIN_ID = "pr-review-toolkit@claude-plugins-official"

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


def _run_plugin_update() -> None:
    """Invoke `claude plugin update` so the local upstream files are fresh.

    Failures are warned but non-fatal: the plugin may already be at the
    latest version (claude reports rc=0), the user may be offline, or
    `claude` may not be on PATH. In every case we still want the rest
    of the script to run against whatever upstream content is present —
    so a missing-binary or network error becomes a stderr warning, not
    a hard exit.
    """
    # `flush=True` so the heading appears above claude's own stdout when
    # both share a TTY; without it Python's buffered stdout flushes only
    # at exit and the heading prints out-of-order under the subprocess.
    print(f"running `claude plugin update {PLUGIN_ID}`…", flush=True)
    try:
        rc = subprocess.call(["claude", "plugin", "update", PLUGIN_ID])
    except FileNotFoundError:
        print(
            "warning: `claude` not on PATH; skipping plugin update "
            "and continuing with the current upstream content",
            file=sys.stderr,
        )
        return
    if rc != 0:
        print(
            f"warning: `claude plugin update {PLUGIN_ID}` exited with status "
            f"{rc}; continuing with the current upstream content",
            file=sys.stderr,
        )


def _save_baseline(args: argparse.Namespace) -> int:
    """Write current normalised upstream into the baseline snapshot dir.

    Used both for first-time setup and post-triage refresh — the two
    are functionally identical (overwrite whatever's there with the
    current upstream), only the intent differs. One flag covers both.
    """
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    wrote = skipped = 0
    for name in REVIEW_AGENT_FILES:
        upstream_path = args.upstream / name
        if not upstream_path.is_file():
            print(f"  SKIP (no upstream)  {name}", file=sys.stderr)
            skipped += 1
            continue
        # Explicit utf-8: agent prompts contain em-dashes and Unicode
        # bullets, and Windows defaults to the locale codepage which
        # would mis-decode those silently. Same reasoning for every
        # other read_text/write_text in this file.
        normalised = normalise_upstream(upstream_path.read_text(encoding="utf-8"))
        (BASELINE_DIR / name).write_text(normalised, encoding="utf-8")
        print(f"  captured            {name}")
        wrote += 1
    print()
    print(f"wrote {wrote} baseline snapshots to {BASELINE_DIR}")
    if skipped:
        print(f"{skipped} skipped due to missing upstream — re-run after fixing the upstream dir")
    print("commit them so future runs can detect upstream drift")
    return 1 if skipped else 0


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
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help=(
            "capture the current normalised upstream as the baseline "
            "snapshot in scripts/upstream_baseline/. Run once after first "
            "sync, then again whenever you've triaged upstream changes "
            "and want to lock in the new upstream state as the new "
            "reference point."
        ),
    )
    parser.add_argument(
        "--update-plugin",
        action="store_true",
        help=(
            f"run `claude plugin update {PLUGIN_ID}` before the comparison. "
            "One-stop command for the daily drift check."
        ),
    )
    args = parser.parse_args()

    # `--save-baseline` and `--write` both mutate filesystem state but
    # touch different trees; running them together is almost always a
    # mistake (you'd snapshot upstream AND blow away the bundled
    # adaptations in the same breath). Refuse the combo loudly.
    if args.save_baseline and args.write:
        parser.error("--save-baseline and --write can't be combined")

    if args.update_plugin:
        _run_plugin_update()
        print()

    if not args.upstream.is_dir():
        print(f"error: upstream agents dir not found: {args.upstream}", file=sys.stderr)
        print(
            f"       run `claude plugin update {PLUGIN_ID}` or pass --upstream <dir>",
            file=sys.stderr,
        )
        return 2

    if args.save_baseline:
        return _save_baseline(args)

    local_dir = _review_agents_dir()
    baseline_exists = BASELINE_DIR.is_dir() and all(
        (BASELINE_DIR / name).is_file() for name in REVIEW_AGENT_FILES
    )

    in_sync = drifted = rewrote = missing = 0
    upstream_changed: list[tuple[str, str, str]] = []  # (name, baseline_text, normalised)

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

        normalised = normalise_upstream(upstream_path.read_text(encoding="utf-8"))
        local = local_path.read_text(encoding="utf-8")

        # Upstream-vs-baseline check — the load-bearing automation: when
        # this trips, the maintainer has actual work to do. The
        # upstream-vs-bundled "drift" reading below is informational
        # (mostly the persistent adaptation noise).
        if baseline_exists:
            baseline_text = (BASELINE_DIR / name).read_text(encoding="utf-8")
            if normalised != baseline_text:
                upstream_changed.append((name, baseline_text, normalised))

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
            local_path.write_text(normalised, encoding="utf-8")
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

    # ---- final report ----

    print()
    parts = [f"{in_sync} in sync", f"{drifted} drifted", f"{missing} missing"]
    if args.write:
        parts.insert(2, f"{rewrote} rewrote")
    print(f"summary: {', '.join(parts)}")

    if extras:
        print()
        print("upstream has agents not in our manifest (new since fork?):")
        for name in extras:
            print(f"  + {name}")

    if not baseline_exists:
        print()
        print(f"!  no upstream baseline at {BASELINE_DIR}")
        print("   run `--save-baseline` once to capture the current upstream as the reference")
    elif upstream_changed:
        print()
        print(f"!! UPSTREAM CHANGED since baseline ({len(upstream_changed)} file(s)):")
        for name, baseline_text, normalised in upstream_changed:
            baseline_lines = baseline_text.splitlines(keepends=True)
            current_lines = normalised.splitlines(keepends=True)
            upstream_diff = list(
                difflib.unified_diff(
                    baseline_lines,
                    current_lines,
                    fromfile=f"baseline/{name}",
                    tofile=f"upstream(normalised)/{name}",
                    n=3,
                )
            )
            delta = sum(
                1
                for ln in upstream_diff
                if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
            )
            print(f"   ! {name} ({delta} lines changed upstream)")
        print()
        print("   triage the upstream changes into the bundled files, then run")
        print("   `--save-baseline` to lock in the new upstream state as the new reference.")
        if not args.diff:
            print("   re-run with --diff to inspect the upstream changes inline.")
        else:
            print()
            for _name, baseline_text, normalised in upstream_changed:
                baseline_lines = baseline_text.splitlines(keepends=True)
                current_lines = normalised.splitlines(keepends=True)
                sys.stdout.writelines(
                    difflib.unified_diff(
                        baseline_lines,
                        current_lines,
                        fromfile=f"baseline/{_name}",
                        tofile=f"upstream(normalised)/{_name}",
                        n=3,
                    )
                )
                print()
    else:
        print("upstream unchanged since baseline")

    if drifted and not args.diff and not upstream_changed:
        # Only nudge about --diff for the bundled-vs-upstream view when
        # there's no louder upstream-changed message above; otherwise
        # we'd double-suggest --diff and confuse what it should show.
        print("re-run with --diff to see how bundled differs from upstream (mostly adaptations)")
    if rewrote:
        print(
            "review the overwrites with `git diff cc_pr_reviewer/pr_review_agents/` "
            "and re-apply adaptations before committing"
        )

    needs_action = upstream_changed or missing or extras or not baseline_exists
    return 1 if needs_action else 0


if __name__ == "__main__":
    raise SystemExit(main())
