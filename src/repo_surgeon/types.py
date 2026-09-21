"""The three things that move through the pipeline.

A `Site` is somewhere a rule matched. A `Hunk` is a proposed replacement for the whole
function containing one or more sites. A `Verdict` is what the ladder decided about it.

Rewriting happens at **function** granularity rather than line granularity, and that is a
deliberate constraint rather than convenience. A single-line rewrite cannot be verified:
there is nothing to call. A function can be called with inputs and compared against the
original, which is the only evidence this tool accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The rungs of the ladder, in order. A hunk's verdict names the first one it failed.
RUNGS = ("syntax", "signature", "suite", "differential", "mutation")


@dataclass(frozen=True)
class Site:
    """One place a rule matched."""

    path: Path
    lineno: int
    rule: str
    snippet: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} [{self.rule}] {self.snippet}"


@dataclass
class Hunk:
    """One function, its original source, and the rewrite being proposed for it."""

    path: Path
    func: str
    lineno: int
    end_lineno: int
    original: str
    sites: list[Site]
    module_header: str = ""  # imports the function needs, lifted from its module
    proposed: str | None = None

    # Enough context to import the way the function's own module does. Without these, a
    # function whose module does `from . import x` or `import mypackage.y` cannot be
    # loaded at all, and the refusal says "uncallable" when the truth is that the harness
    # never put the package on the path.
    sys_path: str = ""  # directory to prepend to sys.path (the package's parent)
    package: str = ""  # dotted package name, so relative imports resolve

    @property
    def rules(self) -> set[str]:
        return {s.rule for s in self.sites}

    @property
    def key(self) -> str:
        """Stable id for the ledger, so a resumed run skips what it already decided."""
        return f"{self.path.as_posix()}::{self.func}::{self.lineno}"


@dataclass
class Verdict:
    """What the ladder decided, and the evidence for it.

    `rung` is the first rung that failed, or None when every rung passed. `detail` is
    always populated for a refusal - a refusal without a reason is not useful to anyone
    reading the report, and "the model produced something wrong" is not a reason.
    """

    hunk_key: str
    landed: bool
    rung: str | None = None
    detail: str = ""
    witness: dict[str, Any] | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "hunk": self.hunk_key,
            "landed": self.landed,
            "rung": self.rung,
            "detail": self.detail[:400],
            "witness": self.witness,
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
        }
