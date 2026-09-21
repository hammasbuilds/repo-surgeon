"""Run the target repo's own test suite against a patched working tree.

The expensive rung, so it runs late and it runs once per batch rather than once per hunk.
A suite that takes 40 seconds and 300 hunks is three and a half hours of pytest to learn
what a single run can establish.

The baseline is the point. A repo whose suite is already red - a missing optional
dependency, a test that needs network - would make every hunk look like it broke
something. So the suite is run once before anything is touched, the set of tests failing
then is recorded, and afterwards only *newly* failing tests count against a change.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# pytest's short summary lines: "FAILED tests/test_x.py::test_y - AssertionError: ..."
_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed)")


def run_suite(repo: Path, timeout: float = 900.0, extra: list[str] | None = None) -> dict:
    """Run pytest in `repo`. Returns which tests failed, not just whether any did."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--tb=no",
        "-rf",
        *(extra or []),
    ]
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "failed": set(),
            "detail": f"suite did not finish in {timeout}s",
            "usable": False,
        }
    except OSError as exc:
        return {"ok": False, "failed": set(), "detail": str(exc), "usable": False}

    out = (proc.stdout or "") + (proc.stderr or "")
    failed = set(_FAILED.findall(out))
    counts = {k: int(v) for v, k in _COUNTS.findall(out)}

    # Exit code 5 is "no tests collected" - a repo this tool cannot use this rung on.
    usable = proc.returncode != 5 and bool(counts)
    return {
        "ok": proc.returncode == 0,
        "failed": failed,
        "counts": counts,
        "detail": out.strip().splitlines()[-1][:300] if out.strip() else "",
        "usable": usable,
    }


def newly_failing(baseline: dict, current: dict) -> set[str]:
    """Tests that fail now and did not fail before.

    Only these count. A repo with pre-existing failures is normal, and blaming them on
    whatever change happened to be applied would make every hunk unlandable.
    """
    return set(current.get("failed", set())) - set(baseline.get("failed", set()))
