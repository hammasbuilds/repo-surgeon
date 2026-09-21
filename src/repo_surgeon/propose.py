"""Ask the model to rewrite one function. One attempt, no retry loop.

The single attempt is a measured decision rather than a simplification. `code-llm-lab`
project 02 ran self-debug loops to five rounds on MBPP and found rounds 1-2 captured 100%
of everything the loop ever achieved; project 04 pitted repair against rewrite on
first-attempt failures and 93.3% survived both. On work this mechanical, a second attempt
at the same function buys close to nothing and costs a full generation.

So when a proposal is refused, it is refused. The refusal goes in the report with its
reason, which is more useful to a human than a second guess would be.

The prompt is deliberately narrow: the function, the rule's one instruction, and an
explicit demand that nothing else change. A broad "modernise this" invites unrelated edits,
and every unrelated edit is something the ladder then has to refuse - which shows up as a
low landing rate that is the prompt's fault, not the model's.
"""

from __future__ import annotations

from repo_surgeon.model import extract_code, generate_many
from repo_surgeon.rules import REGISTRY
from repo_surgeon.types import Hunk

PROMPT = """Rewrite this Python function.

{instruction}

Do not change the function's name, its parameters, its defaults or its decorators.
Do not change what it returns or what exceptions it raises.
Do not add, remove or reorder anything the instruction did not ask for.

```python
{source}
```

Output ONLY the rewritten function, plus any import lines it needs. No explanation.
"""


def build_prompt(hunk: Hunk) -> str:
    instructions = [REGISTRY[r].instruction for r in sorted(hunk.rules) if r in REGISTRY]
    return PROMPT.format(
        instruction="\n\n".join(instructions),
        source=hunk.original,
    )


def propose(
    hunks: list[Hunk],
    model: str = "qwen2.5-coder:14b",
    workers: int = 8,
    progress: str = "propose",
) -> list[Hunk]:
    """Fill in `hunk.proposed` for each. Mutates and returns the same list."""
    if not hunks:
        return hunks
    raws = generate_many(
        [build_prompt(h) for h in hunks],
        model=model,
        temperature=0.0,
        workers=workers,
        num_predict=1024,
        progress=progress,
    )
    for hunk, raw in zip(hunks, raws, strict=True):
        hunk.proposed = extract_code(raw) if raw else ""
    return hunks
