"""The evidence table.

The refusals are the product, so they are what this prints first and in most detail. A
report that leads with "212 changes applied" is a diff summary; one that leads with "128
refused, here is the input that proves each one" is a reason to trust the 212.

Every refusal carries the rung it failed and, where the differential rung produced one,
the exact argument set on which the two versions disagree. That witness is the thing a
reviewer can check in ten seconds without reading the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

from repo_surgeon.pipeline import RunResult

RUNG_LABEL = {
    "syntax": "did not parse",
    "signature": "changed the function's interface",
    "differential": "behaved differently",
    "mutation": "made the function less distinguishable",
    "suite": "broke the repo's tests",
}


def summary(result: RunResult) -> str:
    n = len(result.verdicts)
    if not n:
        return "nothing was proposed."

    landed, refused = len(result.landed), len(result.refused)
    lines = [
        "=" * 78,
        f"proposed {n}   landed {landed} ({landed / n:.0%})   REFUSED {refused} "
        f"({refused / n:.0%})",
        "=" * 78,
    ]

    by_rung = result.by_rung()
    if by_rung:
        lines.append("\nrefused at:")
        for rung in ("syntax", "signature", "differential", "mutation", "suite"):
            if rung in by_rung:
                lines.append(
                    f"  {rung:14} {by_rung[rung]:4}  {'#' * round(50 * by_rung[rung] / n)}  "
                    f"{RUNG_LABEL[rung]}"
                )

    witnesses = [v for v in result.refused if v.witness]
    if witnesses:
        lines.append(f"\n{len(witnesses)} refusals carry an input that proves the difference:")
        for v in witnesses[:10]:
            lines.append(f"\n  {v.hunk_key}")
            lines.append(f"    args : {v.witness['args']}")
            lines.append(f"    old  : {v.witness['old']}")
            lines.append(f"    new  : {v.witness['new']}")
        if len(witnesses) > 10:
            lines.append(f"\n  ... and {len(witnesses) - 10} more in the ledger")

    inconclusive = [
        v
        for v in result.refused
        if v.rung == "differential" and ("inconclusive" in v.detail or "uncallable" in v.detail)
    ]
    if inconclusive:
        lines.append(
            f"\n{len(inconclusive)} were refused for lack of evidence rather than for being "
            "wrong -\nthe function could not be called in isolation, so nothing was proven "
            "either way."
        )

    lines.append(f"\ntook {result.seconds:.0f}s")
    return "\n".join(lines)


def write_markdown(result: RunResult, path: Path, repo_name: str) -> None:
    """The PR body: what landed, what did not, and why."""
    n = len(result.verdicts)
    landed, refused = len(result.landed), len(result.refused)
    by_rung = result.by_rung()

    out = [
        f"# repo-surgeon on `{repo_name}`",
        "",
        f"**Proposed {n} rewrites. Landed {landed}. Refused {refused}.**",
        "",
        "Every landed change cleared five rungs: it parses, it keeps the function's exact",
        "interface, it agrees with the original on every input tried, it did not make the",
        "function less distinguishable from a broken version of itself, and the repo's own",
        "test suite is no worse than it was.",
        "",
        "## Why the refusals happened",
        "",
        "| rung | count | meaning |",
        "|---|---:|---|",
    ]
    for rung in ("syntax", "signature", "differential", "mutation", "suite"):
        if rung in by_rung:
            out.append(f"| `{rung}` | {by_rung[rung]} | {RUNG_LABEL[rung]} |")

    witnesses = [v for v in result.refused if v.witness]
    if witnesses:
        out += [
            "",
            "## Proven behaviour changes",
            "",
            "Each of these is a rewrite that looked correct and is not. The argument set is",
            "the proof.",
            "",
            "| function | input | before | after |",
            "|---|---|---|---|",
        ]
        for v in witnesses[:25]:
            w = v.witness
            out.append(f"| `{v.hunk_key}` | `{w['args']}` | `{w['old']}` | `{w['new']}` |")

    if result.applied:
        out += ["", "## Applied", "", "| file | function |", "|---|---|"]
        for h in sorted(result.applied, key=lambda x: (str(x.path), x.lineno)):
            out.append(f"| `{h.path}` | `{h.func}` |")

    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="")


def write_json(result: RunResult, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "proposed": len(result.verdicts),
                "landed": len(result.landed),
                "refused": len(result.refused),
                "by_rung": result.by_rung(),
                "seconds": round(result.seconds, 1),
                "baseline": {
                    "counts": result.baseline.get("counts"),
                    "already_failing": len(result.baseline.get("failed", [])),
                },
                "verdicts": [v.as_row() for v in result.verdicts],
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="",
    )
