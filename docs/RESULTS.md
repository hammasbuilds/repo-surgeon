# Results: `pypa/build`, `os.path` -> `pathlib`

One run, reproduced by:

```bash
git clone --depth 1 https://github.com/pypa/build targets/build
repo-surgeon run targets/build --rule ospath --out surgeon-out
```

Model `qwen2.5-coder:14b` via Ollama, on one Quadro RTX 5000. Every number below is read
out of `result.json` from that run, not typed by hand.

## Summary

```
24 sites in 10 functions across 3 files

proposed 10   landed 2 (20%)   REFUSED 8 (80%)
```

| rung | refused |
|---|---:|
| `differential` | 7 |
| `signature` | 1 |

## Every verdict

| function | verdict | rung | detail |
|---|---|---|---|
| `build_package` | **landed** | `-` | differential 38 of 40 inputs exercised it, all agree |
| `build_package_via_sdist` | refused | `differential` | inconclusive: all 40 argument sets raised on both sides |
| `_build_metadata` | refused | `differential` | load_error: new: ImportError: cannot import name 'ConfigSettings' from 'typing' (C:\Users\dell\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-n |
| `main` | refused | `differential` | inconclusive: all 24 argument sets raised on both sides |
| `_write_report` | refused | `differential` | load_error: new: ImportError: cannot import name 'StrPath' from 'typing' (C:\Users\dell\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\Lib |
| `_validate_sdist_archive` | refused | `differential` | load_error: new: ImportError: cannot import name 'StrPath' from 'typing' (C:\Users\dell\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\Lib |
| `_extract_sdist` | refused | `signature` | parameter annotations: ('StrPath', 'str', 'StrPath \| None') -> ('Union[str, Path]', 'str', 'Union[str, Path, None]') |
| `_validate_source_directory` | refused | `differential` | inconclusive: all 19 argument sets raised on both sides |
| `_validate_backend_path` | **landed** | `-` | differential 20 of 37 inputs exercised it, all agree; mutation 1/1 mutants distinguished |
| `_find_executable_and_scripts` | refused | `differential` | inconclusive: all 11 argument sets raised on both sides |

## The two that landed

```diff
- built.append(os.path.basename(out))
+ built.append(Path(out).name)

- resolved = os.path.join(source_dir, path)
- if not os.path.isdir(resolved):
+ resolved = Path(source_dir) / path
+ if not resolved.is_dir():
```

`basename` and `.name` are not equivalent in general - they disagree on a trailing slash.
The first change landed because 38 of 40 argument sets exercised that function and all 38
agreed. That is a claim about the inputs tried, not a proof of universal safety.

## Timing

The whole run took 3.0s with a warm generation cache (the first run, generating all ten rewrites, took 263s).
