# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""pytest checks for tau_guard.py, expected_tests.json and tau_check_config.py.

The guard is exercised on synthetic cocotb-style results files, through its
command line (exit code and the single output line) as the Makefile uses it.
The gate-level name set is covered by a synthetic gl results file, because a
real gate-level run needs the sky130 netlist and happens only in CI.

Names and counts are written out here; they are not read from
expected_tests.json, which is under test.
Run:  cd <repo> && python -B -m pytest -q test/test_tau_guard.py
"""

import ast
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pytest  # noqa: E402

import tau_check_config  # noqa: E402
import tau_guard  # noqa: E402

REPO = os.path.dirname(HERE)
GUARD = os.path.join(HERE, "tau_guard.py")
CHECK = os.path.join(HERE, "tau_check_config.py")
EXPECTED = os.path.join(HERE, "expected_tests.json")
TEST_PY = os.path.join(HERE, "test.py")

DIRECTED = [
    "test_d01_reset_state",
    "test_d02_strobe_high_through_reset",
    "test_d03_zero_weights",
    "test_d04_all_plus",
    "test_d05_all_minus",
    "test_d06_reserved_weight_code",
    "test_d07_d08_saturate_high",
    "test_d09_d10_saturate_low",
    "test_d11_sptr_wrap",
    "test_d12_lptr_wrap",
    "test_d13_clear_mid_pass",
    "test_d14_strobe_held_high",
    "test_d15_byte_select",
    "test_d16_nop_and_reserved",
    "test_d17_reset_mid_pass",
    "test_d18_neuron",
    "test_d19_inputs_after_ack_ignored",
    "test_d20_handshake_timing",
    "test_d21_saturation_recovery",
    "test_nw_probe",
]
RANDOM_GL = ["test_random_seed_a", "test_random_seed_b"]
RANDOM_RTL = RANDOM_GL + ["test_random_seed_c", "test_random_seed_d"]
RTL_NAMES = DIRECTED + RANDOM_RTL  # 24
GL_NAMES = DIRECTED + RANDOM_GL  # 22


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write_results(path, names, kinds=None, honest_counters=True, counters=None):
    """Write a cocotb-style results file (testsuites / testsuite / testcase).

    kinds           {name: 'failure' | 'error' | 'skipped'}: add that child element
    honest_counters the testsuite counters follow the children (like cocotb);
                    False writes 0 everywhere so only the child can be noticed
    counters        {'failures': n, 'errors': n, 'skipped': n} written as given
    """
    kinds = kinds or {}
    count = {"failures": 0, "errors": 0, "skipped": 0}
    if honest_counters:
        for kind in kinds.values():
            count[{"failure": "failures", "error": "errors", "skipped": "skipped"}[kind]] += 1
    if counters:
        count.update(counters)
    root = ET.Element("testsuites", name="cocotb tests")
    suite = ET.SubElement(
        root,
        "testsuite",
        name="test",
        errors=str(count["errors"]),
        failures=str(count["failures"]),
        skipped=str(count["skipped"]),
        tests=str(len(names)),
        time="1.000",
    )
    for name in names:
        case = ET.SubElement(suite, "testcase", classname="test", name=name, time="0.001")
        if name in kinds:
            ET.SubElement(case, kinds[name], message="synthetic")
        ET.SubElement(case, "properties")
    ET.ElementTree(root).write(str(path), encoding="utf-8", xml_declaration=True)


def run_guard(results, mode, expected=EXPECTED):
    proc = subprocess.run(
        [sys.executable, "-B", GUARD, "--results", str(results), "--expected", str(expected), "--mode", mode],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def assert_pass(res, line):
    rc, out, err = res
    assert rc == 0, "exit %d, output %r" % (rc, out)
    assert out == line + "\n", out
    assert err == ""


def assert_fail(res, *needles):
    rc, out, err = res
    assert rc != 0, "guard passed: %r" % out
    lines = out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("tau_guard: FAIL "), out
    for needle in needles:
        assert needle in lines[0], "%r not in %r" % (needle, lines[0])


# ---------------------------------------------------------------------------
# the basic cases: good runs, no tests, missing file, failed / errored / skipped
# tests, wrong names, wrong mode
# ---------------------------------------------------------------------------

def test_case_good_rtl(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES)
    assert len(RTL_NAMES) == 24
    assert_pass(run_guard(f, "rtl"), "tau_guard: PASS mode=rtl tests=24/24")


def test_case_good_gl(tmp_path):
    # synthetic gate-level results: 20 directed tests plus random A and B
    f = tmp_path / "results.xml"
    write_results(f, GL_NAMES)
    assert len(GL_NAMES) == 22
    assert_pass(run_guard(f, "gl"), "tau_guard: PASS mode=gl tests=22/22")


def test_case_good_order_does_not_matter(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, list(reversed(RTL_NAMES)))
    assert_pass(run_guard(f, "rtl"), "tau_guard: PASS mode=rtl tests=24/24")


@pytest.mark.parametrize("mode", ["rtl", "gl"])
def test_case_zero_testcases(tmp_path, mode):
    f = tmp_path / "results.xml"
    write_results(f, [])
    assert_fail(run_guard(f, mode), "0 testcases")


@pytest.mark.parametrize("body", [
    "<testsuites name=\"cocotb tests\" />",
    "<?xml version='1.0' encoding='utf-8'?>\n<testsuites name=\"cocotb tests\" />\n",
    "<testsuites name=\"cocotb tests\"><testsuite name=\"test\" errors=\"0\" failures=\"0\" "
    "skipped=\"0\" tests=\"0\" /></testsuites>",
])
def test_case_zero_testcases_as_cocotb_writes_them(tmp_path, body):
    f = tmp_path / "results.xml"
    f.write_text(body)
    assert_fail(run_guard(f, "rtl"), "0 testcases")


@pytest.mark.parametrize("mode", ["rtl", "gl"])
def test_case_missing_file(tmp_path, mode):
    assert_fail(run_guard(tmp_path / "no_such_results.xml", mode), "results file missing")


@pytest.mark.parametrize("kind", ["failure", "error", "skipped"])
def test_case_failure_error_skipped(tmp_path, kind):
    # the counters stay 0, so only the child element can be what fails the run
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES, kinds={"test_d18_neuron": kind}, honest_counters=False)
    assert_fail(run_guard(f, "rtl"), "test_d18_neuron (%s)" % kind)
    # and with honest counters (as cocotb writes them)
    write_results(f, RTL_NAMES, kinds={"test_d18_neuron": kind})
    assert_fail(run_guard(f, "rtl"), "test_d18_neuron (%s)" % kind)


def test_case_missing_name(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, [n for n in RTL_NAMES if n != "test_d21_saturation_recovery"])
    assert_fail(run_guard(f, "rtl"), "missing testcases: test_d21_saturation_recovery")


def test_case_extra_name(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES + ["test_extra_unlisted"])
    assert_fail(run_guard(f, "rtl"), "unexpected testcases: test_extra_unlisted")


def test_case_renamed_test(tmp_path):
    # same count, one name changed: both a missing and an unexpected name
    f = tmp_path / "results.xml"
    names = [n if n != "test_d05_all_minus" else "test_d05_all_minus_renamed" for n in RTL_NAMES]
    write_results(f, names)
    assert_fail(run_guard(f, "rtl"), "missing testcases: test_d05_all_minus", "unexpected testcases")


def test_case_duplicate_name(tmp_path):
    f = tmp_path / "results.xml"
    # 25 testcases, the set of names is still exactly the expected set
    write_results(f, RTL_NAMES + ["test_d04_all_plus"])
    assert_fail(run_guard(f, "rtl"), "duplicate testcase names: test_d04_all_plus")
    # 24 testcases (the right count), one name twice and another one absent
    names = [n if n != "test_d05_all_minus" else "test_d04_all_plus" for n in RTL_NAMES]
    write_results(f, names)
    assert len(names) == 24
    assert_fail(run_guard(f, "rtl"), "duplicate testcase names", "missing testcases: test_d05_all_minus")


def test_case_rtl_file_checked_as_gl(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES)
    assert_fail(run_guard(f, "gl"), "unexpected testcases: test_random_seed_c, test_random_seed_d")


def test_gl_file_checked_as_rtl(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, GL_NAMES)
    assert_fail(run_guard(f, "rtl"), "missing testcases: test_random_seed_c, test_random_seed_d")


# ---------------------------------------------------------------------------
# more ways to be green by accident
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("counter", ["failures", "errors", "skipped"])
def test_testsuite_counter_without_child_fails(tmp_path, counter):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES, counters={counter: 1})
    assert_fail(run_guard(f, "rtl"), "testsuite test counts %s=1" % counter)


def test_malformed_and_empty_files_fail(tmp_path):
    f = tmp_path / "results.xml"
    for body in ("", "not xml at all", "<testsuites><testsuite></testsuites>", "<other/>"):
        f.write_text(body)
        assert_fail(run_guard(f, "rtl"))
    f.write_bytes(b"\xff\xfe\x00garbage")
    assert_fail(run_guard(f, "rtl"))


def test_results_path_is_a_directory(tmp_path):
    assert_fail(run_guard(tmp_path, "rtl"), "results file missing")


def test_testcase_without_name_fails(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES)
    tree = ET.parse(str(f))
    del tree.getroot().find("testsuite").findall("testcase")[0].attrib["name"]
    tree.write(str(f), encoding="utf-8", xml_declaration=True)
    assert_fail(run_guard(f, "rtl"), "testcase without a name")


def test_two_suites_are_summed_up(tmp_path):
    # the names may be spread over testsuites; a failure in either one is seen
    root = ET.Element("testsuites")
    for i, part in enumerate((RTL_NAMES[:10], RTL_NAMES[10:])):
        suite = ET.SubElement(root, "testsuite", name="s%d" % i, errors="0", failures="0", skipped="0")
        for n in part:
            ET.SubElement(suite, "testcase", name=n)
    f = tmp_path / "results.xml"
    ET.ElementTree(root).write(str(f))
    assert_pass(run_guard(f, "rtl"), "tau_guard: PASS mode=rtl tests=24/24")
    ET.SubElement(root[1].findall("testcase")[0], "failure")
    ET.ElementTree(root).write(str(f))
    assert_fail(run_guard(f, "rtl"), "failure")


def test_bad_expected_file(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES)
    cases = {
        "missing": None,
        "not_json": "{",
        "no_mode": json.dumps({"gl": GL_NAMES}),
        "empty_list": json.dumps({"rtl": [], "gl": GL_NAMES}),
        "dup_in_expected": json.dumps({"rtl": RTL_NAMES + [RTL_NAMES[0]], "gl": GL_NAMES}),
        "non_string": json.dumps({"rtl": RTL_NAMES + [7], "gl": GL_NAMES}),
        "not_dict": json.dumps(RTL_NAMES),
    }
    for label, body in cases.items():
        exp = tmp_path / ("expected_%s.json" % label)
        if body is not None:
            exp.write_text(body)
        assert_fail(run_guard(f, "rtl", expected=exp))


def test_unknown_mode_is_refused(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, RTL_NAMES)
    rc, out, _ = run_guard(f, "both")
    assert rc != 0 and "PASS" not in out
    ok, message, _, _ = tau_guard.check(str(f), EXPECTED, "both")
    assert not ok and "unknown mode" in message


def test_check_function_matches_cli(tmp_path):
    f = tmp_path / "results.xml"
    write_results(f, GL_NAMES)
    assert tau_guard.check(str(f), EXPECTED, "gl") == (True, "", 22, 22)
    ok, message, found, want = tau_guard.check(str(f), EXPECTED, "rtl")
    assert not ok and (found, want) == (22, 24)


# ---------------------------------------------------------------------------
# expected_tests.json and test.py
# ---------------------------------------------------------------------------

def load_expected():
    with open(EXPECTED, "r", encoding="utf-8") as f:
        return json.load(f)


def test_expected_tests_json_lists():
    data = load_expected()
    assert sorted(data) == ["gl", "rtl"]
    assert data["rtl"] == RTL_NAMES
    assert data["gl"] == GL_NAMES
    assert len(data["rtl"]) == 24 and len(set(data["rtl"])) == 24
    assert len(data["gl"]) == 22 and len(set(data["gl"])) == 22
    assert set(data["rtl"]) - set(data["gl"]) == {"test_random_seed_c", "test_random_seed_d"}
    assert set(data["gl"]) <= set(data["rtl"])


def _is_cocotb_test(node):
    if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        return None
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "test"
            and isinstance(target.value, ast.Name)
            and target.value.id == "cocotb"
        ):
            return dec
    return None


def _tests_in_test_py():
    """(tests defined always, tests defined only under `if not GATES:`, all decorators)."""
    with open(TEST_PY, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    always, rtl_only, decorators = [], [], []
    for node in tree.body:
        dec = _is_cocotb_test(node)
        if dec is not None:
            always.append(node.name)
            decorators.append(dec)
        elif isinstance(node, ast.If):
            inner = [(n, _is_cocotb_test(n)) for n in ast.walk(node)]
            inner = [(n, d) for n, d in inner if d is not None]
            if not inner:
                continue
            t = node.test
            assert (
                isinstance(t, ast.UnaryOp)
                and isinstance(t.op, ast.Not)
                and isinstance(t.operand, ast.Name)
                and t.operand.id == "GATES"
                and not node.orelse
            ), "tests may be conditional only as `if not GATES:` without else"
            for n, d in inner:
                rtl_only.append(n.name)
                decorators.append(d)
    return always, rtl_only, decorators


def test_expected_names_equal_the_tests_in_test_py():
    data = load_expected()
    always, rtl_only, _ = _tests_in_test_py()
    assert len(set(always + rtl_only)) == len(always + rtl_only), "a test name is defined twice"
    assert set(always) == set(data["gl"])
    assert set(always) | set(rtl_only) == set(data["rtl"])
    assert sorted(rtl_only) == ["test_random_seed_c", "test_random_seed_d"]


def test_test_py_has_no_skip_or_expected_failure():
    _, _, decorators = _tests_in_test_py()
    assert len(decorators) == 24
    for dec in decorators:
        for kw in getattr(dec, "keywords", []):
            assert kw.arg not in ("skip", "expect_fail", "expect_error"), "test.py uses %s" % kw.arg


# ---------------------------------------------------------------------------
# tau_check_config.py
# ---------------------------------------------------------------------------

def make_tree(
    root,
    nw=32,
    cfg_nw=None,
    demo_nw=None,
    demo_dir=True,
    period=40,
    hz=25000000,
    top="tt_um_ternara_tau_seed",
    project_extra="",
    docs=True,
    docs_nw=None,
    docs_mhz=None,
    docs_text=None,
):
    """A synthetic repository tree with just the files tau_check_config.py reads.

    docs      write docs/info.md; by default it names `nw` and `hz // 1e6` MHz
    docs_nw   NW named in the datasheet instead of `nw`
    docs_mhz  MHz named in the datasheet instead of `hz // 1000000`
    docs_text the whole datasheet text, instead of the generated one
    """
    cfg_nw = nw if cfg_nw is None else cfg_nw
    demo_nw = nw if demo_nw is None else demo_nw
    docs_nw = nw if docs_nw is None else docs_nw
    docs_mhz = hz // 1000000 if docs_mhz is None else docs_mhz
    (root / "src").mkdir()
    (root / "test").mkdir()
    (root / "src" / "project.v").write_text(
        "module m;\n  localparam integer NW = %d;\n%s endmodule\n" % (nw, project_extra)
    )
    (root / "test" / "tau_config.py").write_text('"""doc."""\n\nNW = %d\n' % cfg_nw)
    (root / "src" / "config.json").write_text(
        json.dumps({"//": "comment", "CLOCK_PERIOD": period}, indent=2)
    )
    (root / "info.yaml").write_text(
        'project:\n  top_module:  "%s"\n  clock_hz:     %d       # Hz\n' % (top, hz)
    )
    if demo_dir:
        (root / "demo").mkdir()
        (root / "demo" / "tau_seed_demo.py").write_text("# demo\nNW = %d\n" % demo_nw)
    if docs:
        (root / "docs").mkdir()
        if docs_text is None:
            docs_text = (
                "## How it works\n\n"
                "- Stored weights: an on-chip memory of NW = %d ternary weights.\n"
                "The pointer wraps after NW steps.\n\n"
                "The target clock is %d MHz.\n" % (docs_nw, docs_mhz)
            )
        (root / "docs" / "info.md").write_text(docs_text)
    return root


def cfg_problems(root, strict=False):
    return tau_check_config.check(str(root), strict)


def fresh(tmp_path, name, **kw):
    """make_tree() in a new subdirectory of tmp_path."""
    sub = tmp_path / name
    sub.mkdir()
    return make_tree(sub, **kw)


def test_config_good_tree(tmp_path):
    problems, values = cfg_problems(make_tree(tmp_path))
    assert problems == []
    assert values == {"NW": 32, "demo": 32, "CLOCK_PERIOD": 40, "clock_hz": 25000000, "docs": "NW32/25MHz"}


@pytest.mark.parametrize("nw", [0, 16, 32])
def test_config_valid_nw(tmp_path, nw):
    problems, values = cfg_problems(make_tree(tmp_path, nw=nw))
    assert problems == [] and values["NW"] == nw


@pytest.mark.parametrize("nw", [1, 8, 24, 64])
def test_config_invalid_nw(tmp_path, nw):
    problems, _ = cfg_problems(make_tree(tmp_path, nw=nw))
    assert any("is not one of" in p for p in problems)


def test_config_nw_must_agree(tmp_path):
    problems, _ = cfg_problems(make_tree(tmp_path, nw=32, cfg_nw=16))
    assert any("tau_config.py NW = 16 but src/project.v NW = 32" in p for p in problems)


def test_config_demo_nw_must_agree(tmp_path):
    problems, _ = cfg_problems(make_tree(tmp_path, nw=32, demo_nw=16))
    assert any("demo/tau_seed_demo.py NW = 16 but src/project.v NW = 32" in p for p in problems)


def test_config_exactly_one_localparam(tmp_path):
    two = fresh(tmp_path, "two", project_extra="  localparam integer NW = 32;\n")
    problems, _ = cfg_problems(two)
    assert any("found 2" in p for p in problems)
    none = fresh(tmp_path, "none")
    (none / "src" / "project.v").write_text("module m; endmodule\n")
    problems, _ = cfg_problems(none)
    assert any("found 0" in p for p in problems)


@pytest.mark.parametrize("period,hz,ok", [
    (40, 25000000, True),
    (100, 10000000, True),
    (40, 10000000, False),
    (100, 25000000, False),
    (50, 20000000, False),  # 50 * 2e7 = 1e9 but 50 ns is not a permitted period
    (20, 50000000, False),
])
def test_config_clock(tmp_path, period, hz, ok):
    problems, _ = cfg_problems(make_tree(tmp_path, period=period, hz=hz))
    assert (problems == []) == ok, problems


def test_config_clock_period_not_an_integer(tmp_path):
    root = make_tree(tmp_path)
    for body in ('{"CLOCK_PERIOD": 40.0}', '{"CLOCK_PERIOD": "40"}', "{}", "[]", "not json", '{"CLOCK_PERIOD": true}'):
        (root / "src" / "config.json").write_text(body)
        problems, _ = cfg_problems(root)
        assert problems, body


def test_config_demo_is_skipped_while_there_is_no_demo_directory(tmp_path):
    root = make_tree(tmp_path, demo_dir=False)
    problems, values = cfg_problems(root)
    assert problems == []
    assert values["demo"].startswith("SKIPPED")
    problems, _ = cfg_problems(root, strict=True)
    assert any("no demo directory" in p for p in problems)


def test_config_demo_directory_without_script_fails(tmp_path):
    root = make_tree(tmp_path, demo_dir=False)
    (root / "demo").mkdir()
    problems, _ = cfg_problems(root)
    assert any("demo/tau_seed_demo.py: cannot read" in p for p in problems)


def test_config_clock_hz_is_skipped_for_the_template_info_yaml(tmp_path):
    root = make_tree(tmp_path, top="tt_um_example", hz=0)
    problems, values = cfg_problems(root)
    assert problems == []
    assert values["clock_hz"].startswith("SKIPPED")
    problems, _ = cfg_problems(root, strict=True)
    assert any("still the template" in p for p in problems)


def test_config_clock_hz_zero_fails_once_info_yaml_is_ours(tmp_path):
    problems, _ = cfg_problems(make_tree(tmp_path, hz=0))
    assert any("clock_hz = 0" in p for p in problems)


def test_config_missing_files_fail(tmp_path):
    for name in ("src/project.v", "test/tau_config.py", "src/config.json", "info.yaml", "docs/info.md"):
        root = fresh(tmp_path, name.replace("/", "_"))
        os.remove(str(root / name))
        problems, _ = cfg_problems(root)
        assert any("cannot read" in p for p in problems), name


# --- the datasheet (docs/info.md) -------------------------------------------

@pytest.mark.parametrize("nw", [0, 16, 32])
def test_config_docs_follow_nw(tmp_path, nw):
    problems, values = cfg_problems(make_tree(tmp_path, nw=nw))
    assert problems == []
    assert values["docs"] == "NW%d/25MHz" % nw


def test_config_docs_nw_must_agree(tmp_path):
    root = make_tree(tmp_path, nw=32, docs_nw=16)
    problems, _ = cfg_problems(root)
    assert any("docs/info.md says NW = 16 but src/project.v NW = 32" in p for p in problems), problems
    # the other NW mentions of the datasheet are checked too, not just the headline one
    other = fresh(tmp_path, "other", docs_text=(
        "NW = 32 ternary weights. The pointer wraps after NW steps (NW = 16 in the small build).\n"
        "The target clock is 25 MHz.\n"
    ))
    problems, _ = cfg_problems(other)
    assert any("docs/info.md mentions NW = 16 but src/project.v NW = 32" in p for p in problems), problems


def test_config_docs_nw_follows_project_v(tmp_path):
    # RTL changed to 16, datasheet still says 32
    problems, _ = cfg_problems(make_tree(tmp_path, nw=16, docs_nw=32))
    assert any("docs/info.md says NW = 32 but src/project.v NW = 16" in p for p in problems), problems


def test_config_docs_exactly_one_nw_sentence(tmp_path):
    two = fresh(tmp_path, "two", docs_text=(
        "NW = 32 ternary weights, and again NW = 32 ternary weights.\nThe target clock is 25 MHz.\n"))
    problems, _ = cfg_problems(two)
    assert any("NW = <n> ternary weights" in p and "found 2" in p for p in problems), problems
    none = fresh(tmp_path, "none", docs_text="The target clock is 25 MHz.\nNW = 32 weights.\n")
    problems, _ = cfg_problems(none)
    assert any("NW = <n> ternary weights" in p and "found 0" in p for p in problems), problems


def test_config_docs_clock_must_agree(tmp_path):
    problems, _ = cfg_problems(make_tree(tmp_path, docs_mhz=10))
    assert any("target clock is 10 MHz but info.yaml clock_hz = 25000000" in p for p in problems), problems
    # 100 ns / 10 MHz in the files, 25 MHz left in the datasheet
    problems, _ = cfg_problems(fresh(tmp_path, "slow", period=100, hz=10000000, docs_mhz=25))
    assert any("target clock is 25 MHz but info.yaml clock_hz = 10000000" in p for p in problems), problems
    # the datasheet follows the files
    problems, values = cfg_problems(fresh(tmp_path, "slow_ok", period=100, hz=10000000))
    assert problems == [] and values["docs"] == "NW32/10MHz"


def test_config_docs_exactly_one_clock_sentence(tmp_path):
    two = fresh(tmp_path, "two", docs_text=(
        "NW = 32 ternary weights.\nThe target clock is 25 MHz. The target clock is 25 MHz.\n"))
    problems, _ = cfg_problems(two)
    assert any("The target clock is <m> MHz" in p and "found 2" in p for p in problems), problems
    none = fresh(tmp_path, "none", docs_text="NW = 32 ternary weights.\nThe clock is 25 MHz.\n")
    problems, _ = cfg_problems(none)
    assert any("The target clock is <m> MHz" in p and "found 0" in p for p in problems), problems


def test_config_docs_clock_is_not_compared_for_the_template_info_yaml(tmp_path):
    root = make_tree(tmp_path, top="tt_um_example", hz=0, docs_mhz=25)
    problems, values = cfg_problems(root)
    assert problems == []
    assert values["clock_hz"].startswith("SKIPPED") and values["docs"] == "NW32/25MHz"
    # but the sentence has to be there exactly once
    problems, _ = cfg_problems(fresh(tmp_path, "nosentence", top="tt_um_example", hz=0,
                                     docs_text="NW = 32 ternary weights.\n"))
    assert any("The target clock is <m> MHz" in p for p in problems), problems


def test_config_real_repository_is_consistent():
    problems, values = tau_check_config.check(REPO)
    assert problems == [], problems
    assert values["NW"] == 32 and values["CLOCK_PERIOD"] == 40
    assert values["docs"] == "NW32/25MHz"


def test_config_cli_on_real_repository():
    proc = subprocess.run([sys.executable, "-B", CHECK], capture_output=True, text=True, cwd=HERE)
    assert proc.returncode == 0, proc.stdout
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("tau_check_config: PASS NW=32 CLOCK_PERIOD=40 clock_hz=")
    assert lines[0].endswith(" docs=NW32/25MHz")


def test_config_cli_failure_exit_code(tmp_path):
    root = make_tree(tmp_path, nw=32, cfg_nw=16)
    proc = subprocess.run(
        [sys.executable, "-B", CHECK, "--root", str(root)], capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert proc.stdout.startswith("tau_check_config: FAIL ")
