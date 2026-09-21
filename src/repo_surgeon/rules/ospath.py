"""`os.path` -> `pathlib`.

The textbook safe migration, which is exactly why it is worth checking. Most of the
mapping is clean, and a few corners are not:

- `os.path.join("a", "/b")` returns `"/b"`; `Path("a") / "/b"` also yields `/b`, but
  `Path("a").joinpath("/b")` and string concatenation do not agree in general.
- `os.path.basename("a/b/")` is `""`; `Path("a/b/").name` is `"b"`.
- `os.path.splitext(".bashrc")` gives `(".bashrc", "")`; `Path(".bashrc").suffix` is `""`
  but `.stem` is `".bashrc"`, so a naive rewrite of the *pair* can silently swap them.
- `os.path.abspath` normalises without touching the filesystem; `Path.resolve()` follows
  symlinks. On a path that is a symlink the two return different strings.

Every one of those is a real behaviour change that a test suite usually will not catch,
because the suite passes tidy paths. The differential rung is what finds them.
"""

from __future__ import annotations

import ast

from repo_surgeon.rules import Rule, register

_FUNCS = {
    "join",
    "exists",
    "basename",
    "dirname",
    "splitext",
    "abspath",
    "isfile",
    "isdir",
    "realpath",
    "expanduser",
    "getsize",
    "islink",
    "relpath",
    "normpath",
}


def _match(node: ast.AST) -> str | None:
    """An `os.path.<fn>(...)` call, however `os.path` was imported."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr not in _FUNCS:
        return None

    # os.path.join(...)
    v = f.value
    if (
        isinstance(v, ast.Attribute)
        and v.attr == "path"
        and isinstance(v.value, ast.Name)
        and v.value.id == "os"
    ):
        return f"os.path.{f.attr}(...)"
    # `from os import path` / `import os.path as osp` -> path.join(...), osp.join(...)
    if isinstance(v, ast.Name) and v.id in {"path", "osp", "op"}:
        return f"{v.id}.{f.attr}(...)"
    return None


register(
    Rule(
        name="ospath",
        summary="os.path calls -> pathlib.Path",
        instruction=(
            "Replace every `os.path` call with the equivalent `pathlib.Path` operation.\n"
            "Add `from pathlib import Path` if the function needs it.\n"
            "Keep the function's return TYPE exactly as it is: if it returned `str`, it must\n"
            "still return `str` - wrap the result in `str(...)` rather than returning a Path.\n"
            "Change nothing else about the function."
        ),
        match=_match,
    )
)
