"""CI entrypoint: audit the Solidity changed in a pull request.

Designed to run inside a PR pipeline (e.g. Azure DevOps build validation) in a
*separate* checkout of this agent repo, pointed at the checked-out contracts
repo. It:

  1. Asks git for the .sol files changed between the PR's target branch and its
     head (added / modified / renamed; deleted files are skipped).
  2. Loads those changed files as the audit target, annotated exactly like
     ``lucid.codebase`` does (``// FILE:`` markers + per-line numbers so cited
     ``file:line`` locations are real).
  3. Runs the existing lucid audit pipeline over them — single pass, context
     pass, exclusion loop, or the full specialist fan-out + judge — reusing
     ``lucid.agent`` / ``lucid.context`` / ``lucid.orchestrator`` /
     ``lucid.judge`` unchanged. The pipeline is already provider-agnostic
     (``lucid.llm`` dispatches to the API, Kiro CLI, or Codex CLI), so this
     entrypoint inherits that.
  4. Injects the unified diff into the protocol-context document so the model
     reviews the change, not just the files in isolation.
  5. Writes a JSON report, prints a Markdown summary (for the build log or a PR
     comment), and sets the process exit code from a severity gate so the
     pipeline can block a PR on, say, High-and-above findings.

Nothing here is smart-contract reasoning — that all lives in the reused
modules. This file is diff scoping, wiring, reporting, and the gate.

Usage (from the agent repo root, with the contracts repo checked out):

    python -m lucid.ci --repo /path/to/contracts --base origin/main \\
        --mode context --fail-on high --report pr-audit.json

Exit codes: 0 = clean / below gate, 1 = findings at or above the gate,
2 = operational error (bad git ref, oversized scope, provider failure).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import llm
from .agent import (
    find_bugs,
    find_bugs_loop,
    find_bugs_with_context,
)
from .codebase import MAX_CHARS, load_codebase
from .context import build_context
from .judge import judge_findings
from .orchestrator import run_specialists_parallel
from .schema import Finding

# Worst-first ordering, matching lucid.run.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Gate thresholds the pipeline can select with --fail-on. "none" never fails
# the build on findings (report-only mode).
_FAIL_ON_CHOICES = ("critical", "high", "medium", "low", "none")

# Keep the injected diff bounded so a large PR can't blow past the model's
# context on its own; the full changed-file source is already in the prompt.
_MAX_DIFF_CHARS = 60_000

# Process exit codes.
_EXIT_CLEAN = 0
_EXIT_GATE_TRIPPED = 1
_EXIT_ERROR = 2


class CIError(RuntimeError):
    """An operational failure (git, scope, provider) — distinct from findings."""


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return stdout, raising CIError on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError) as exc:
        raise CIError(f"Could not run git: {exc}") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise CIError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def changed_sol_files(repo: Path, base: str, head: str = "HEAD",
                      subdir: str | None = None) -> list[str]:
    """Return repo-relative paths of .sol files added/modified/renamed base...head.

    Uses a three-dot range so the comparison is against the merge-base of the
    target branch and the PR head — i.e. only what the PR itself changed, not
    commits that landed on the target branch meanwhile. Deleted files are
    excluded (nothing to audit). Renames report the new path.

    Args:
        repo: the contracts working directory.
        base: the target-branch ref to diff against (e.g. ``origin/main``).
        head: the PR head ref (default current ``HEAD``).
        subdir: if set, keep only paths under this repo-relative directory.
    """
    # -M detects renames; --name-status gives one status+path per line.
    out = _git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
    changed: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("D"):
            continue  # deleted — nothing to audit
        # For renames/copies (R100, C75) the new path is the last column.
        path = parts[-1]
        if not path.endswith(".sol"):
            continue
        if subdir and not (path == subdir or path.startswith(subdir.rstrip("/") + "/")):
            continue
        changed.append(path)
    # Stable, de-duplicated order.
    return sorted(dict.fromkeys(changed))


def unified_diff(repo: Path, base: str, head: str, paths: list[str]) -> str:
    """Return the unified diff for ``paths`` across base...head, size-bounded."""
    if not paths:
        return ""
    out = _git(repo, "diff", "-M", "--unified=3", f"{base}...{head}", "--", *paths)
    if len(out) > _MAX_DIFF_CHARS:
        out = out[:_MAX_DIFF_CHARS] + "\n[... diff truncated for length ...]\n"
    return out


def annotate_files(repo: Path, rel_paths: list[str]) -> str:
    """Annotate specific files like codebase.load_codebase, for a fixed file list.

    Mirrors the loader's format exactly — a ``// FILE: <rel>`` header then every
    source line prefixed with its 1-based number — so cited ``file:line``
    locations are real and the model sees the same shape it always does. Raises
    CIError if a file is missing or the combined size exceeds MAX_CHARS.
    """
    parts: list[str] = []
    total = 0
    for rel in rel_paths:
        file = repo / rel
        if not file.is_file():
            # A path can vanish between diff and read (e.g. a follow-up force
            # push); skip rather than crash the whole audit.
            continue
        source = file.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(
            f"{i}: {line}" for i, line in enumerate(source.splitlines(), start=1)
        )
        block = f"// FILE: {rel}\n{numbered}\n"
        total += len(block)
        if total > MAX_CHARS:
            raise CIError(
                f"Changed Solidity exceeds {MAX_CHARS:,} chars after adding {rel}. "
                "Narrow the PR scope, raise MAX_CHARS, or split the review."
            )
        parts.append(block)
    return "\n".join(parts)


def _context_with_diff(context: str, diff: str) -> str:
    """Append the PR's unified diff to the protocol-context document.

    The audit prompts label this input "Protocol context" and treat it as
    free-form guidance, so appending a clearly marked diff section makes the
    pass diff-aware without changing any prompt.
    """
    if not diff:
        return context
    return (
        f"{context}\n\n"
        "## Changes under review (unified diff for this pull request)\n"
        "The following diff is exactly what this PR changes. Focus your review "
        "on how these changes affect the guarantees above — new attack surface, "
        "removed checks, altered accounting — not on pre-existing code they do "
        "not touch.\n\n"
        f"```diff\n{diff}\n```"
    )


def run_audit(audit_codebase: str, *, mode: str, rounds: int,
              context_source: str, diff: str) -> list[Finding]:
    """Dispatch to the chosen lucid pipeline mode and return findings.

    Provider and model are not passed here: the reused pipeline functions read
    them from the environment through ``lucid.llm`` (the same way ``lucid.run``
    does). ``main`` sets ``LUCID_PROVIDER`` / the model env vars from the CLI
    flags before this runs, so selection still works end to end.

    Args:
        audit_codebase: annotated blob of the changed .sol files (the target).
        mode: single | context | loop | specialists.
        rounds: loop rounds, or per-lane rounds for specialists.
        context_source: blob to build protocol context from (changed files or
            the whole repo); ignored for ``single``.
        diff: unified diff, injected into context for non-single modes.
    """
    if mode == "single":
        # No context pass; audit the changed files directly.
        return find_bugs(audit_codebase)

    context = build_context(context_source)
    context = _context_with_diff(context, diff)

    if mode == "context":
        return find_bugs_with_context(audit_codebase, context)
    if mode == "loop":
        return find_bugs_loop(audit_codebase, context, rounds=rounds)
    if mode == "specialists":
        by_lane = run_specialists_parallel(audit_codebase, context, rounds=rounds)
        return judge_findings(by_lane, audit_codebase)
    raise CIError(f"Unknown mode: {mode}")


# Which env var carries the model default for each provider kind (see
# lucid.llm.default_model).
_MODEL_ENV = {"api": "OPENAI_MODEL", "kiro": "LUCID_KIRO_MODEL", "codex": "LUCID_CODEX_MODEL"}


def _apply_provider_model_env(provider: str | None, model: str | None) -> None:
    """Translate --provider/--model into the environment the pipeline reads.

    The reused audit functions take no provider/model arguments; they resolve
    both from the environment via lucid.llm. Setting these here keeps selection
    working without touching those functions.
    """
    if provider is not None:
        os.environ["LUCID_PROVIDER"] = provider
    if model is not None:
        kind = llm.provider_kind(provider)
        os.environ[_MODEL_ENV[kind]] = model


def gate_severity(findings: list[Finding], fail_on: str) -> bool:
    """Return True if any finding is at or above the fail-on threshold."""
    if fail_on == "none":
        return False
    limit = _SEVERITY_RANK[fail_on]
    return any(_SEVERITY_RANK.get(f.severity, 99) <= limit for f in findings)


def summarize(findings: list[Finding], *, repo_files: list[str], mode: str,
              model: str, provider: str, tripped: bool, fail_on: str) -> str:
    """Render a Markdown summary suitable for a build log or PR comment."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    tally = ", ".join(f"{counts[s]} {s}" for s in ("critical", "high", "medium", "low")
                      if counts.get(s)) or "none"

    verdict = (f"❌ Gate failed — findings at or above `{fail_on}`."
               if tripped else "✅ No findings at or above the gate.")

    lines = [
        "## 🛡️ Lucid security review of PR changes",
        "",
        verdict,
        "",
        f"- Changed Solidity files reviewed: **{len(repo_files)}**",
        f"- Findings: **{len(findings)}** ({tally})",
        f"- Mode: `{mode}` · Provider: `{provider}` · Model: `{model}`",
        "",
    ]
    if findings:
        ordered = sorted(findings, key=lambda f: _SEVERITY_RANK.get(f.severity, 99))
        lines.append("### Findings")
        for i, f in enumerate(ordered, start=1):
            steps = "".join(f"\n     {n}. {s}" for n, s in enumerate(f.exploit_steps, start=1))
            lines.append(
                f"\n**{i}. [{f.severity.upper()}] {f.title}**\n"
                f"- Location: `{f.location}`\n"
                f"- What: {f.description}\n"
                f"- Impact: {f.impact}\n"
                f"- Exploit:{steps}"
            )
    else:
        lines.append("No evidence-backed findings in the changed Solidity.")

    lines += [
        "",
        "---",
        "_Model findings require human verification. An empty report is not a "
        "proof of security, and only the changed files (not their full "
        "dependency closure) were in scope._",
    ]
    return "\n".join(lines)


def _report_dict(findings: list[Finding], *, repo_files: list[str], base: str,
                 head: str, mode: str, model: str, provider: str,
                 fail_on: str, tripped: bool) -> dict:
    return {
        "schema": "lucid-pr-audit/1",
        "base": base,
        "head": head,
        "mode": mode,
        "provider": provider,
        "model": model,
        "fail_on": fail_on,
        "gate_tripped": tripped,
        "changed_files": repo_files,
        "finding_count": len(findings),
        "findings": [f.model_dump() for f in findings],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lucid.ci",
        description="Audit the Solidity changed in a pull request and gate the build.",
    )
    parser.add_argument("--repo", default=".", help="Path to the contracts repo working directory (default: cwd).")
    parser.add_argument("--base", default=os.environ.get("LUCID_PR_BASE"),
                        help="Target-branch ref to diff against, e.g. origin/main. "
                             "Defaults to $LUCID_PR_BASE.")
    parser.add_argument("--head", default=os.environ.get("LUCID_PR_HEAD", "HEAD"),
                        help="PR head ref (default: HEAD or $LUCID_PR_HEAD).")
    parser.add_argument("--subdir", default=os.environ.get("LUCID_PR_SUBDIR"),
                        help="Only audit changed files under this repo-relative directory.")
    parser.add_argument("--mode", default="context",
                        choices=("single", "context", "loop", "specialists"),
                        help="Audit depth (default: context).")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Loop rounds, or per-lane rounds for specialists (default: 3).")
    parser.add_argument("--context-scope", default="repo", choices=("changed", "repo"),
                        help="Build protocol context from just the changed files or the whole "
                             "src tree (default: repo, for cross-contract understanding).")
    parser.add_argument("--src", default=os.environ.get("LUCID_PR_SRC"),
                        help="Repo-relative source dir to load for repo-scope context "
                             "(default: the repo root).")
    parser.add_argument("--provider", default=None, choices=("auto", "api", "kiro", "codex"),
                        help="Force a provider (default: auto / $LUCID_PROVIDER).")
    parser.add_argument("--model", default=None, help="Model ID (default: provider default).")
    parser.add_argument("--fail-on", default="high", choices=_FAIL_ON_CHOICES,
                        help="Fail the build on findings at or above this severity (default: high).")
    parser.add_argument("--report", default=None, help="Write the JSON report to this path.")
    parser.add_argument("--summary", default=None, help="Write the Markdown summary to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists() and not (repo.parent / ".git").exists():
        # Not fatal (worktrees, submodules), but warn — git may still work.
        print(f"[warning] {repo} does not look like a git working tree", file=sys.stderr)
    if not args.base:
        print("error: --base (or $LUCID_PR_BASE) is required, e.g. origin/main", file=sys.stderr)
        return _EXIT_ERROR

    # Make provider/model selection visible to the env-driven pipeline functions.
    _apply_provider_model_env(args.provider, args.model)

    try:
        files = changed_sol_files(repo, args.base, args.head, subdir=args.subdir)
    except CIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    if not files:
        print("No Solidity (.sol) changes in this PR — nothing to audit.")
        if args.report:
            Path(args.report).write_text(json.dumps(_report_dict(
                [], repo_files=[], base=args.base, head=args.head, mode=args.mode,
                model=args.model or "(default)", provider=args.provider or "auto",
                fail_on=args.fail_on, tripped=False), indent=2), encoding="utf-8")
        return _EXIT_CLEAN

    print(f"Changed Solidity files ({len(files)}):")
    for f in files:
        print(f"  - {f}")

    try:
        audit_codebase = annotate_files(repo, files)
        if not audit_codebase.strip():
            print("Changed files are no longer present on disk — nothing to audit.")
            return _EXIT_CLEAN

        # Protocol-context source: the whole src tree (better cross-contract
        # reasoning) or just the changed files (cheaper). Falls back to the
        # changed files if the repo scope is too big or empty.
        context_source = audit_codebase
        if args.mode != "single" and args.context_scope == "repo":
            src_root = repo / args.src if args.src else repo
            try:
                context_source = load_codebase(str(src_root))
            except (FileNotFoundError, ValueError) as exc:
                print(f"[warning] repo-scope context unavailable ({exc}); "
                      "using changed files for context.", file=sys.stderr)
                context_source = audit_codebase

        diff = unified_diff(repo, args.base, args.head, files)

        print(f"\nRunning {args.mode} audit "
              f"(provider={args.provider or 'auto'}, model={args.model or 'default'})...")
        findings = run_audit(
            audit_codebase, mode=args.mode, rounds=args.rounds,
            context_source=context_source, diff=diff,
        )
    except CIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - surface a clean operational failure, not a traceback.
        print(f"error: audit failed: {type(exc).__name__}", file=sys.stderr)
        return _EXIT_ERROR

    tripped = gate_severity(findings, args.fail_on)
    resolved_model = args.model or llm.default_model(args.provider)
    resolved_provider = llm.provider_kind(args.provider)

    summary = summarize(
        findings, repo_files=files, mode=args.mode, model=resolved_model,
        provider=resolved_provider, tripped=tripped, fail_on=args.fail_on,
    )
    print("\n" + summary)

    if args.summary:
        Path(args.summary).write_text(summary, encoding="utf-8")
    if args.report:
        report = _report_dict(
            findings, repo_files=files, base=args.base, head=args.head,
            mode=args.mode, model=resolved_model, provider=resolved_provider,
            fail_on=args.fail_on, tripped=tripped,
        )
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote JSON report to {args.report}")

    return _EXIT_GATE_TRIPPED if tripped else _EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
