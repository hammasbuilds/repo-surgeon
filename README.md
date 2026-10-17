# repo-surgeon

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![runtime deps](https://img.shields.io/badge/runtime%20deps-0-brightgreen)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

We built repo-surgeon to run mechanical codebase migrations (`os.path` to `pathlib`,
`%`-formatting to f-strings) and refuse every change it cannot prove. It finds each site with
`ast`, has a local model rewrite the enclosing function once, then checks the rewrite by
execution: same signature, same results and exceptions on the same inputs, no weaker tests,
and a suite no worse than before. Whatever fails a check is refused with the reason attached.
It is for maintainers who want a migration they can review by reading the refusals, not by
re-checking every hunk.

## What it does

- **Scout** (no model, changes nothing): lists every migration site per function, so you can
  see whether a repo is worth a model run.
- **Propose**: one call to `qwen2.5-coder:14b` per function, one attempt, no retries.
- **Verify** on a five-rung ladder; a verdict names the first rung a proposal failed:

| | rung | what it establishes | model? |
|---|---|---|:--:|
| 1 | `syntax` | it parses | no |
| 2 | `signature` | same name, parameters, defaults, decorators and annotations | no |
| 3 | `differential` | old and new agree (results and exceptions) on every input tried | no |
| 4 | `mutation` | the change did not make the function less distinguishable from a broken version | no |
| 5 | `suite` | the repo's own tests are no worse than the baseline taken before any edit | no |

- Inputs for rung 3 come from literals harvested from the target repo's own tests, from type
  annotations, and from a corner pool per parameter name (`""`, `"a/b/"`, `".bashrc"`,
  `"archive.tar.gz"`, `"/abs"`, `"a/../b"`), varied one at a time so a disagreement points
  at one value.
- If every argument set makes both versions raise, the result is `inconclusive` and refused.
- **Apply** what was proved, edit the working tree in place, and write a resumable JSONL
  ledger plus an evidence table of every verdict.

## Sample input / output

`repo-surgeon scout targets/build --rule ospath -v` on the vendored copy of
[`pypa/build`](https://github.com/pypa/build):

```
targets\build
  24 sites in 10 functions across 3 files

  ospath                24  os.path calls -> pathlib.Path

  src\build\__main__.py:327 build_package()  1 site(s): ospath
  src\build\__main__.py:366 build_package_via_sdist()  2 site(s): ospath
  src\build\__main__.py:429 _build_metadata()  2 site(s): ospath
  src\build\__main__.py:702 main()  4 site(s): ospath
  src\build\__main__.py:768 _write_report()  3 site(s): ospath
  src\build\__main__.py:840 _validate_sdist_archive()  1 site(s): ospath
  src\build\__main__.py:872 _extract_sdist()  2 site(s): ospath
  src\build\_builder.py:74 _validate_source_directory()  5 site(s): ospath
  src\build\_builder.py:156 _validate_backend_path()  2 site(s): ospath
  src\build\env.py:492 _find_executable_and_scripts()  2 site(s): ospath
```

`python demo.py` scouts this repository itself:

```
repo-surgeon: which mechanical migrations does this repo still need?

<clone>/repo-surgeon
  26 sites in 11 functions across 3 files

  ospath                24  os.path calls -> pathlib.Path
  percent_format         2  %-formatting and .format() -> f-strings
```

## Results

One full run on `pypa/build`, migrating `os.path` to `pathlib` (details in
[docs/RESULTS.md](docs/RESULTS.md)):

```
24 sites in 10 functions across 3 files

proposed 10   landed 2 (20%)   REFUSED 8 (80%)

refused at:
  signature         1   changed the function's interface
  differential      7   could not be proven equivalent
```

The two changes that landed, both behaviour-identical on every input tried, with `build`'s
own suite no worse:

```diff
- built.append(os.path.basename(out))
+ built.append(Path(out).name)

- resolved = os.path.join(source_dir, path)
- if not os.path.isdir(resolved):
+ resolved = Path(source_dir) / path
+ if not resolved.is_dir():
```

`basename("a/b/")` is `""` while `Path("a/b/").name` is `"b"`, so the first rewrite is not
safe in general; it landed because 38 of 40 argument sets exercised it on this function and
all 38 agreed.

The eight refusals:

- **4 for lack of evidence**: `main`, `_validate_source_directory` and others take objects the
  harness cannot build, so every argument set raised on both sides.
- **3 invented imports**: the model wrote `from typing import StrPath`, but `StrPath` is
  `build`'s own alias: `load_error: new: ImportError: cannot import name 'StrPath' from 'typing'`.
- **1 interface change**: `_extract_sdist`'s `archive: StrPath` became `archive: Union[str, Path]`.
  Every test still passes; rung 2 catches it because annotations are part of the signature.

## Tech stack

- Python 3.11+, zero runtime dependencies
- `ast` for scouting and signature checks; both function versions loaded in separate module
  namespaces for the differential rung; `subprocess` for the target's pytest
- `urllib` to a local Ollama server running `qwen2.5-coder:14b`; no API key, no hosted call
- pytest, ruff, hatchling, GitHub Actions (Ubuntu and Windows, Python 3.11 to 3.13)

## Run it

```bash
git clone https://github.com/hammasbuilds/repo-surgeon
cd repo-surgeon
uv sync --all-groups

uv run python demo.py

# free, no model, changes nothing
uv run repo-surgeon scout /path/to/repo --rule ospath -v

# propose, verify, apply (needs Ollama with qwen2.5-coder:14b)
uv run repo-surgeon run /path/to/repo --rule ospath --out surgeon-out
```

- `run` edits the working tree in place and refuses to start on a dirty checkout, so
  `git diff` is the change and `git checkout .` is the undo.
- It migrates module-level functions; methods are skipped, because building an instance
  means running the code under test.
- The target repo's own dependencies must be importable; otherwise its functions are refused
  and labelled as environment refusals.
- Rules: `ospath`, `percent_format` (in `src/repo_surgeon/rules/`).

Code layout: `src/repo_surgeon/` holds `scout.py`, `propose.py`, `values.py`, `verify/`
(one module per rung), `pipeline.py`, `ledger.py` and `report.py`.

Related: [pr-referee](https://github.com/hammasbuilds/pr-referee) uses the same engine to
review pull requests; [code-llm-lab](https://github.com/hammasbuilds/code-llm-lab) measured
LLM review at 44.8% precision, which is why the model's opinion is never used as evidence here.

## Licence

MIT, see [LICENSE](LICENSE). `targets/build` is a vendored copy of
[pypa/build](https://github.com/pypa/build) (MIT).
