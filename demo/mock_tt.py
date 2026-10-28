# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Stand-in for the Tiny Tapeout demo board object `tt`, for CPython only.

demo/tau_seed_demo.py runs against MockTT on a computer. The chip behind the
mock is TauSeedCycle from test/model.py (STROBE synchroniser, synchronous
reset, ACK latency), the same model the cocotb tests compare the RTL with.

What is modelled (from the firmware SDK facts the demo relies on):

* tt.ui_in, tt.uio_in, tt.uio_oe_pico: writable buses (`.value`, 0..255).
  ui_in reaches the chip only when tt.mode is the "RP control" mode; with the
  input DIP switches off it reads 0 otherwise. A bit of uio_in reaches the chip
  only when the same bit of uio_oe_pico is 1 (an undriven pin reads 0).
* tt.uo_out, tt.uio_out: read-only buses. uio_out returns the chip's
  uio[7:5] and, for the bits the RP drives, the level the RP drives. A host
  that does not mask uio_out with 0xE0 therefore reads wrong flags.
* tt.clock_project_once(msDelay=0): one full clock pulse. The chip samples its
  inputs at the rising edge. Needs the project to be enabled first.
* tt.clock_project_stop(): stops a running PWM clock, clock low.
* tt.reset_project(True / False): rst_n low / high. The chip's reset is
  synchronous, so it needs clock pulses while rst_n is low.
* tt.shuttle.tt_um_ternara_tau_seed.enable(), tt.mode.

At construction the chip is in a fixed, non-reset state (power-up garbage), so
a host that skips the clocked reset fails.

ProtocolError is raised when the host breaks a rule of docs/info.md:

* CMD or DATA changes while STROBE is high and ACK has not toggled yet;
* STROBE rises after fewer than 3 clock pulses with STROBE low (also: STROBE
  already high at the first pulse after rst_n rises);
* clock_project_PWM is called (the demo single-steps);
* the pins or the reset are written while a PWM clock is still running;
* the RP drives uio[7:5], which the chip drives;
* the clock is pulsed before the project is enabled.

BrokenMock(kind) is a chip with one fault (see BrokenMock.KINDS): "sign" (every
weight acts with the wrong sign), "stuck_ack" (ACK never toggles), "swap_bytes"
(BSEL selects the other Q byte), "sat_never_set" (SAT stays 0), "sptr0_stuck"
(SPTR0 stays 1), "extra_ack" (ACK is already 1 after reset) and
"flags_follow_bsel" (the flag pins change when BSEL changes). The demo must
report FAIL for each, without raising.
"""

import contextlib
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DIR = os.path.join(os.path.dirname(_HERE), "test")
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from model import STEP, STEP_NEG, STEP_POS, TauSeedCycle, TauSeedModel  # noqa: E402

MIN_LOW_EDGES = 3
TOP_MODULE = "tt_um_ternara_tau_seed"


class ProtocolError(Exception):
    """The host broke a rule of the pin protocol."""


# ---------------------------------------------------------------------------
# chips with one fault each
# ---------------------------------------------------------------------------

_SWAP_POS_NEG = {STEP_POS: STEP_NEG, STEP_NEG: STEP_POS}
_FLIP_CODE = {1: 3, 3: 1}


class _SignFlippedModel(TauSeedModel):
    """Every weight acts with the opposite sign (stored and streamed)."""

    def command(self, cmd, data):
        cmd &= 7
        if cmd == STEP and self.nw:
            saved = self.codes[self.sptr]
            self.codes[self.sptr] = _FLIP_CODE.get(saved, saved)
            TauSeedModel.command(self, cmd, data)
            self.codes[(self.sptr - 1) % self.nw] = saved
        else:
            TauSeedModel.command(self, _SWAP_POS_NEG.get(cmd, cmd), data)


class _StuckAckModel(TauSeedModel):
    """ACK stays 0: the commands execute, the handshake never completes."""

    def command(self, cmd, data):
        TauSeedModel.command(self, cmd, data)
        self.ack = 0


class _SwapBytesModel(TauSeedModel):
    """BSEL selects the other Q byte."""

    def outputs(self, bsel):
        return TauSeedModel.outputs(self, 0 if bsel else 1)


class _SatNeverSetModel(TauSeedModel):
    """SAT stays 0, whatever the accumulator does."""

    def command(self, cmd, data):
        TauSeedModel.command(self, cmd, data)
        self.sat = 0


class _Sptr0StuckModel(TauSeedModel):
    """SPTR0 reads 1 all the time, so it does not show the read pointer moving."""

    def outputs(self, bsel):
        out = TauSeedModel.outputs(self, bsel)
        out["sptr0"] = 1
        out["uio_out"] |= 0x40
        return out


class _ExtraAckModel(TauSeedModel):
    """One ACK toggle too many: ACK is 1 after reset, before any command has run."""

    def reset(self):
        TauSeedModel.reset(self)
        self.ack = 1


class _FlagsFollowBselModel(TauSeedModel):
    """With BSEL = 1 the flag pins uio[7:5] show acc[15:13] instead of ACK, SPTR0, SAT."""

    def outputs(self, bsel):
        out = TauSeedModel.outputs(self, bsel)
        if bsel:
            out["uio_out"] = (out["uio_out"] & 0x1F) | (((self.acc & 0xFFFF) >> 8) & 0xE0)
        return out


_FAULTS = {
    "sign": _SignFlippedModel,
    "stuck_ack": _StuckAckModel,
    "swap_bytes": _SwapBytesModel,
    "sat_never_set": _SatNeverSetModel,
    "sptr0_stuck": _Sptr0StuckModel,
    "extra_ack": _ExtraAckModel,
    "flags_follow_bsel": _FlagsFollowBselModel,
}


# ---------------------------------------------------------------------------
# the board object
# ---------------------------------------------------------------------------

class _Bus(object):
    """A byte-wide bus with a `.value` attribute."""

    def __init__(self, name, getter, setter=None):
        self._name = name
        self._get = getter
        self._set = setter

    @property
    def value(self):
        return self._get()

    @value.setter
    def value(self, v):
        if self._set is None:
            raise AttributeError("%s is read-only" % self._name)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
            raise ValueError("%s.value must be an int in 0..255, got %r" % (self._name, v))
        self._set(v)


class _Design(object):
    def __init__(self, tt):
        self._tt = tt

    def enable(self):
        self._tt.enabled = True
        self._tt.calls.append(("enable", TOP_MODULE))


class _Shuttle(object):
    def __init__(self, tt):
        setattr(self, TOP_MODULE, _Design(tt))


class MockTT(object):
    """The demo board object. See the module docstring."""

    RP_CONTROL = "ASIC_RP_CONTROL"

    def __init__(self, nw=32, dirty=True, pwm_running=False, prepared=True, model=None):
        """
        nw           stored weights of the chip (0, 16 or 32)
        dirty        start in a non-reset state, as a chip after power-up
        pwm_running  a PWM clock is running at construction
        prepared     True: project enabled and mode set (what main() does);
                     False: the host must call enable() and set the mode
        model        a replacement TauSeedModel (BrokenMock uses it)
        """
        self.cyc = TauSeedCycle(nw)
        if model is not None:
            self.cyc.model = model
        self.nw = nw
        if dirty:
            self._make_dirty()
        self.cyc.rst_n = 1  # pull-up on the board

        self.pwm_running = bool(pwm_running)
        self.enabled = bool(prepared)
        self._mode = self.RP_CONTROL if prepared else None
        self.calls = []  # ("get",) / ("enable", ..) / ("mode", ..): what main() did, in order
        self.events = []  # clocks, resets, stops, oe writes in call order

        self._ui = 0
        self._uio = 0
        self._oe_pico = 0
        self._rst_n = 1

        # protocol bookkeeping
        self._low_edges = 0
        self._prev_strobe = None
        self._hold = None  # (cmd, data, ack at the rise) while waiting for ACK
        self._tracking = False
        self._rise_n = 0
        self.edges = 0
        self.ack_latencies = []  # pulses from the first high STROBE sample to the ACK toggle
        self.low_before_rise = []  # pulses with STROBE low just before each STROBE rise

        self.shuttle = _Shuttle(self)
        self.ui_in = _Bus("ui_in", lambda: self._ui, self._set_ui)
        self.uio_in = _Bus("uio_in", lambda: self._uio, self._set_uio)
        self.uio_oe_pico = _Bus("uio_oe_pico", lambda: self._oe_pico, self._set_oe)
        self.uo_out = _Bus("uo_out", self._read_uo)
        self.uio_out = _Bus("uio_out", self._read_uio_out)

    # -- state ------------------------------------------------------------
    def _make_dirty(self):
        m = self.cyc.model
        m.acc = -12345
        m.sat = 1
        m.ack = 1
        if self.nw:
            m.sptr = 5 % self.nw
            m.lptr = 1 % (self.nw // 4)
            m.codes = [((i * 7) + 1) & 3 for i in range(self.nw)]
        # s2 = 1, p = 0: the chip would fire on the first pulse
        self.cyc.s1, self.cyc.s2, self.cyc.p = 0, 1, 0

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value
        self.calls.append(("mode", value))

    @property
    def pending_edges(self):
        """Pulses since STROBE was first sampled high, while no ACK has toggled (else None)."""
        return self._rise_n if self._tracking else None

    # -- what the chip sees ----------------------------------------------
    def _asic_ui(self):
        return self._ui if self.mode == self.RP_CONTROL else 0

    def _asic_uio(self):
        return self._uio & self._oe_pico

    def _inputs(self):
        uio = self._asic_uio()
        return (uio & 1, (uio >> 1) & 7, self._asic_ui())

    # -- writes -----------------------------------------------------------
    def _need_stopped_clock(self, what):
        if self.pwm_running:
            raise ProtocolError("%s written while a PWM clock is running: call clock_project_stop() first" % what)

    def _after_write(self, before):
        b_strobe, _, _ = before
        a_strobe, a_cmd, a_data = self._inputs()
        if not a_strobe:
            self._hold = None
        elif not b_strobe:
            self._hold = (a_cmd, a_data, self.cyc.model.ack)
        elif self._hold is not None and (a_cmd, a_data) != self._hold[:2]:
            raise ProtocolError("CMD or DATA changed while STROBE is high and ACK has not toggled")

    def _set_ui(self, v):
        self._need_stopped_clock("ui_in")
        before = self._inputs()
        self._ui = v
        self._after_write(before)

    def _set_uio(self, v):
        self._need_stopped_clock("uio_in")
        before = self._inputs()
        self._uio = v
        self._after_write(before)

    def _set_oe(self, v):
        before = self._inputs()
        self._oe_pico = v
        self.events.append(("oe_pico", v))
        self._after_write(before)

    # -- reads ------------------------------------------------------------
    def _read_uo(self):
        bsel = (self._asic_uio() >> 4) & 1
        return self.cyc.model.outputs(bsel)["q"]

    def _read_uio_out(self):
        bsel = (self._asic_uio() >> 4) & 1
        chip = self.cyc.model.outputs(bsel)["uio_out"] & 0xE0
        return chip | (self._uio & self._oe_pico & 0x1F)

    # -- the SDK calls the demo uses -------------------------------------
    def clock_project_stop(self):
        self.pwm_running = False
        self.events.append(("stop",))

    def clock_project_PWM(self, *args, **kwargs):
        raise ProtocolError("clock_project_PWM called: the demo must single-step the clock")

    def reset_project(self, active):
        self._need_stopped_clock("rst_n")
        self._rst_n = 0 if active else 1
        self._hold = None
        self.events.append(("reset", bool(active)))

    def clock_project_once(self, msDelay=0):
        if not self.enabled:
            raise ProtocolError("clock_project_once before tt.shuttle.%s.enable()" % TOP_MODULE)
        if self._oe_pico & 0xE0:
            raise ProtocolError("uio_oe_pico drives uio[7:5], which are outputs of the chip")
        self.pwm_running = False  # the SDK stops a PWM clock first
        self.events.append(("edge",))

        strobe = self._asic_uio() & 1
        if not self._rst_n:
            self._low_edges = 0
            self._prev_strobe = None
            self._tracking = False
        else:
            if strobe:
                if self._prev_strobe != 1:
                    if self._low_edges < MIN_LOW_EDGES:
                        raise ProtocolError(
                            "STROBE rises after %d clock pulse(s) with STROBE low, at least %d needed"
                            % (self._low_edges, MIN_LOW_EDGES)
                        )
                    self.low_before_rise.append(self._low_edges)
                    self._tracking = True
                    self._rise_n = 0
                self._low_edges = 0
            else:
                self._low_edges += 1
            self._prev_strobe = strobe

        ack_before = self.cyc.model.ack
        if self._tracking:
            self._rise_n += 1
        self.cyc.set_inputs(ui_in=self._asic_ui(), uio_in=self._asic_uio(), rst_n=self._rst_n)
        self.cyc.clock()
        self.edges += 1
        if self._tracking and self.cyc.model.ack != ack_before:
            self.ack_latencies.append(self._rise_n)
            self._tracking = False
        if self._hold is not None and self.cyc.model.ack != self._hold[2]:
            self._hold = None


class BrokenMock(MockTT):
    """A MockTT whose chip has one fault: kind is one of KINDS (see the module docstring)."""

    KINDS = tuple(sorted(_FAULTS))

    def __init__(self, kind, nw=32, **kwargs):
        if kind not in _FAULTS:
            raise ValueError("kind must be one of %r, got %r" % (self.KINDS, kind))
        self.kind = kind
        MockTT.__init__(self, nw=nw, model=_FAULTS[kind](nw), **kwargs)


# ---------------------------------------------------------------------------
# fake `ttboard` package, so main() can run on a computer
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def fake_ttboard(tt):
    """Make `from ttboard.demoboard import DemoBoard` / `from ttboard.mode import RPMode` work.

    DemoBoard.get() returns `tt`. The modules are removed again on exit.
    """
    names = ("ttboard", "ttboard.demoboard", "ttboard.mode")
    saved = dict((n, sys.modules.get(n)) for n in names)

    class DemoBoard(object):
        @classmethod
        def get(cls):
            tt.calls.append(("get",))
            return tt

    class RPMode(object):
        ASIC_RP_CONTROL = MockTT.RP_CONTROL
        ASIC_ON_BOARD = "ASIC_ON_BOARD"

    pkg = types.ModuleType("ttboard")
    pkg.__path__ = []
    demoboard = types.ModuleType("ttboard.demoboard")
    demoboard.DemoBoard = DemoBoard
    mode = types.ModuleType("ttboard.mode")
    mode.RPMode = RPMode
    sys.modules["ttboard"] = pkg
    sys.modules["ttboard.demoboard"] = demoboard
    sys.modules["ttboard.mode"] = mode
    try:
        yield tt
    finally:
        for n in names:
            if saved[n] is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = saved[n]
