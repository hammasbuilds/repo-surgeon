"""Did the change weaken the tests that cover it?

The last rung, and the one that catches a class of change nothing else does. A rewrite can
be behaviourally identical on every input tried *and* leave the code less testable than it
found it - collapsing two branches into one, swallowing an exception, replacing an
explicit comparison with a truthiness check. The suite stays green because the suite never
tested the branch that disappeared.

Mutation testing measures it directly, and here the oracle is the **function's own
inputs**, not the repo's test suite. Break the function in one place and check whether any
of the argument sets from the differential rung can tell the mutant apart. Do that for the
original and for the proposal. A drop means the proposal made the function harder to
distinguish from a broken version of itself - it lost a branch, swallowed an error, or
turned an explicit comparison into a truthiness test.

Using the function's inputs rather than the suite keeps this rung at function scope, and
makes it work on a repo whose suite does not cover the function at all - where a
suite-based score would be a flat zero for both versions and say nothing.

This is a *comparison*, not an absolute score. A function whose mutants all survive in
both versions is hard to test either way; that is a fact about the function, not about the
proposal, and it is reported rather than held against the change.
"""

from __future__ import annotations

import ast


class _Mutator(ast.NodeTransformer):
    """One single-point change per instance, chosen by index."""

    SWAPS = {
        ast.Lt: ast.LtE,
        ast.LtE: ast.Lt,
        ast.Gt: ast.GtE,
        ast.GtE: ast.Gt,
        ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq,
        ast.Add: ast.Sub,
        ast.Sub: ast.Add,
        ast.Mult: ast.FloorDiv,
        ast.And: ast.Or,
        ast.Or: ast.And,
    }

    def __init__(self, target: int) -> None:
        self.target = target
        self.seen = 0
        self.applied = False

    def _take(self) -> bool:
        hit = self.seen == self.target
        self.seen += 1
        return hit

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if node.ops and type(node.ops[0]) in self.SWAPS and self._take():
            node.ops[0] = self.SWAPS[type(node.ops[0])]()
            self.applied = True
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in self.SWAPS and self._take():
            node.op = self.SWAPS[type(node.op)]()
            self.applied = True
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in self.SWAPS and self._take():
            node.op = self.SWAPS[type(node.op)]()
            self.applied = True
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, int) and not isinstance(node.value, bool) and self._take():
            self.applied = True
            return ast.Constant(value=node.value + 1)
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if self._take():
            self.applied = True
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return node


def mutants(source: str, cap: int = 12) -> list[str]:
    """Single-point mutants of one function's source."""
    try:
        base = ast.parse(source)
    except SyntaxError:
        return []

    out: list[str] = []
    for i in range(cap * 3):
        m = _Mutator(i)
        try:
            tree = m.visit(ast.parse(source))
        except (SyntaxError, RecursionError):
            continue
        if not m.applied:
            if i > m.seen:  # ran out of sites
                break
            continue
        ast.fix_missing_locations(tree)
        try:
            text = ast.unparse(tree)
        except (AttributeError, ValueError):
            continue
        if text != ast.unparse(base) and text not in out:
            out.append(text)
        if len(out) >= cap:
            break
    return out


def kill_rate(
    header: str,
    source: str,
    func: str,
    argsets: list[str],
    timeout: float = 30.0,
    sys_path: str = "",
    package: str = "",
) -> dict:
    """What fraction of this function's mutants the given inputs can tell apart.

    The oracle is the *original function itself*, not the repo's test suite: a mutant is
    killed if it disagrees with the unmutated version on some input. That keeps this rung
    at function scope and makes it usable on a repo whose suite does not cover the
    function at all - where a suite-based score would be a flat zero and say nothing.
    """
    ms = mutants(source)
    if not ms or not argsets:
        return {"mutants": 0, "killed": 0, "rate": None, "detail": "no mutable sites"}

    from repo_surgeon.verify.differential import run_differential

    killed = 0
    scored = 0
    for mutant in ms:
        res = run_differential(header, source, mutant, func, argsets, timeout, sys_path, package)
        if res["status"] == "differs":
            killed += 1
            scored += 1
        elif res["status"] == "agree":
            scored += 1
        # inconclusive / error mutants are not scored either way

    if not scored:
        return {
            "mutants": len(ms),
            "killed": 0,
            "rate": None,
            "detail": "no mutant could be scored",
        }
    return {
        "mutants": len(ms),
        "scored": scored,
        "killed": killed,
        "rate": killed / scored,
        "detail": f"{killed}/{scored} mutants distinguished",
    }
