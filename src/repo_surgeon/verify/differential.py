"""Run the old function and the new one on the same inputs and compare.

This is the rung the whole tool exists for. The earlier rungs (parses, signature kept,
suite still green) only establish that nothing obvious broke; a suite that formats tidy
values stays green through a change that breaks on a trailing slash.

Four things make the comparison trustworthy:

**Each version gets its own module namespace.** The naive approach - define both in one
namespace and grab a reference to each - silently breaks recursion: the body's self-call
resolves through the module globals, so after the second `def` shadows the name, the *old*
function recurses into the *new* one and the two appear to agree. Separate namespaces also
let each version carry its own helpers and imports.

**Both run in one process.** No cross-process nondeterminism to explain away.

**An exception is a result.** A function that raised `KeyError` and now returns `None` has
changed behaviour; comparing only return values would call that agreement. The exception
type and message are compared too.

**Agreement only counts when the call did something.** If every input makes both sides
raise `TypeError`, the arguments were wrong for this function and the run proves nothing.
That is `inconclusive`, not a pass - treating it as a pass is exactly how an unverified
change lands.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = '''
import json, sys, types

SYS_PATH = {sys_path!r}
PACKAGE = {package!r}
HEADER = {header!r}
OLD = {old!r}
NEW = {new!r}
NAME = {name!r}

# The function's own package has to be importable, or a module that does
# `from . import x` or `import mypackage.y` cannot be loaded at all - and the refusal
# would read "uncallable" when the truth is that nothing put the package on the path.
if SYS_PATH:
    sys.path.insert(0, SYS_PATH)

# Annotations are not evaluated. Real modules routinely annotate with names that only
# exist under `if TYPE_CHECKING:`, and evaluating them raises NameError on a function
# that is otherwise perfectly callable.
PREAMBLE = "from __future__ import annotations\\n"


def _load(tag, source):
    """Execute one version in its own module namespace.

    Separate namespaces are what make a recursive function comparable: its self-call
    resolves inside its own globals, so the old version recurses into the old version.

    `__package__` is set so that relative imports resolve the way they do when the real
    module is imported rather than raising "no known parent package".
    """
    # Both namespaces carry the SAME __name__ on purpose. They are separate module
    # objects either way, and a class defined inside one reprs as `<__name__.Foo ...>` -
    # so distinct names would make every returned instance look like a difference, which
    # is the very artefact the address normalisation above exists to remove.
    name = (PACKAGE + ".rs_probe") if PACKAGE else "rs_probe"
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    mod.__dict__["__package__"] = PACKAGE
    exec(compile(PREAMBLE + HEADER + "\\n\\n" + source, "<" + tag + ">", "exec"), mod.__dict__)
    return mod.__dict__[NAME]


try:
    OLD_FN = _load("old", OLD)
except Exception as exc:
    print("__RS_LOAD_FAIL__old: " + type(exc).__name__ + ": " + str(exc)[:200])
    raise SystemExit(0)

try:
    NEW_FN = _load("new", NEW)
except Exception as exc:
    print("__RS_LOAD_FAIL__new: " + type(exc).__name__ + ": " + str(exc)[:200])
    raise SystemExit(0)


import re as _re

_ADDR = _re.compile(r"0x[0-9a-fA-F]{{4,}}")


def _norm(text):
    """Blank out memory addresses before comparing.

    An object without a custom __repr__ reprs as `<Foo object at 0x7f...>`, and the two
    versions necessarily allocate at different addresses. Comparing those raw makes every
    function that returns such an object a guaranteed disagreement, and the report then
    presents the address difference as proof of a behaviour change - a confident false
    accusation, which is worse for this tool than missing a real one.
    """
    return _ADDR.sub("0x...", text)


def _call(fn, args):
    try:
        return ("ok", _norm(repr(fn(*args))))
    except Exception as exc:
        return ("raise", _norm(type(exc).__name__ + ": " + str(exc)[:200]))


ARGSETS = {argsets}

rows = []
for args in ARGSETS:
    a = _call(OLD_FN, args)
    b = _call(NEW_FN, args)
    rows.append({{"args": repr(args), "old": a, "new": b, "same": a == b}})

print("__RS_JSON__" + json.dumps(rows))
'''


def run_differential(
    header: str,
    original: str,
    proposed: str,
    func: str,
    argsets: list[str],
    timeout: float = 30.0,
    sys_path: str = "",
    package: str = "",
) -> dict:
    """Compare the two versions. Returns a verdict dict; never raises."""
    if not argsets:
        return {"status": "inconclusive", "detail": "no argument sets could be built"}

    script = RUNNER.format(
        sys_path=sys_path,
        package=package,
        header=header,
        old=original,
        new=proposed,
        name=func,
        argsets="[" + ", ".join(argsets) + "]",
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "differential.py"
        # newline="" or Windows rewrites the newlines to CR CR LF, which breaks any line
        # continuation inside the source being compared.
        path.write_text(script, encoding="utf-8", newline="")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "detail": f"no result within {timeout}s"}

    out = proc.stdout or ""

    if "__RS_LOAD_FAIL__" in out:
        which = out.split("__RS_LOAD_FAIL__", 1)[1].strip().splitlines()[0]
        # The old side failing to load means the function cannot be isolated (it needs
        # module state the header did not carry). That is a limit of this tool, not a
        # fault in the proposal, and must not be reported as one.
        side = "old" if which.startswith("old") else "new"
        return {
            "status": "uncallable" if side == "old" else "load_error",
            "detail": which[:300],
        }

    marker = out.find("__RS_JSON__")
    if marker < 0:
        err = (proc.stderr or out).strip().splitlines()
        return {
            "status": "error",
            "detail": err[-1][:300] if err else "the comparison harness produced no output",
        }

    rows = json.loads(out[marker + len("__RS_JSON__") :])
    disagreements = [r for r in rows if not r["same"]]
    exercised = [r for r in rows if r["old"][0] == "ok" or r["new"][0] == "ok"]

    if not exercised:
        return {
            "status": "inconclusive",
            "detail": f"all {len(rows)} argument sets raised on both sides",
            "tried": len(rows),
        }

    if disagreements:
        w = disagreements[0]
        return {
            "status": "differs",
            "detail": f"{len(disagreements)} of {len(rows)} inputs disagree",
            "witness": {
                "args": w["args"],
                "old": f"{w['old'][0]}: {w['old'][1]}",
                "new": f"{w['new'][0]}: {w['new'][1]}",
            },
            "tried": len(rows),
            "exercised": len(exercised),
        }

    return {
        "status": "agree",
        "detail": f"{len(exercised)} of {len(rows)} inputs exercised it, all agree",
        "tried": len(rows),
        "exercised": len(exercised),
    }
