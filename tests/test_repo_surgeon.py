"""Tests for the verification machinery. No model, no network.

The model is not tested here and could not usefully be - it is the one component whose
output this tool assumes nothing about. What must be right is everything that *judges*
that output, because a bug there is how a wrong change lands quietly.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from repo_surgeon import rules
from repo_surgeon.ledger import Ledger
from repo_surgeon.pipeline import apply_hunks, split_imports
from repo_surgeon.scout import module_header, python_files, scout_file
from repo_surgeon.types import Hunk, Verdict
from repo_surgeon.values import argument_sets, for_parameter, harvest
from repo_surgeon.verify import mutation, statics
from repo_surgeon.verify.differential import run_differential
from repo_surgeon.verify.suite import newly_failing

PATHLIB = "import os\nimport os.path\nfrom pathlib import Path"


# --- the differential rung -------------------------------------------------------------


def test_catches_the_trailing_slash_divergence():
    # os.path.basename("a/b/") == ""  but  Path("a/b/").name == "b".
    # This is the canonical example of a migration that every tidy test suite accepts.
    old = "def base(p):\n    return os.path.basename(p)\n"
    new = "def base(p):\n    return Path(p).name\n"
    r = run_differential(PATHLIB, old, new, "base", ['("a/b",)', '("a/b/",)'])
    assert r["status"] == "differs"
    assert r["witness"]["args"] == "('a/b/',)"


def test_equivalent_rewrite_agrees():
    old = "def up(s):\n    return s.upper()\n"
    new = "def up(s):\n    return str(s).upper()\n"
    r = run_differential("", old, new, "up", ['("a",)', '("",)', '("Ab",)'])
    assert r["status"] == "agree"
    assert r["exercised"] == 3


def test_each_version_recurses_into_itself():
    # Defining both in one namespace makes the old version's self-call resolve to the new
    # one, so the two appear to agree. Correct isolation gives 8 and 27; a leak gives 18.
    old = "def f(n):\n    return 1 if n <= 0 else 2 * f(n - 1)\n"
    new = "def f(n):\n    return 1 if n <= 0 else 3 * f(n - 1)\n"
    r = run_differential("", old, new, "f", ["(3,)"])
    assert r["status"] == "differs"
    assert r["witness"]["old"] == "ok: 8"
    assert r["witness"]["new"] == "ok: 27"


def test_each_version_sees_its_own_helpers():
    old = "def _h(x):\n    return x + 1\n\ndef g(n):\n    return _h(n)\n"
    new = "def _h(x):\n    return x + 100\n\ndef g(n):\n    return _h(n)\n"
    r = run_differential("", old, new, "g", ["(1,)"])
    assert r["status"] == "differs"
    assert r["witness"]["old"] == "ok: 2"


def test_an_exception_counts_as_behaviour():
    # Returning None where the original raised is a behaviour change. Comparing only
    # return values would call this agreement and let it land.
    old = "def d(a, b):\n    return a / b\n"
    new = (
        "def d(a, b):\n"
        "    try:\n"
        "        return a / b\n"
        "    except ZeroDivisionError:\n"
        "        return None\n"
    )
    r = run_differential("", old, new, "d", ["(1, 1)", "(1, 0)"])
    assert r["status"] == "differs"
    assert "ZeroDivisionError" in r["witness"]["old"]
    assert r["witness"]["new"] == "ok: None"


def test_memory_addresses_are_not_a_behaviour_difference():
    # `<Foo object at 0x7f...>` reprs differently on every allocation, so comparing raw
    # makes any function returning a plain object a guaranteed "disagreement" - and the
    # report then presents the address as proof of a behaviour change.
    src = "class Foo:\n    pass\n\ndef make(x):\n    return Foo()\n"
    r = run_differential("", src, src, "make", ["(1,)", "(2,)"])
    assert r["status"] == "agree"


def test_addresses_inside_an_exception_message_are_normalised_too():
    src = "class Foo:\n    pass\n\ndef f(x):\n    raise ValueError(repr(Foo()))\n"
    r = run_differential("", src, src, "f", ["(1,)"])
    # Both sides raise the same exception; only the embedded address differs.
    assert r["status"] == "inconclusive"  # nothing was exercised, but they did not differ


def test_raising_on_both_sides_is_inconclusive_not_agreement():
    # The arguments never exercised the function, so nothing was proven. Calling this a
    # pass is exactly how an unverified change lands.
    old = "def f(x):\n    return x.method_that_does_not_exist()\n"
    new = "def f(x):\n    return x.other_missing_method()\n"
    r = run_differential("", old, new, "f", ["(1,)", "(2,)"])
    assert r["status"] == "inconclusive"


def test_no_argument_sets_is_inconclusive():
    r = run_differential("", "def f():\n    return 1\n", "def f():\n    return 1\n", "f", [])
    assert r["status"] == "inconclusive"


def test_proposal_that_does_not_load_is_reported_as_such():
    old = "def f(x):\n    return x\n"
    new = "def f(x):\n    return undefined_name(x)\n"
    r = run_differential("", old, new, "f", ["(1,)"])
    # It loads fine (the NameError is at call time), so this is a real disagreement.
    assert r["status"] == "differs"
    assert "NameError" in r["witness"]["new"]


# --- the static rungs ------------------------------------------------------------------


def test_syntax_rung():
    assert statics.parses("def f():\n    return 1\n")[0] is True
    ok, detail = statics.parses("def f(:\n")
    assert ok is False and "line" in detail


@pytest.mark.parametrize(
    "proposed,reason",
    [
        ("def copy(src, dst, follow=True):\n    pass\n", "parameters"),
        ("def copy(source, dst):\n    pass\n", "parameters"),
        ("def copy(src, dst, *extra):\n    pass\n", "*args"),
        ("def copy(src, dst=None):\n    pass\n", "defaults"),
    ],
)
def test_signature_changes_are_refused(proposed, reason):
    # A model asked to rewrite a function will sometimes return a better function that is
    # a different function. Callers still work, tests still pass, the API silently moved.
    original = "def copy(src, dst):\n    pass\n"
    ok, detail = statics.signature_kept(original, proposed, "copy")
    assert ok is False
    assert reason in detail


def test_annotation_changes_are_refused():
    # This one slipped through a real run: `archive: StrPath` came back as
    # `archive: Union[str, Path]`. Nothing executable changes, every test still passes,
    # and the package now promises its callers something different.
    original = "def f(archive: StrPath) -> str:\n    pass\n"
    proposed = "def f(archive: Union[str, Path]) -> str:\n    pass\n"
    ok, detail = statics.signature_kept(original, proposed, "f")
    assert ok is False
    assert "annotation" in detail


def test_return_annotation_change_is_refused():
    ok, detail = statics.signature_kept(
        "def f(x) -> str:\n    pass\n", "def f(x) -> Path:\n    pass\n", "f"
    )
    assert ok is False and "return annotation" in detail


def test_identical_signature_passes():
    src = "def copy(src, dst, *, follow=True):\n    pass\n"
    assert statics.signature_kept(src, src, "copy")[0] is True


def test_missing_function_is_refused():
    ok, detail = statics.signature_kept("def f():\n    pass\n", "def g():\n    pass\n", "f")
    assert ok is False and "does not define" in detail


def test_dropped_decorator_is_refused():
    original = "@property\ndef f(self):\n    return 1\n"
    proposed = "def f(self):\n    return 1\n"
    ok, detail = statics.decorators_kept(original, proposed, "f")
    assert ok is False and "property" in detail


# --- scouting --------------------------------------------------------------------------


def test_scout_finds_sites_and_attributes_them_to_the_inner_function(tmp_path):
    src = textwrap.dedent("""
        import os.path

        CONST = 3

        def outer(p):
            def inner(q):
                return os.path.join(q, "x")
            return inner(p)

        def untouched(a):
            return a
    """).strip()
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    hunks = scout_file(f, rules.get(["ospath"]), tmp_path)
    assert len(hunks) == 1
    # The helper is the smallest thing that can be replaced and still be called.
    assert hunks[0].func == "inner"


def test_methods_are_skipped(tmp_path):
    # A method cannot be called without an instance, so nothing about a rewrite of one
    # could be proven. Before this was handled, methods were extracted by line range,
    # came out indented, failed to parse, and were reported as "the proposal does not
    # define `__init__`" - which blames the model for the scout's mistake.
    src = textwrap.dedent("""
        import os.path

        class Builder:
            def __init__(self, root):
                self.root = os.path.abspath(root)

            def build(self, name):
                def helper(n):
                    return os.path.join(self.root, n)
                return helper(name)

        def free(p):
            return os.path.dirname(p)
    """).strip()
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    names = {h.func for h in scout_file(f, rules.get(["ospath"]), tmp_path)}
    assert "__init__" not in names
    # A nested function inside a method IS callable on its own.
    assert names == {"helper", "free"}


def test_header_is_pruned_to_what_the_function_reads(tmp_path):
    # Carrying every import means a function touching only os.path is unverifiable
    # whenever some unrelated third-party import at the top of its module is missing.
    src = textwrap.dedent("""
        import os.path
        import pyproject_hooks
        from somewhere import unrelated

        LIMIT = 5

        def f(p):
            return os.path.dirname(p)
    """).strip()
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    hunk = scout_file(f, rules.get(["ospath"]), tmp_path)[0]
    assert "import os.path" in hunk.module_header
    assert "pyproject_hooks" not in hunk.module_header
    assert "unrelated" not in hunk.module_header
    assert "LIMIT" not in hunk.module_header


def test_module_level_sites_are_skipped(tmp_path):
    # Nothing to call, so nothing that could be verified.
    f = tmp_path / "m.py"
    f.write_text('import os.path\nX = os.path.join("a", "b")\n', encoding="utf-8")
    assert scout_file(f, rules.get(["ospath"]), tmp_path) == []


def test_module_header_takes_imports_and_literals_but_not_calls():
    src = textwrap.dedent("""
        import os
        from pathlib import Path
        LIMIT = 5
        NAMES = ["a", "b"]
        SIDE_EFFECT = open("/etc/passwd")

        def f():
            pass
    """).strip()
    header = module_header(ast.parse(src), src)
    assert "import os" in header
    assert "LIMIT = 5" in header
    assert 'NAMES = ["a", "b"]' in header
    # Carrying this over would run it every time a function is isolated.
    assert "SIDE_EFFECT" not in header


def test_decorators_are_included_in_the_hunk(tmp_path):
    src = "import os.path\n\n@staticmethod\ndef f(p):\n    return os.path.join(p, 'x')\n"
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")
    hunk = scout_file(f, rules.get(["ospath"]), tmp_path)[0]
    # Excluding them would silently drop the decorator when the rewrite is applied.
    assert hunk.original.startswith("@staticmethod")


# --- rules -----------------------------------------------------------------------------


def test_ospath_rule_matches_aliases_and_ignores_others():
    match = rules.REGISTRY["ospath"].match
    assert match(ast.parse("os.path.join(a, b)").body[0].value)
    assert match(ast.parse("path.basename(x)").body[0].value)
    assert match(ast.parse("os.getcwd()").body[0].value) is None
    assert match(ast.parse("shutil.join(a, b)").body[0].value) is None


def test_percent_rule_matches_literals_only():
    match = rules.REGISTRY["percent_format"].match
    assert match(ast.parse('"%s" % x').body[0].value)
    assert match(ast.parse('"{}".format(x)').body[0].value)
    # A variable on the left is not a literal format string; and modulo on numbers is
    # arithmetic, not formatting.
    assert match(ast.parse("template % x").body[0].value) is None
    assert match(ast.parse("a % b").body[0].value) is None


def test_unknown_rule_name_is_an_error():
    with pytest.raises(KeyError, match="unknown rule"):
        rules.get(["no_such_rule"])


# --- argument values -------------------------------------------------------------------


def test_path_parameters_get_the_divergent_corners():
    vals = for_parameter("src_path", "str", {})
    # The trailing slash and the dotfile are what separate os.path from pathlib.
    assert '"a/b/"' in vals
    assert '".bashrc"' in vals


def test_argument_sets_vary_one_parameter_at_a_time():
    # One-at-a-time keeps the witness readable: exactly one value differs from the
    # baseline, so that value is the cause.
    fn = ast.parse("def f(a, b):\n    pass\n").body[0]
    sets = argument_sets(fn, {}, cap=10)
    assert sets[0].count(",") == 2  # two args plus the trailing comma
    baseline = sets[0]
    for s in sets[1:]:
        differing = sum(x != y for x, y in zip(s.split(", "), baseline.split(", "), strict=False))
        assert differing <= 1


def test_no_parameters_gives_an_empty_call():
    fn = ast.parse("def f():\n    pass\n").body[0]
    assert argument_sets(fn, {}) == ["()"]


def test_self_is_not_an_argument():
    fn = ast.parse("def m(self, x):\n    pass\n").body[0]
    sets = argument_sets(fn, {}, cap=3)
    assert sets[0].count(",") == 1


def test_harvest_reads_literals_from_the_repos_tests(tmp_path):
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_x.py").write_text(
        'def test_a():\n    join(base="/srv/data", name="x.txt")\n    f("positional")\n',
        encoding="utf-8",
    )
    got = harvest(tmp_path)
    assert "'/srv/data'" in got["base"]
    assert "'x.txt'" in got["name"]
    assert "'positional'" in got["*"]


# --- mutation --------------------------------------------------------------------------


def test_mutants_are_distinct_and_single_point():
    src = "def f(n):\n    if n > 0:\n        return n + 1\n    return 0\n"
    ms = mutation.mutants(src)
    assert len(ms) >= 3
    assert len(set(ms)) == len(ms)
    assert src not in ms


def test_kill_rate_is_higher_for_a_well_exercised_function():
    src = "def f(n):\n    if n > 2:\n        return n + 1\n    return 0\n"
    argsets = ["(0,)", "(1,)", "(2,)", "(3,)", "(10,)"]
    r = mutation.kill_rate("", src, "f", argsets)
    assert r["rate"] is not None and r["rate"] > 0.5

    # One input can distinguish far less.
    thin = mutation.kill_rate("", src, "f", ["(0,)"])
    assert thin["rate"] is not None
    assert thin["rate"] <= r["rate"]


def test_kill_rate_without_mutable_sites():
    r = mutation.kill_rate("", "def f():\n    return None\n", "f", ["()"])
    assert r["rate"] is None


# --- the suite rung --------------------------------------------------------------------


def test_only_newly_failing_tests_count():
    # A repo with pre-existing failures is normal. Blaming them on whatever change
    # happened to be applied would make every hunk unlandable.
    base = {"failed": {"tests/test_a.py::test_x"}}
    now = {"failed": {"tests/test_a.py::test_x", "tests/test_b.py::test_y"}}
    assert newly_failing(base, now) == {"tests/test_b.py::test_y"}


def test_a_fixed_test_is_not_a_regression():
    assert newly_failing({"failed": {"a", "b"}}, {"failed": {"a"}}) == set()


# --- ledger ----------------------------------------------------------------------------


def test_ledger_round_trips_and_resumes(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    led.append(Verdict("f.py::a::1", True, None, "fine"))
    led.append(Verdict("f.py::b::9", False, "differential", "differs", {"args": "(1,)"}))
    seen = led.seen()
    assert set(seen) == {"f.py::a::1", "f.py::b::9"}
    assert seen["f.py::b::9"]["rung"] == "differential"


def test_a_torn_final_line_does_not_lose_the_file(tmp_path):
    # A run killed mid-write must cost at most its last verdict, not the whole ledger.
    p = tmp_path / "l.jsonl"
    led = Ledger(p)
    led.append(Verdict("a", True))
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"hunk": "b", "lan')
    assert set(led.seen()) == {"a"}


# --- applying --------------------------------------------------------------------------


def test_hunks_are_applied_bottom_up(tmp_path):
    # Replacing a function changes the file's length, so a top-down pass would corrupt
    # every line number recorded below the first edit.
    src = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    h1 = Hunk(Path("m.py"), "a", 1, 2, "def a():\n    return 1", [])
    h1.proposed = "def a():\n    # grew\n    # by two\n    return 1"
    h2 = Hunk(Path("m.py"), "b", 5, 6, "def b():\n    return 2", [])
    h2.proposed = "def b():\n    return 22"

    apply_hunks(tmp_path, [h1, h2])
    out = f.read_text(encoding="utf-8")
    assert "return 22" in out
    assert "# by two" in out
    assert ast.parse(out)  # still valid Python


def test_imports_are_hoisted_not_spliced_mid_file(tmp_path):
    # The model returns "the function plus any imports it needs". Splicing that whole
    # block in at the function's line number drops `from pathlib import Path` into the
    # middle of the module, once per rewritten function.
    src = '"""Doc."""\n\nimport os\n\n\ndef a():\n    return 1\n'
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    h = Hunk(Path("m.py"), "a", 6, 7, "def a():\n    return 1", [])
    h.proposed = "from pathlib import Path\n\ndef a():\n    return Path('.')"

    apply_hunks(tmp_path, [h])
    out = f.read_text(encoding="utf-8")
    tree = ast.parse(out)

    import_lines = [n.lineno for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    func_line = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef))
    assert import_lines, "the import was dropped"
    assert max(import_lines) < func_line, "an import landed below the function"


def test_an_import_already_present_is_not_duplicated(tmp_path):
    src = "from pathlib import Path\n\n\ndef a():\n    return 1\n"
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")

    h = Hunk(Path("m.py"), "a", 4, 5, "def a():\n    return 1", [])
    h.proposed = "from pathlib import Path\n\ndef a():\n    return Path('.')"

    apply_hunks(tmp_path, [h])
    out = f.read_text(encoding="utf-8")
    assert out.count("from pathlib import Path") == 1


def test_split_imports_separates_the_two_parts():
    imports, body = split_imports("import os\nfrom pathlib import Path\n\ndef f():\n    pass\n")
    assert imports == ["import os", "from pathlib import Path"]
    assert body.strip().startswith("def f")


# --- which files get scouted at all ------------------------------------------------------
#
# Both of these fail the same way when they regress: the tool finds nothing and reports
# that as a successful run. For a tool whose whole output is "here is what I could prove",
# silently proving nothing is the worst possible failure.


def test_a_package_named_build_is_not_mistaken_for_an_artifact_dir(tmp_path):
    """This bug scouted `pypa/build` to zero os.path sites and called it success."""
    pkg = tmp_path / "src" / "build"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text("import os.path\n", encoding="utf-8")

    artifacts = tmp_path / "build"
    artifacts.mkdir()
    (artifacts / "generated.py").write_text("import os.path\n", encoding="utf-8")

    found = {p.name for p in python_files(tmp_path)}
    assert "m.py" in found  # src/build/ is somebody's source
    assert "generated.py" not in found  # ./build/ really is an artifact dir


def test_skip_list_is_applied_relative_to_root(tmp_path):
    # A checkout that happens to live under a directory called "dist" is still scoutable.
    root = tmp_path / "dist" / "myrepo"
    root.mkdir(parents=True)
    (root / "m.py").write_text("import os.path\n", encoding="utf-8")
    assert [p.name for p in python_files(root)] == ["m.py"]
