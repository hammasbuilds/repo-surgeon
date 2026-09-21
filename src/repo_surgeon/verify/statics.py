"""The cheap rungs: does it parse, and is it still the same function?

Both run in microseconds and both catch things that would otherwise waste a subprocess or,
worse, land. They come first because the ladder is ordered by cost.

The signature check is the one that is easy to leave out. A model asked to rewrite
`def copy(src, dst)` will sometimes return `def copy(src, dst, *, follow_symlinks=True)`,
which is a perfectly good function and a different one. Every caller in the repo still
works, the test suite still passes, and the public API of the package has silently
changed. Nothing downstream would notice, so it is checked here.
"""

from __future__ import annotations

import ast


def parses(source: str) -> tuple[bool, str]:
    try:
        ast.parse(source)
        return True, ""
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"


def _ann(p: ast.arg) -> str:
    return ast.unparse(p.annotation) if p.annotation else ""


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple:
    a = fn.args
    return (
        fn.name,
        tuple(p.arg for p in a.posonlyargs),
        tuple(p.arg for p in a.args),
        a.vararg.arg if a.vararg else None,
        tuple(p.arg for p in a.kwonlyargs),
        a.kwarg.arg if a.kwarg else None,
        len(a.defaults),
        len(a.kw_defaults),
        # Annotations are part of the interface even though they do not change runtime
        # behaviour. A rewrite that turns `archive: StrPath` into `archive: Union[str,
        # Path]` passes every executable check and has still altered what the package
        # promises its callers - type checkers downstream will disagree about it, and no
        # test suite anywhere will notice.
        tuple(_ann(p) for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)),
        ast.unparse(fn.returns) if fn.returns else "",
    )


def _first_function(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def signature_kept(original: str, proposed: str, name: str) -> tuple[bool, str]:
    """The proposal must define the same function, callable the same way."""
    old = _first_function(original, name)
    new = _first_function(proposed, name)
    if old is None:
        return False, f"could not find `{name}` in the original"
    if new is None:
        return False, f"the proposal does not define `{name}`"

    a, b = _signature(old), _signature(new)
    if a == b:
        return True, ""

    labels = (
        "name",
        "positional-only",
        "parameters",
        "*args",
        "keyword-only",
        "**kwargs",
        "positional defaults",
        "keyword defaults",
        "parameter annotations",
        "return annotation",
    )
    changed = [f"{labels[i]}: {a[i]!r} -> {b[i]!r}" for i in range(len(a)) if a[i] != b[i]]
    return False, "; ".join(changed)


def decorators_kept(original: str, proposed: str, name: str) -> tuple[bool, str]:
    """Dropping `@property` or `@lru_cache` changes behaviour without changing the body."""
    old = _first_function(original, name)
    new = _first_function(proposed, name)
    if old is None or new is None:
        return True, ""  # the signature rung already reported this
    a = [ast.unparse(d) for d in old.decorator_list]
    b = [ast.unparse(d) for d in new.decorator_list]
    if a == b:
        return True, ""
    return False, f"decorators changed: {a} -> {b}"
