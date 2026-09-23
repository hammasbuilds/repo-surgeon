"""Show what repo-surgeon does, in one command, with nothing to set up.

    python demo.py

Scouts this repository for migrations it can perform - os.path to pathlib,
%-formatting to f-strings - and reports the call sites it found. Scout only:
it changes nothing here.

Pointed at this repository itself, so the output below is a real run against
real code rather than a fixture built to flatter the tool.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    print('repo-surgeon: which mechanical migrations does this repo still need?', flush=True)
    print(flush=True)
    result = subprocess.run(
        [sys.executable, "-m", 'repo_surgeon.cli', "scout", "."],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    print(flush=True)
    print("Point it at your own code with:", flush=True)
    for line in ['repo-surgeon scout <repo>', 'repo-surgeon run <repo> --rule ospath']:
        print("    " + line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
