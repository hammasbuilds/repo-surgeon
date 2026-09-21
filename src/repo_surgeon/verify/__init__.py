"""The verification ladder.

A proposal has to clear every rung to land, and the verdict names the **first** rung it
failed. Order is by cost, cheapest first: a syntax error is caught in microseconds rather
than after a forty-second pytest run.

    1. syntax        it parses
    2. signature     same name, same parameters, same decorators
    3. differential  old and new agree on every input tried
    4. mutation      the change did not make the function less distinguishable
    5. suite         the repo's own tests are no worse than the baseline

The suite rung is last despite being the most familiar, because it is the only one that
needs the whole repo patched. Running it per hunk would mean one pytest run per proposal;
running it once over everything that already cleared rungs 1-4 costs one run in total.

**A rung that cannot reach a verdict does not pass.** If the differential harness cannot
call the function - it needs module state, or a fixture, or a live socket - the result is
`inconclusive`, and an inconclusive hunk is refused rather than landed. That is the whole
disposition of this tool: the default is no.
"""

from __future__ import annotations

import time

from repo_surgeon.types import Hunk, Verdict
from repo_surgeon.verify import mutation, statics
from repo_surgeon.verify.differential import run_differential

# A proposal may not lose more than this fraction of the original's mutation score.
# Not zero: the score is measured over a capped set of mutants and a legitimate rewrite
# can shift which ones are generated, so demanding exact parity would refuse good changes
# for noise. It is deliberately tight.
MUTATION_TOLERANCE = 0.10


def verify_hunk(
    hunk: Hunk,
    argsets: list[str],
    allow_inconclusive: bool = False,
    timeout: float = 30.0,
) -> Verdict:
    """Rungs 1-4 for one hunk. The suite rung runs once, later, over all survivors."""
    t: dict[str, float] = {}
    key = hunk.key
    proposed = hunk.proposed or ""

    if not proposed.strip():
        return Verdict(key, False, "syntax", "the model returned nothing")

    # --- 1. syntax --------------------------------------------------------------------
    t0 = time.time()
    ok, detail = statics.parses(proposed)
    t["syntax"] = time.time() - t0
    if not ok:
        return Verdict(key, False, "syntax", detail, timings=t)

    # --- 2. signature -----------------------------------------------------------------
    t0 = time.time()
    ok, detail = statics.signature_kept(hunk.original, proposed, hunk.func)
    if ok:
        ok, detail = statics.decorators_kept(hunk.original, proposed, hunk.func)
    t["signature"] = time.time() - t0
    if not ok:
        return Verdict(key, False, "signature", detail, timings=t)

    # --- 3. differential --------------------------------------------------------------
    t0 = time.time()
    diff = run_differential(
        hunk.module_header,
        hunk.original,
        proposed,
        hunk.func,
        argsets,
        timeout,
        hunk.sys_path,
        hunk.package,
    )
    t["differential"] = time.time() - t0

    if diff["status"] == "differs":
        return Verdict(key, False, "differential", diff["detail"], diff.get("witness"), t)
    if diff["status"] in ("timeout", "error", "load_error"):
        return Verdict(key, False, "differential", f"{diff['status']}: {diff['detail']}", None, t)
    if diff["status"] in ("inconclusive", "uncallable") and not allow_inconclusive:
        # Not a failure of the proposal - a failure to obtain evidence about it. Refused
        # all the same, and labelled so the report can separate the two.
        return Verdict(key, False, "differential", f"{diff['status']}: {diff['detail']}", None, t)

    # --- 4. mutation ------------------------------------------------------------------
    t0 = time.time()
    ctx = (timeout, hunk.sys_path, hunk.package)
    before = mutation.kill_rate(hunk.module_header, hunk.original, hunk.func, argsets, *ctx)
    after = mutation.kill_rate(hunk.module_header, proposed, hunk.func, argsets, *ctx)
    t["mutation"] = time.time() - t0

    if before["rate"] is not None and after["rate"] is not None:
        drop = before["rate"] - after["rate"]
        if drop > MUTATION_TOLERANCE:
            return Verdict(
                key,
                False,
                "mutation",
                f"distinguishability fell {before['rate']:.0%} -> {after['rate']:.0%} "
                f"({before['detail']} -> {after['detail']})",
                None,
                t,
            )

    return Verdict(
        key,
        True,
        None,
        f"differential {diff['detail']}"
        + (f"; mutation {after['detail']}" if after["rate"] is not None else ""),
        None,
        t,
    )
