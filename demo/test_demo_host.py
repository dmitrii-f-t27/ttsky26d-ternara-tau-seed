# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""pytest checks for the board demo (demo/tau_seed_demo.py) on a computer.

The demo runs against MockTT (demo/mock_tt.py), which is backed by the cycle
model of the chip (test/model.py) and raises ProtocolError when the host
breaks a rule of docs/info.md. Expected numbers are written out here from the
datasheet, not computed by the demo.
Run:  cd <repo> && python -B -m pytest -q demo/test_demo_host.py
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in (HERE, os.path.join(ROOT, "test")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import pytest  # noqa: E402

import mock_tt  # noqa: E402
import tau_seed_demo  # noqa: E402
import vectors  # noqa: E402
from mock_tt import BrokenMock, MockTT, ProtocolError  # noqa: E402

DEMO_PATH = os.path.join(HERE, "tau_seed_demo.py")
DOCS_PATH = os.path.join(ROOT, "docs", "info.md")

D18_BYTES_SPEC = [0xD3, 0x34, 0x4D, 0xD3, 0x34, 0x4D, 0xD3, 0x34]
D18_NEG_BYTES_SPEC = [0x71, 0x1C, 0xC7, 0x71, 0x1C, 0xC7, 0x71, 0x1C]

# Commands the demo sends (written out from the demo's sequence, NW = 32):
#   D18 (a): CLEAR, 8 x LOAD_W, 32 x STEP                 = 41
#   D18 (b): CLEAR, 8 x LOAD_W, 32 x STEP                 = 41
#   D18 (c): CLEAR, 21 streamed steps (11 of 32 weights are 0) = 22
#   D7/D8:   CLEAR, 255 x STEP_NEG, 1 x STEP_NEG          = 257
DEMO_COMMANDS = 41 + 41 + 22 + 257


def bring_up(tt, idle=3, strobe_in_reset=0):
    """The reset sequence of the host rules, by hand (not the demo's code)."""
    tt.clock_project_stop()
    tt.uio_oe_pico.value = 0x1F
    tt.ui_in.value = 0
    tt.uio_in.value = strobe_in_reset
    tt.reset_project(True)
    for _ in range(3):
        tt.clock_project_once()
    tt.reset_project(False)
    for _ in range(idle):
        tt.clock_project_once()


def send(tt, cmd, data, wait=3):
    """One command by hand: CMD+DATA, STROBE, `wait` pulses, STROBE low, 3 pulses."""
    tt.ui_in.value = data & 0xFF
    tt.uio_in.value = cmd << 1
    tt.uio_in.value = (cmd << 1) | 1
    for _ in range(wait):
        tt.clock_project_once()
    tt.uio_in.value = 0
    for _ in range(3):
        tt.clock_project_once()


def acc_and_ack(tt):
    lo = tt.uo_out.value
    tt.uio_in.value = 0x10
    hi = tt.uo_out.value
    ack = (tt.uio_out.value >> 7) & 1
    tt.uio_in.value = 0
    v = (hi << 8) | lo
    return (v - 65536 if v & 0x8000 else v), ack


# ---------------------------------------------------------------------------
# the demo against the good mock and the broken ones (BrokenMock.KINDS)
# ---------------------------------------------------------------------------

def test_good_mock_passes(capsys):
    tt = MockTT()
    ok = tau_seed_demo.run(tt)
    lines = capsys.readouterr().out.splitlines()
    assert ok is True
    assert lines[-1] == "TAU SEED DEMO: PASS"
    assert sum(1 for ln in lines if ln.startswith("TAU SEED DEMO:")) == 1
    assert not any("FAIL" in ln for ln in lines)
    text = "\n".join(lines)
    for want in (
        "D18 (a) stored weights: acc = 2099 (0x0833), SAT 0",
        "D18 (b) negated weights: acc = -2099 (0xF7CD), SAT 0",
        "D18 (c) streamed weights: acc = 2099 (0x0833), SAT 0",
        "D7 after 255 steps of +128: acc = 32640 (0x7F80), SAT 0",
        "D8 one more step: acc = 32767 (0x7FFF), SAT 1",
    ):
        assert want in text
    # the chip itself ended in the D8 state
    assert tt.cyc.model.acc == 32767 and tt.cyc.model.sat == 1
    assert tt.cyc.fired == DEMO_COMMANDS
    # every ACK came on exactly the 3rd pulse, every STROBE rise followed 3 low pulses
    assert tt.ack_latencies == [3] * DEMO_COMMANDS
    assert tt.low_before_rise == [3] * DEMO_COMMANDS
    # pins left idle
    assert tt.pending_edges is None
    assert tt.uio_in.value == 0 and tt.ui_in.value == 0
    assert tt.uio_oe_pico.value == 0x1F


def test_reset_order_and_single_stepping():
    tt = MockTT()
    assert tau_seed_demo.run(tt) is True
    ev = tt.events
    assert ev[0] == ("stop",)
    assert ev[1] == ("oe_pico", 0x1F)
    i_on = ev.index(("reset", True))
    i_off = ev.index(("reset", False))
    assert 1 < i_on < i_off
    assert ev[i_on + 1:i_off].count(("edge",)) >= 2  # reset is clocked
    assert ev[i_off + 1:i_off + 4] == [("edge",)] * 3  # then 3 pulses before anything else
    assert all(e[0] in ("stop", "oe_pico", "reset", "edge") for e in ev)
    assert ("stop",) not in ev[1:]  # no later clock changes


# (kind of the broken chip, the reason the demo must print after "TAU SEED DEMO: FAIL ")
BROKEN_CASES = [
    ("sign", "D18 (a) STEP 0: acc -105 SAT 0, expected acc 105 SAT 0"),
    ("stuck_ack", "no ACK within 8 clocks (cmd 1)"),
    ("swap_bytes", "D18 (a) STEP 0: acc 26880 SAT 0, expected acc 105 SAT 0"),
    # one case per comparison of the demo's check(): SAT, SPTR0, ACK level, flags stable over BSEL
    ("sat_never_set", "D8 result: acc 32767 SAT 0, expected acc 32767 SAT 1"),
    ("sptr0_stuck", "D18 (a) STEP 0: SPTR0 1, expected 0"),
    ("extra_ack", "after reset: ACK 1, expected 0"),
    ("flags_follow_bsel", "SAT/SPTR0/ACK changed with BSEL"),
]


@pytest.mark.parametrize("kind, reason", BROKEN_CASES)
def test_broken_mock_fails(kind, reason, capsys):
    tt = BrokenMock(kind)
    ok = tau_seed_demo.run(tt)  # must not raise
    lines = capsys.readouterr().out.splitlines()
    assert ok is False
    assert lines[-1] == "TAU SEED DEMO: FAIL " + reason
    assert not any("PASS" in ln for ln in lines)
    assert tt.uio_in.value == 0  # STROBE left low
    if kind == "stuck_ack":
        assert tt.pending_edges == 8  # waited 8 pulses, not 9


def test_broken_mock_kinds():
    assert BrokenMock.KINDS == (
        "extra_ack",
        "flags_follow_bsel",
        "sat_never_set",
        "sign",
        "sptr0_stuck",
        "stuck_ack",
        "swap_bytes",
    )
    with pytest.raises(ValueError):
        BrokenMock("no_such_fault")


def test_dirty_power_up_and_synchronous_reset():
    tt = MockTT()
    assert tt.uo_out.value == 0xC7  # acc = -12345 = 0xCFC7
    assert tt.uio_out.value == 0xA0  # ACK 1, SPTR0 0, SAT 1
    tt.reset_project(True)
    tt.reset_project(False)  # no pulse: a synchronous reset has not seen a clock edge
    assert tt.uo_out.value == 0xC7
    tt.reset_project(True)
    tt.clock_project_once()
    tt.reset_project(False)
    assert tt.uo_out.value == 0x00
    assert tt.uio_out.value == 0x40  # SPTR0 1, ACK 0, SAT 0


def test_running_pwm_clock_is_stopped_first(capsys):
    tt = MockTT(pwm_running=True)
    assert tau_seed_demo.run(tt) is True
    assert tt.events[0] == ("stop",)
    assert capsys.readouterr().out.splitlines()[-1] == "TAU SEED DEMO: PASS"


def test_main_enables_project_and_sets_mode(capsys):
    tt = MockTT(prepared=False)
    with mock_tt.fake_ttboard(tt):
        ok = tau_seed_demo.main()
    assert ok is True
    assert tt.calls == [("get",), ("enable", "tt_um_ternara_tau_seed"), ("mode", "ASIC_RP_CONTROL")]
    assert capsys.readouterr().out.splitlines()[-1] == "TAU SEED DEMO: PASS"
    assert "ttboard" not in sys.modules and "ttboard.mode" not in sys.modules


def test_every_broken_mock_kind_has_a_failing_case():
    # the cases of test_broken_mock_fails cover all kinds, each once
    assert sorted(k for k, _ in BROKEN_CASES) == sorted(BrokenMock.KINDS)


def test_good_mock_flags_do_not_depend_on_bsel():
    # the check that the broken mock "flags_follow_bsel" trips is not tripped by the good chip
    tt = MockTT()
    bring_up(tt)
    send(tt, 4, 5)
    flags = []
    for bsel in (0, 1, 0):
        tt.uio_in.value = bsel << 4
        flags.append(tt.uio_out.value & 0xE0)
    assert flags[0] == flags[1] == flags[2]


def test_without_rp_control_mode_the_demo_fails(capsys):
    tt = MockTT(prepared=False)
    tt.shuttle.tt_um_ternara_tau_seed.enable()  # enabled, but ui_in is not driven
    assert tau_seed_demo.run(tt) is False
    assert capsys.readouterr().out.splitlines()[-1].startswith("TAU SEED DEMO: FAIL ")


class _NoDesignShuttle(object):
    """A shuttle list without the design (a firmware release that predates the shuttle)."""

    def __getattr__(self, name):
        raise AttributeError("no such design: " + name)


class _ModeRefused(MockTT):
    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        raise RuntimeError("mode switch refused")


class _ClockFails(MockTT):
    def clock_project_once(self, msDelay=0):
        if self.edges >= 5:
            raise OSError("pulse failed")
        MockTT.clock_project_once(self, msDelay)


def _main_fail_line(tt, capsys):
    with mock_tt.fake_ttboard(tt):
        ok = tau_seed_demo.main()  # must not raise
    lines = capsys.readouterr().out.splitlines()
    assert ok is False
    assert sum(1 for ln in lines if ln.startswith("TAU SEED DEMO:")) == 1
    assert not any("PASS" in ln for ln in lines)
    return lines[-1]


def test_main_without_the_design_in_the_shuttle_fails(capsys):
    tt = MockTT(prepared=False)
    tt.shuttle = _NoDesignShuttle()
    assert _main_fail_line(tt, capsys) == (
        "TAU SEED DEMO: FAIL setup: AttributeError('no such design: tt_um_ternara_tau_seed')"
    )
    assert tt.calls == [("get",)]  # stopped before the mode was set


def test_main_other_board_errors_end_in_a_fail_line(capsys):
    tt = _ModeRefused(prepared=False)
    assert _main_fail_line(tt, capsys) == "TAU SEED DEMO: FAIL setup: RuntimeError('mode switch refused')"
    # an error from the board in the middle of the run
    tt = _ClockFails(prepared=False)
    assert _main_fail_line(tt, capsys) == "TAU SEED DEMO: FAIL setup: OSError('pulse failed')"
    assert tt.uio_in.value == 0 and tt.ui_in.value == 0  # pins left idle


def test_main_without_the_sdk_fails(capsys, monkeypatch):
    # a None entry in sys.modules makes `import ttboard...` raise ImportError, as on a computer
    for name in ("ttboard", "ttboard.demoboard", "ttboard.mode"):
        monkeypatch.setitem(sys.modules, name, None)
    ok = tau_seed_demo.main()  # must not raise
    lines = capsys.readouterr().out.splitlines()
    assert ok is False
    assert len(lines) == 1 and lines[0].startswith("TAU SEED DEMO: FAIL setup: ")
    assert "ttboard" in lines[0]


# ---------------------------------------------------------------------------
# the mock enforces the host rules (so a good run is meaningful)
# ---------------------------------------------------------------------------

def test_valid_command_control():
    tt = MockTT()
    bring_up(tt)
    send(tt, 4, 5)  # STEP_POS x=5
    assert acc_and_ack(tt) == (5, 1)
    assert tt.ack_latencies == [3]


def test_cmd_or_data_change_before_ack_raises():
    for change in ("data", "cmd", "data_after_strobe"):
        tt = MockTT()
        bring_up(tt)
        if change == "data":
            tt.ui_in.value = 5
            tt.uio_in.value = (4 << 1) | 1
            tt.clock_project_once()
            with pytest.raises(ProtocolError, match="CMD or DATA"):
                tt.ui_in.value = 6
        elif change == "cmd":
            tt.ui_in.value = 5
            tt.uio_in.value = (4 << 1) | 1
            tt.clock_project_once()
            with pytest.raises(ProtocolError, match="CMD or DATA"):
                tt.uio_in.value = (5 << 1) | 1
        else:  # STROBE first, DATA after: DATA changes while STROBE is high
            tt.uio_in.value = (4 << 1) | 1
            with pytest.raises(ProtocolError, match="CMD or DATA"):
                tt.ui_in.value = 5


def test_changes_after_ack_are_allowed():
    tt = MockTT()
    bring_up(tt)
    tt.ui_in.value = 10
    tt.uio_in.value = (4 << 1) | 1
    for _ in range(3):
        tt.clock_project_once()
    tt.ui_in.value = 0x80  # ACK has toggled: CMD and DATA may change, STROBE still high
    tt.uio_in.value = (5 << 1) | 1
    for _ in range(10):
        tt.clock_project_once()
    tt.uio_in.value = 0
    assert acc_and_ack(tt) == (10, 1)


def test_strobe_rise_after_too_few_low_pulses_raises():
    tt = MockTT()
    bring_up(tt, idle=2)
    tt.ui_in.value = 5
    tt.uio_in.value = (4 << 1) | 1
    with pytest.raises(ProtocolError, match="at least 3"):
        tt.clock_project_once()

    # exactly 3 low pulses between two commands is fine, 2 is not
    tt = MockTT()
    bring_up(tt)
    send(tt, 4, 5)
    tt.ui_in.value = 6
    tt.uio_in.value = (4 << 1) | 1
    tt.clock_project_once()  # 3 low pulses before this rise: accepted

    tt = MockTT()
    bring_up(tt)
    tt.ui_in.value = 5
    tt.uio_in.value = (4 << 1) | 1
    for _ in range(3):
        tt.clock_project_once()
    tt.uio_in.value = 0
    tt.clock_project_once()
    tt.clock_project_once()
    tt.uio_in.value = (4 << 1) | 1
    with pytest.raises(ProtocolError, match="after 2 clock"):
        tt.clock_project_once()


def test_strobe_high_right_after_reset_raises():
    tt = MockTT()
    bring_up(tt, idle=0, strobe_in_reset=1)  # STROBE high through reset: allowed
    with pytest.raises(ProtocolError, match="after 0 clock"):
        tt.clock_project_once()  # first pulse after rst_n rises with STROBE high


def test_pwm_calls_raise():
    tt = MockTT()
    with pytest.raises(ProtocolError, match="single-step"):
        tt.clock_project_PWM(1000)

    tt = MockTT(pwm_running=True)
    with pytest.raises(ProtocolError, match="PWM clock is running"):
        tt.ui_in.value = 0
    with pytest.raises(ProtocolError, match="PWM clock is running"):
        tt.reset_project(True)
    tt.clock_project_stop()
    tt.ui_in.value = 0  # fine once stopped


def test_driving_the_chip_outputs_raises():
    tt = MockTT()
    tt.uio_oe_pico.value = 0xFF
    with pytest.raises(ProtocolError, match=r"uio\[7:5\]"):
        tt.clock_project_once()


def test_clock_before_enable_raises():
    tt = MockTT(prepared=False)
    with pytest.raises(ProtocolError, match="enable"):
        tt.clock_project_once()


def test_bus_value_checks():
    tt = MockTT()
    for bad in (-1, 256, 1.5, True, "1"):
        with pytest.raises(ValueError):
            tt.ui_in.value = bad
    with pytest.raises(AttributeError):
        tt.uo_out.value = 1
    with pytest.raises(AttributeError):
        tt.shuttle.tt_um_no_such_design.enable()


# ---------------------------------------------------------------------------
# data and constants
# ---------------------------------------------------------------------------

def test_demo_d18_equals_vectors():
    assert tau_seed_demo.D18_W == vectors.D18_W
    assert tau_seed_demo.D18_X == vectors.D18_X
    assert tau_seed_demo.D18_BYTES == vectors.D18_BYTES
    assert tau_seed_demo.D18_NEG_BYTES == vectors.D18_NEG_BYTES
    assert tau_seed_demo.D18_EXPECT == vectors.D18_EXPECT
    assert tau_seed_demo.D18_NEG_EXPECT == vectors.D18_NEG_EXPECT


def test_demo_d18_numbers_from_the_spec():
    assert tau_seed_demo.D18_BYTES == D18_BYTES_SPEC
    assert tau_seed_demo.D18_NEG_BYTES == D18_NEG_BYTES_SPEC
    assert tau_seed_demo.D18_EXPECT == 2099
    assert tau_seed_demo.D18_NEG_EXPECT == -2099
    assert tau_seed_demo.D18_X[:5] == [-105, 0, 105, -101, 4]
    assert len(tau_seed_demo.D18_W) == len(tau_seed_demo.D18_X) == 32


def test_demo_clamp():
    clamp = tau_seed_demo.clamp
    assert clamp(32767) == 32767 and clamp(32768) == 32767 and clamp(32895) == 32767
    assert clamp(-32768) == -32768 and clamp(-32769) == -32768 and clamp(-32896) == -32768
    assert clamp(0) == 0 and clamp(-1) == -1


def test_demo_constants():
    assert tau_seed_demo.NW == 32
    assert tau_seed_demo.UIO_OE_PICO == 0x1F
    assert tau_seed_demo.ACK_WAIT == 8
    assert tau_seed_demo.GAP_EDGES >= 3 and tau_seed_demo.RESET_EDGES >= 2


def test_demo_nw_matches_rtl():
    with open(os.path.join(ROOT, "src", "project.v"), encoding="utf-8") as f:
        found = re.findall(r"localparam\s+integer\s+NW\s*=\s*(\d+)\s*;", f.read())
    assert found == [str(tau_seed_demo.NW)]


# ---------------------------------------------------------------------------
# the docs (manual steps and numbers) against the demo and the mock
# ---------------------------------------------------------------------------

def _docs_text():
    with open(DOCS_PATH, encoding="utf-8") as f:
        return f.read()


def test_docs_manual_steps_run_on_the_mock(capsys):
    blocks = [b for b in re.findall(r"```python\n(.*?)```", _docs_text(), re.S) if "DemoBoard.get()" in b]
    assert len(blocks) == 1
    tt = MockTT(prepared=False, pwm_running=True)
    with mock_tt.fake_ttboard(tt):
        exec(compile(blocks[0], "docs/info.md", "exec"), {"__name__": "docs_manual_steps"})
    assert capsys.readouterr().out.strip().splitlines()[-1] == "acc = 5 ACK = 1"
    assert tt.cyc.fired == 1


def test_docs_numbers_match_the_demo():
    text = _docs_text()
    assert " ".join("%02X" % b for b in tau_seed_demo.D18_BYTES) in text
    assert " ".join("%02X" % b for b in tau_seed_demo.D18_NEG_BYTES) in text
    assert "2099 (`0x0833`)" in text and "-2099 (`0xF7CD`)" in text
    assert "`tau_guard: PASS mode=rtl tests=24/24`" in text
    assert re.search(r"NW = %d ternary weights" % tau_seed_demo.NW, text)
    assert text.isascii()


def test_docs_host_rules_and_demo_board_notes():
    text = _docs_text()
    # host rule: a free-running clock needs one clock period between ACK and the read
    sentence = (
        "With a free-running clock, read `Q` (and change `BSEL`) at least one clock period "
        "after `ACK` is seen."
    )
    assert text.count(sentence) == 1
    # the demo board needs a firmware release that lists this shuttle
    assert text.count("The board needs a firmware release whose shuttle list includes TTSKY26d.") == 1
    # the three template headings stay
    assert [ln for ln in text.splitlines() if ln.startswith("## ")] == [
        "## How it works",
        "## How to test",
        "## External hardware",
    ]


# ---------------------------------------------------------------------------
# language subset of the demo script
# ---------------------------------------------------------------------------

class _Scan(ast.NodeVisitor):
    """Finds constructs outside the subset: f-strings, annotations, walrus, async,
    match, and imports other than `time` and `ttboard.*` (the latter only inside main)."""

    def __init__(self):
        self.found = []
        self.stack = []

    def add(self, node, what):
        self.found.append("line %d: %s" % (getattr(node, "lineno", 0), what))

    def visit_JoinedStr(self, node):
        self.add(node, "f-string")

    def visit_AnnAssign(self, node):
        self.add(node, "variable annotation")

    def visit_NamedExpr(self, node):
        self.add(node, "walrus")

    def visit_Match(self, node):
        self.add(node, "match")

    def visit_AsyncFunctionDef(self, node):
        self.add(node, "async def")

    def visit_AsyncFor(self, node):
        self.add(node, "async for")

    def visit_AsyncWith(self, node):
        self.add(node, "async with")

    def visit_Await(self, node):
        self.add(node, "await")

    def visit_TryStar(self, node):
        self.add(node, "except*")

    def visit_FunctionDef(self, node):
        a = node.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if arg is not None and arg.annotation is not None:
                self.add(node, "argument annotation")
        if node.returns is not None:
            self.add(node, "return annotation")
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _import(self, node, name):
        top = name.split(".")[0]
        if top == "time":
            return
        if top == "ttboard":
            if self.stack[:1] != ["main"]:
                self.add(node, "ttboard import outside main()")
            return
        self.add(node, "import %s" % name)

    def visit_Import(self, node):
        for alias in node.names:
            self._import(node, alias.name)

    def visit_ImportFrom(self, node):
        if node.level or node.module is None:
            self.add(node, "relative import")
        else:
            self._import(node, node.module)


def subset_violations(source):
    scan = _Scan()
    scan.visit(ast.parse(source))
    return scan.found


def test_demo_language_subset():
    with open(DEMO_PATH, encoding="utf-8") as f:
        source = f.read()
    assert subset_violations(source) == []
    assert source.isascii()
    assert "clock_project_PWM" not in source


@pytest.mark.parametrize(
    "snippet",
    [
        'x = f"{1}"',
        "def f(a: int):\n    pass",
        "def f() -> int:\n    return 1",
        "x: int = 1",
        "if (n := 1):\n    pass",
        "async def f():\n    pass",
        "match 1:\n    case 1:\n        pass",
        "import os",
        "from os import path",
        "import ttboard.demoboard",
        "def helper():\n    from ttboard.mode import RPMode",
        "from . import x",
    ],
)
def test_subset_scanner_flags(snippet):
    assert subset_violations(snippet) != []


@pytest.mark.parametrize(
    "snippet",
    [
        "import time",
        "def main():\n    from ttboard.demoboard import DemoBoard\n    from ttboard.mode import RPMode",
        "x = '%d' % 3\nprint(x)",
    ],
)
def test_subset_scanner_accepts(snippet):
    assert subset_violations(snippet) == []
