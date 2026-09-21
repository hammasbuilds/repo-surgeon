"""Find every function in a repo that a rule matches. No model involved.

Deterministic and exhaustive on purpose. Asking a model to find the sites would make the
tool's coverage a probability rather than a fact, and "did it find all of them" would
become a second thing needing verification. `ast` already answers it exactly.

Three things here are less obvious than the walk itself, and every one of them exists
because the differential rung has to be able to **call** what the scout hands it. A site
the scout reports but nothing can execute is not a candidate, it is a false positive that
shows up later as an unexplained refusal.

**Functions are attributed to their innermost enclosing definition.** A site inside a
nested helper belongs to the helper, not to the outer function, because the helper is the
smallest thing that can be replaced and still be called.

**Methods are skipped.** A method needs an instance, and building one means running the
code under test. That is a real limitation rather than an oversight, and it is stated in
the README rather than hidden behind a low landing rate.

**Each function carries a pruned header** - only the imports and module-level constants it
actually reads. The whole module would drag in import-time side effects; the whole import
list would make a function that touches nothing but `os.path` unverifiable because some
unrelated third-party import at the top of its file is not installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

from repo_surgeon.rules import Rule
from repo_surgeon.types import Hunk, Site

# Never source code, wherever they appear in a path.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".eggs",
    ".ruff_cache",
    "node_modules",
    "site-packages",
}

# Conventional build-artifact directories - and also perfectly ordinary package names.
# `pypa/build` ships its own package at `src/build/`, so treating the name as a blanket
# skip makes the tool report "0 sites" on that repo and call it a success.
ARTIFACT_DIRS = {"build", "dist"}


def python_files(root: Path, include_tests: bool = False) -> list[Path]:
    """Every Python file worth rewriting, under `root`.

    Two traps here, and both fail the same way - silently finding nothing and reporting
    it as success, which is the worst possible failure mode for a tool like this.

    Skipping is judged on the path **relative to root**, never the absolute one, or a
    checkout living under any directory whose name is on the list disappears entirely.

    And `build`/`dist` are only treated as artifacts when they sit at the repo root and
    are not packages. They are ordinary package names too: `pypa/build` ships its own
    source at `src/build/`, and a blanket name match scouts that repo to zero sites.
    """
    artifacts = {
        d
        for d in ARTIFACT_DIRS
        # A real artifact directory sits at the repo root and is not a package. The same
        # name one level down, or carrying an __init__.py, is somebody's source.
        if (root / d).is_dir() and not (root / d / "__init__.py").is_file()
    }
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in artifacts:
            continue
        if not include_tests and ("test" in p.name or "tests" in rel.parts):
            continue
        out.append(p)
    return out


def _bound_names(node: ast.stmt) -> set[str]:
    """The names a header statement makes available."""
    out: set[str] = set()
    if isinstance(node, ast.Import | ast.ImportFrom):
        for alias in node.names:
            out.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    return out


def free_names(source: str) -> set[str]:
    """Every name a chunk of code reads without defining. Over-approximate on purpose."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    } | {
        n.value.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }


def module_header(tree: ast.Module, source: str, needed: set[str] | None = None) -> str:
    """Imports and simple module-level constants - enough to run one function alone.

    Deliberately not the whole module: importing a module *runs* it, and a module that
    opens a file or reads an env var at import time would fail the differential rung for
    reasons that have nothing to do with the change being verified.

    And deliberately not every import either. `pass `needed` to keep only the statements
    binding names the function actually reads. Carrying the rest means a function that
    touches nothing but `os.path` still fails to load because some unrelated third-party
    import at the top of its module is not installed - which gets reported as "could not
    verify this change" when the truth is "could not be bothered to prune an import".
    """
    lines = source.splitlines()
    kept: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Import | ast.ImportFrom | ast.Assign):
            continue
        if isinstance(node, ast.Assign) and not isinstance(
            node.value, ast.Constant | ast.Tuple | ast.List | ast.Dict | ast.Set
        ):
            # Calls are excluded: evaluating one is exactly the side effect being avoided.
            continue
        if needed is not None and not (_bound_names(node) & needed):
            continue
        kept.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    return "\n".join(kept)


def _enclosing(
    tree: ast.Module,
) -> tuple[dict[int, ast.FunctionDef | ast.AsyncFunctionDef], set[int]]:
    """node id -> innermost enclosing function, plus the ids of functions inside a class.

    Methods are tracked separately because they are out of scope, and the reason is not
    squeamishness: a method cannot be called without an instance, and this tool's entire
    claim rests on calling a function and comparing what comes back. There is no honest
    way to verify a rewrite of `__init__` without constructing the object, and
    constructing it means running the code under test.

    Treating them as ordinary functions does not merely fail, it fails *confusingly*. A
    method extracted by line range is indented, so it will not parse alone, and the
    verdict comes out as "the proposal does not define `__init__`" - blaming the model
    for the scout's mistake.
    """
    owner: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    methods: set[int] = set()

    def walk(node: ast.AST, current, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            is_fn = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            nxt = child if is_fn else current
            if is_fn and in_class:
                methods.add(id(child))
            if current is not None or is_fn:
                owner[id(child)] = nxt
            # A nested function inside a method is callable on its own, so entering one
            # clears the class context.
            walk(child, nxt, isinstance(child, ast.ClassDef) or (in_class and not is_fn))

    walk(tree, None, False)
    return owner, methods


def package_context(path: Path) -> tuple[str, str]:
    """(directory to put on sys.path, dotted package name) for a module.

    Walk up while each directory is a package. The first one that is not is what belongs
    on `sys.path`, and everything below it is the dotted name. `src/build/__main__.py`
    gives `("<abs>/src", "build")`, which is exactly what makes `import build` and
    `from . import x` resolve the way they do when the real module is imported.
    """
    parts: list[str] = []
    current = path.parent
    while (current / "__init__.py").is_file():
        parts.append(current.name)
        if current.parent == current:
            break
        current = current.parent
    return str(current.resolve()), ".".join(reversed(parts))


def scout_file(path: Path, rules: list[Rule], root: Path) -> list[Hunk]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    owner, methods = _enclosing(tree)
    lines = source.splitlines()
    rel = path.relative_to(root)

    by_func: dict[int, list[Site]] = {}
    funcs: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    for node in ast.walk(tree):
        for rule in rules:
            snippet = rule.match(node)
            if snippet is None:
                continue
            fn = owner.get(id(node))
            if fn is None:
                continue  # module-level site: nothing to call, so nothing to verify
            if id(fn) in methods:
                continue  # a method needs an instance to call; see _enclosing
            funcs[id(fn)] = fn
            by_func.setdefault(id(fn), []).append(
                Site(
                    path=rel,
                    lineno=getattr(node, "lineno", fn.lineno),
                    rule=rule.name,
                    snippet=snippet,
                )
            )

    sys_path, package = package_context(path)

    hunks = []
    for fid, sites in by_func.items():
        fn = funcs[fid]
        # Include decorators, or the rewrite silently drops them.
        start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
        body = "\n".join(lines[start - 1 : fn.end_lineno])
        hunks.append(
            Hunk(
                path=rel,
                func=fn.name,
                lineno=start,
                end_lineno=fn.end_lineno or fn.lineno,
                original=body,
                sites=sorted(sites, key=lambda s: s.lineno),
                # Pruned per function: only the imports and constants this one reads.
                module_header=module_header(tree, source, free_names(body)),
                sys_path=sys_path,
                package=package,
            )
        )
    return sorted(hunks, key=lambda h: (str(h.path), h.lineno))


def scout(root: Path, rules: list[Rule], include_tests: bool = False) -> list[Hunk]:
    out: list[Hunk] = []
    for path in python_files(root, include_tests):
        out.extend(scout_file(path, rules, root))
    return out
