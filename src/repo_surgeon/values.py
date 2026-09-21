"""Inputs to call a function with, so the old and new versions can be compared.

This is the part that decides whether the tool finds anything. A differential check is
only as good as the values it tries, and the obvious approaches both fail:

- **Random values** almost never satisfy a real function's preconditions. A function
  expecting a path gets `-4471` and raises on both sides, the two sides "agree", and the
  hunk lands unverified. Agreement on garbage is not evidence.
- **Type-driven only** gives tidy values - `"string"`, `1`, `[]` - and tidy values are
  exactly the ones where `os.path` and `pathlib` agree. The disagreements live in the
  corners: a trailing slash, a leading dot, an absolute second argument.

So values come from three places, in this order:

1. **Harvested from the repo's own tests.** Every string, number and literal in the test
   files, indexed by the parameter name it was passed to where that is recoverable. These
   are real values from the real domain - a repo that tests `join(base, "../etc/passwd")`
   hands that straight over.
2. **Type-driven**, from the annotation when there is one.
3. **A corner pool chosen per parameter name**, which is where the known-divergent cases
   live: `""`, `"a/b/"`, `".bashrc"`, `"/abs"`, `None`, `0`, `-1`, `()`.

The corner pool is hand-written rather than generated, because each entry is there for a
specific reason that a generator has no way to know.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Values chosen because a known migration disagrees on them. Each line is a real corner.
PATH_CORNERS = [
    '""',
    '"."',
    '"a/b"',
    '"a/b/"',  # basename disagrees: "" vs "b"
    '"/abs/path"',
    '"a/../b"',  # abspath normalises, resolve() also hits the filesystem
    '".bashrc"',  # splitext vs .suffix/.stem disagree
    '"archive.tar.gz"',  # splitext takes the last ext only; .suffixes takes all
    '"~"',  # expanduser
    '"with space/x"',
    '"//double//slash"',
]

STRING_CORNERS = ['""', '"x"', '"a b"', '"%s"', '"{}"', '"\\n"', '"0"', '"None"']
NUMBER_CORNERS = ["0", "1", "-1", "2", "10", "0.0", "1.5", "-0.5"]
GENERIC_CORNERS = ["None", "True", "False", "[]", "()", "{}", "[1, 2]", '("a", "b")']


def _looks_like_path(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("path", "file", "dir", "src", "dst", "dest", "root", "loc"))


def _looks_like_number(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("count", "size", "num", "n", "i", "idx", "index", "len"))


def harvest(test_root: Path, limit_per_name: int = 12) -> dict[str, list[str]]:
    """Literal values from a repo's tests, indexed by the keyword they were passed as.

    Keyword arguments are the recoverable case: `join(base="x")` tells us `base` is a
    string that this repo really uses. Positional arguments are collected under `"*"` as
    an untyped pool, since their parameter name is not knowable without resolving the
    call target.
    """
    found: dict[str, list[str]] = {}

    def add(key: str, value: str) -> None:
        bucket = found.setdefault(key, [])
        if value not in bucket and len(bucket) < limit_per_name:
            bucket.append(value)

    files = [p for p in test_root.rglob("*.py") if "test" in p.name or "tests" in p.parts]
    for path in files[:400]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg and isinstance(kw.value, ast.Constant):
                    add(kw.arg, repr(kw.value.value))
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str | int | float):
                    add("*", repr(arg.value))
    return found


def for_parameter(name: str, annotation: str | None, harvested: dict[str, list[str]]) -> list[str]:
    """Candidate source-literals for one parameter, best guesses first."""
    out: list[str] = []
    out.extend(harvested.get(name, []))

    ann = (annotation or "").lower()
    if "path" in ann or "str" in ann or _looks_like_path(name):
        out.extend(PATH_CORNERS if (_looks_like_path(name) or "path" in ann) else STRING_CORNERS)
    if "int" in ann or "float" in ann or _looks_like_number(name):
        out.extend(NUMBER_CORNERS)
    if "bool" in ann:
        out.extend(["True", "False"])

    if not out or len(out) < 4:
        # Nothing specific is known, so try the untyped pool from the repo's own tests
        # before falling back to generic corners.
        out.extend(harvested.get("*", [])[:8])
        out.extend(STRING_CORNERS)
        out.extend(GENERIC_CORNERS)

    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def argument_sets(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    harvested: dict[str, list[str]],
    cap: int = 40,
) -> list[str]:
    """Source strings for calling `fn`, e.g. `('a/b/', '/abs')`.

    Rather than a full cross product, which explodes, this varies **one parameter at a
    time** around a baseline. A disagreement almost always comes from one argument's
    corner case, and one-at-a-time makes the witness immediately readable: exactly one
    value differs from the baseline, so that value is the cause.
    """
    params = [a for a in fn.args.args if a.arg not in ("self", "cls")]
    if not params:
        return ["()"]

    pools = []
    for p in params:
        ann = ast.unparse(p.annotation) if p.annotation else None
        pools.append(for_parameter(p.arg, ann, harvested))

    baseline = [pool[0] if pool else "None" for pool in pools]
    sets: list[str] = ["(" + ", ".join(baseline) + ",)"]

    for i, pool in enumerate(pools):
        for value in pool[1:]:
            args = list(baseline)
            args[i] = value
            sets.append("(" + ", ".join(args) + ",)")
            if len(sets) >= cap:
                return sets
    return sets
