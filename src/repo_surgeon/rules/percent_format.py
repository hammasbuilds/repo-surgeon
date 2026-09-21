"""`"..." % x` and `"...".format(x)` -> f-strings.

This one looks like pure cosmetics and is the most dangerous rule in the set, which makes
it the best demonstration of why the ladder exists.

`%` formatting is not string interpolation. It is an operator whose behaviour depends on
the *type* of its right-hand side:

    "%s" % some_tuple      # spreads the tuple across placeholders, or raises
    "%s" % some_list       # formats the list
    "%d" % "5"             # TypeError - f"{'5'}" would not raise
    "%s" % None            # "None"
    "%.2f" % Decimal(...)  # different rounding path from format()

An f-string rewrite of any of those changes behaviour, and no test suite that formats tidy
values will notice. A model asked to "modernise this" will happily make the change, and it
will look right in review.

So this rule exists to be caught. The interesting output is not the hunks that land - it
is the ones the differential rung refuses, with the input that proves it.
"""

from __future__ import annotations

import ast

from repo_surgeon.rules import Rule, register


def _match(node: ast.AST) -> str | None:
    # "literal %s" % value
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and "%" in node.left.value
    ):
        return f'"{node.left.value[:40]}" % ...'

    # "literal {}".format(value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return f'"{node.func.value.value[:40]}".format(...)'
    return None


register(
    Rule(
        name="percent_format",
        summary="%-formatting and .format() -> f-strings",
        instruction=(
            "Replace `%` formatting and `.format()` calls on string literals with f-strings.\n"
            "Preserve the format specifiers exactly: `%.2f` becomes `{value:.2f}`, `%5d`\n"
            "becomes `{value:5d}`, `%r` becomes `{value!r}`, `%s` becomes `{value}`.\n"
            "Do not change logging calls that pass arguments separately - `log.info('%s', x)`\n"
            "is deferred formatting and must stay as it is.\n"
            "Change nothing else about the function."
        ),
        match=_match,
    )
)
