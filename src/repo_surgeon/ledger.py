"""A resumable append-only record of every verdict.

A migration over a few hundred hunks runs for hours, and the expensive parts - generation
and the differential subprocesses - are exactly the parts you do not want to repeat because
something crashed at hunk 240.

A JSONL ledger keyed by hunk gives that for about thirty lines, which is why there is no
workflow framework in this repo. The alternative considered was LangGraph with a
checkpointer; it would have added a dependency, a graph definition and a store to do what
`seen()` does here, and the hard part of this tool is the verification, which no
orchestration library helps with.

Append-only matters: a run that dies mid-write loses at most the last line, and a partial
final line is skipped on read rather than corrupting the whole file.
"""

from __future__ import annotations

import json
from pathlib import Path

from repo_surgeon.types import Verdict


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def seen(self) -> dict[str, dict]:
        """Every verdict already recorded, by hunk key."""
        out: dict[str, dict] = {}
        if not self.path.is_file():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from an interrupted run
            if "hunk" in row:
                out[row["hunk"]] = row
        return out

    def append(self, verdict: Verdict) -> None:
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(verdict.as_row()) + "\n")

    def rows(self) -> list[dict]:
        return list(self.seen().values())
