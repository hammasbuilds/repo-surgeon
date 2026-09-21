"""Scout -> propose -> verify -> apply. The whole run.

Flow, and where the model is and is not involved:

    scout      ast              no model    exhaustive, deterministic
    propose    14B              model       one call per hunk, one attempt
    verify     subprocess       no model    rungs 1-4, per hunk, in parallel
    apply      text edit        no model    only what survived
    suite      pytest           no model    rung 5, once, over everything applied

The model appears exactly once, in the middle, and nothing it says is taken as evidence.
That split is the point of the tool: `code-llm-lab` project 08 measured LLM code review at
44.8% precision - it flagged MBPP's own human-written reference solutions 38.8% of the time
- and project 09 found stated confidence does not separate correct answers from wrong ones.
So neither is in the loop here. Only execution decides.

Verification is parallel because each hunk's differential run is an independent subprocess.
Applying is sequential and bottom-up within a file, so that editing one function does not
move the line numbers of another that has not been applied yet.
"""

from __future__ import annotations

import ast
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from repo_surgeon import propose as proposer
from repo_surgeon.ledger import Ledger
from repo_surgeon.rules import Rule
from repo_surgeon.scout import free_names, scout
from repo_surgeon.types import Hunk, Verdict
from repo_surgeon.values import argument_sets, harvest
from repo_surgeon.verify import verify_hunk
from repo_surgeon.verify.suite import newly_failing, run_suite


@dataclass
class RunResult:
    hunks: list[Hunk] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    applied: list[Hunk] = field(default_factory=list)
    reverted: list[Hunk] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def landed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.landed]

    @property
    def refused(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.landed]

    def by_rung(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.refused:
            out[v.rung or "?"] = out.get(v.rung or "?", 0) + 1
        return out


def _argsets_for(hunk: Hunk, harvested: dict[str, list[str]]) -> list[str]:
    try:
        tree = ast.parse(hunk.original)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == hunk.func:
            return argument_sets(node, harvested)
    return []


def split_imports(source: str) -> tuple[list[str], str]:
    """Separate a proposal's leading imports from the function itself.

    The model is asked for "the function plus any imports it needs", and it obliges - but
    splicing that whole block in at the function's line number drops `from pathlib import
    Path` into the middle of the module, once per rewritten function. That is valid Python
    and it is not something anyone would merge.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], source

    lines = source.splitlines()
    imports, keep = [], []
    for node in tree.body:
        block = lines[node.lineno - 1 : node.end_lineno]
        if isinstance(node, ast.Import | ast.ImportFrom):
            imports.extend(block)
        else:
            keep.extend(block)
    return imports, "\n".join(keep)


def _bound_names_of(import_line: str) -> set[str]:
    """The names one import statement makes available."""
    try:
        tree = ast.parse(import_line.strip())
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
    return out


def _import_anchor(tree: ast.Module) -> int:
    """The line to insert new imports before: after the docstring and existing imports."""
    line = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            line = node.end_lineno or line  # module docstring
        elif isinstance(node, ast.Import | ast.ImportFrom):
            line = node.end_lineno or line
        else:
            break
    return line


def apply_hunks(repo: Path, hunks: list[Hunk]) -> list[Hunk]:
    """Write the surviving proposals into the working tree.

    Function bodies are replaced bottom-up within each file: a replacement changes how
    many lines the file has, so a top-down pass would invalidate every line number
    recorded below the first edit.

    Imports the proposals need are hoisted to the top of the file instead, de-duplicated
    against what is already there, and inserted last so they do not shift the line numbers
    the body edits depend on.
    """
    by_file: dict[Path, list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault(h.path, []).append(h)

    done: list[Hunk] = []
    for rel, group in by_file.items():
        path = repo / rel
        try:
            original = path.read_text(encoding="utf-8")
            lines = original.splitlines()
            anchor = _import_anchor(ast.parse(original))
        except (OSError, SyntaxError):
            continue

        existing = {ln.strip() for ln in lines}
        wanted: list[str] = []

        for h in sorted(group, key=lambda x: x.lineno, reverse=True):
            imports, body = split_imports(h.proposed or "")
            used = free_names(body)
            for imp in imports:
                # Asked for "any imports it needs", a model tends to list a few it does
                # not - `Union` and `Sequence` came back on a rewrite that used neither.
                # Landing those is import noise in someone else's file.
                bound = _bound_names_of(imp)
                if bound and not (bound & used):
                    continue
                if imp.strip() not in existing and imp.strip() not in wanted:
                    wanted.append(imp.strip())
            lines[h.lineno - 1 : h.end_lineno] = body.splitlines()
            done.append(h)

        if wanted:
            lines[anchor:anchor] = wanted
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return done


def run(
    repo: Path,
    rules: list[Rule],
    ledger_path: Path,
    model: str = "qwen2.5-coder:14b",
    workers: int = 8,
    limit: int | None = None,
    include_tests: bool = False,
    run_suite_rung: bool = True,
    allow_inconclusive: bool = False,
) -> RunResult:
    t_start = time.time()
    result = RunResult()
    ledger = Ledger(ledger_path)
    already = ledger.seen()

    print(f"scouting {repo} for {', '.join(r.name for r in rules)}...")
    hunks = scout(repo, rules, include_tests)
    n_sites = sum(len(h.sites) for h in hunks)
    print(
        f"  {n_sites} sites in {len(hunks)} functions across {len({h.path for h in hunks})} files"
    )

    todo = [h for h in hunks if h.key not in already]
    if limit:
        todo = todo[:limit]
    if already:
        print(f"  {len(already)} already decided in a previous run, skipping")
    result.hunks = todo
    if not todo:
        print("nothing to do")
        result.seconds = time.time() - t_start
        return result

    # Rung 5's baseline must be taken before anything is touched, or a repo that was
    # already red would make every hunk look like it broke something.
    if run_suite_rung:
        print("running the suite once, untouched, for a baseline...")
        result.baseline = run_suite(repo)
        if result.baseline.get("usable"):
            print(
                f"  baseline: {result.baseline.get('counts')} "
                f"({len(result.baseline['failed'])} already failing)"
            )
        else:
            print(
                f"  suite unusable here ({result.baseline.get('detail', '')[:80]}); "
                "rung 5 will be skipped"
            )

    print(f"harvesting argument values from {repo.name}'s own tests...")
    harvested = harvest(repo)
    print(
        f"  {sum(len(v) for v in harvested.values())} literals under "
        f"{len(harvested)} parameter names"
    )

    print(f"proposing {len(todo)} rewrites with {model}...")
    proposer.propose(todo, model=model, workers=workers)

    print("verifying...")
    argsets = {h.key: _argsets_for(h, harvested) for h in todo}

    def one(h: Hunk) -> Verdict:
        return verify_hunk(h, argsets.get(h.key, []), allow_inconclusive)

    with ThreadPoolExecutor(max_workers=max(2, workers // 2)) as pool:
        for verdict in pool.map(one, todo):
            result.verdicts.append(verdict)
            ledger.append(verdict)

    landed_keys = {v.hunk_key for v in result.landed}
    survivors = [h for h in todo if h.key in landed_keys]
    print(f"  {len(survivors)} of {len(todo)} cleared rungs 1-4")

    if survivors:
        result.applied = apply_hunks(repo, survivors)

    # --- rung 5: the repo's own suite, once, over everything applied -------------------
    if run_suite_rung and survivors and result.baseline.get("usable"):
        print("running the suite against the patched tree...")
        result.after = run_suite(repo)
        broke = newly_failing(result.baseline, result.after)
        if broke:
            print(f"  {len(broke)} newly failing test(s) - reverting everything applied")
            # Which hunk caused it is not knowable from one run, so the honest action is
            # to revert the batch and record it, rather than guess at a culprit.
            for h in result.applied:
                path = repo / h.path
                text = path.read_text(encoding="utf-8")
                path.write_text(
                    text.replace(h.proposed or "", h.original, 1), encoding="utf-8", newline=""
                )
            result.reverted = result.applied
            result.applied = []
            for v in result.verdicts:
                if v.landed:
                    v.landed = False
                    v.rung = "suite"
                    v.detail = f"batch reverted: {len(broke)} newly failing test(s)"
        else:
            print(f"  suite still green: {result.after.get('counts')}")

    result.seconds = time.time() - t_start
    return result
