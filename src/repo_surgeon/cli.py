"""Command line entry point.

    repo-surgeon scout  <repo> --rule ospath
    repo-surgeon run    <repo> --rule ospath --limit 40
    repo-surgeon report <repo>

`scout` is separate and free - it runs no model and touches nothing, so it is the right
way to find out whether a repo is worth pointing the tool at before spending an hour of
GPU on it.

`run` edits the working tree in place and expects the repo to be a clean git checkout, so
that `git diff` is the change and `git checkout .` is the undo. It refuses to start on a
dirty tree rather than mixing its edits into someone else's.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from repo_surgeon import report
from repo_surgeon.model import available
from repo_surgeon.pipeline import run as run_pipeline
from repo_surgeon.rules import REGISTRY, get
from repo_surgeon.scout import scout


def _dirty(repo: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.stdout.strip())


def cmd_scout(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    hunks = scout(repo, get(args.rule), args.include_tests)
    by_rule: dict[str, int] = {}
    for h in hunks:
        for s in h.sites:
            by_rule[s.rule] = by_rule.get(s.rule, 0) + 1

    print(f"{repo}")
    print(
        f"  {sum(by_rule.values())} sites in {len(hunks)} functions across "
        f"{len({h.path for h in hunks})} files\n"
    )
    for rule, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {rule:18} {count:5}  {REGISTRY[rule].summary}")

    if args.verbose:
        print()
        for h in hunks[:40]:
            print(
                f"  {h.path}:{h.lineno} {h.func}()  "
                f"{len(h.sites)} site(s): {', '.join(sorted(h.rules))}"
            )
        if len(hunks) > 40:
            print(f"  ... and {len(hunks) - 40} more")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"not a directory: {repo}")
        return 1
    if not args.model_optional and not available(args.model):
        print(f"{args.model} is not available from ollama")
        return 1
    if _dirty(repo) and not args.allow_dirty:
        print(
            f"{repo} has uncommitted changes.\n"
            "This edits the working tree in place, so it wants a clean checkout - that is\n"
            "what makes `git diff` the change and `git checkout .` the undo.\n"
            "Pass --allow-dirty to override."
        )
        return 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_pipeline(
        repo=repo,
        rules=get(args.rule),
        ledger_path=out_dir / "ledger.jsonl",
        model=args.model,
        workers=args.workers,
        limit=args.limit,
        include_tests=args.include_tests,
        run_suite_rung=not args.no_suite,
        allow_inconclusive=args.allow_inconclusive,
    )

    print()
    print(report.summary(result))
    report.write_markdown(result, out_dir / "REPORT.md", repo.name)
    report.write_json(result, out_dir / "result.json")
    print(f"\nwrote {out_dir / 'REPORT.md'} and {out_dir / 'result.json'}")
    if result.applied:
        print(f"applied {len(result.applied)} change(s) to {repo} - `git diff` to review")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repo-surgeon", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("repo")
        p.add_argument(
            "--rule",
            action="append",
            choices=sorted(REGISTRY),
            help="repeatable; default is every rule",
        )
        p.add_argument(
            "--include-tests", action="store_true", help="also rewrite the repo's test files"
        )

    s = sub.add_parser("scout", help="find sites; no model, changes nothing")
    common(s)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_scout)

    r = sub.add_parser("run", help="propose, verify and apply")
    common(r)
    r.add_argument("--model", default="qwen2.5-coder:14b")
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--limit", type=int, help="stop after N functions")
    r.add_argument("--out", default="surgeon-out")
    r.add_argument("--no-suite", action="store_true", help="skip rung 5")
    r.add_argument("--allow-dirty", action="store_true")
    r.add_argument(
        "--allow-inconclusive",
        action="store_true",
        help="land hunks the differential rung could not reach a verdict on",
    )
    r.add_argument("--model-optional", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
