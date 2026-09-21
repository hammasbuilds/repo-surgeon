"""Migration rules.

A rule is two things: a way to recognise its own sites in an AST, and a sentence telling
the model what to do about them. Nothing else - the rule does not perform the edit and it
does not decide whether the edit was acceptable. Those belong to the model and to the
ladder respectively, and keeping them apart is what lets a new rule be added without
touching any verification code.

`instruction` is deliberately narrow. A rule that says "modernise this function" invites
the model to rewrite everything, and every unrelated change it makes is something the
ladder then has to refuse. Narrow instructions raise the landing rate by not asking for
work nobody wanted.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

MatchFn = Callable[[ast.AST], str | None]


@dataclass(frozen=True)
class Rule:
    name: str
    summary: str
    instruction: str
    match: MatchFn
    """Given a node, return a short snippet if it is a site, else None."""


REGISTRY: dict[str, Rule] = {}


def register(rule: Rule) -> Rule:
    REGISTRY[rule.name] = rule
    return rule


def get(names: list[str] | None = None) -> list[Rule]:
    if not names:
        return list(REGISTRY.values())
    missing = [n for n in names if n not in REGISTRY]
    if missing:
        raise KeyError(f"unknown rule(s): {', '.join(missing)}; have {', '.join(REGISTRY)}")
    return [REGISTRY[n] for n in names]


from repo_surgeon.rules import ospath, percent_format  # noqa: E402,F401  (registers them)
