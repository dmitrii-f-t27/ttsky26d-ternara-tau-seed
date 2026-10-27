# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""cocotb driver for tt_um_ternara_tau_seed.

The driver touches only the top-level ports: clk, rst_n, ena, ui_in, uio_in,
uo_out, uio_out, uio_oe. It never looks inside the design, so the same tests
run on RTL and on the gate-level netlist.

Timing rules (SPEC section 6.1):
    * 1 us clock. Inputs change only at a falling edge (right after it).
    * Outputs are sampled 250 ns after a falling edge, i.e. at least 250 ns
      after the last input change and before the next rising edge.
    * Every public method starts and ends positioned at a falling edge. A
      sample is always followed by a wait for the next falling edge before
      anything is written again.
    * Every read fails on X or Z (not resolvable).

Edge numbering of one command (F0 is the falling edge where STROBE is written):

    Fa    CMD, DATA, BSEL written (STROBE too if same_write)
    F0    STROBE raised                      the rising edges R1, R2, R3 follow F0
    F1    after R1  outputs unchanged
    F2    after R2  outputs unchanged
    F3    after R3  the command has executed: ACK toggled, BSEL 0 read
    F4    BSEL 1 read
    F5    STROBE lowered (later if strobe_edges > 5), BSEL back to the op's value
    then `gap` edges with STROBE low, outputs unchanged.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

import tau_config
from model import UIO_OE, TauSeedModel, pack_uio_in, to_s16

# Command codes (SPEC section 3)
NOP = 0
CLEAR = 1
LOAD_W = 2
STEP = 3
STEP_POS = 4
STEP_NEG = 5

CLOCK_PERIOD_US = 1
SETTLE_NS = 250
ACK_EDGES = 3  # the command executes on the 3rd rising edge after STROBE rises


def fail(msg):
    """Raise an AssertionError even when Python runs with -O."""
    raise AssertionError(msg)


class TauDriver(object):
    """Port-level driver with a mirrored command-level model.

    Values observed on the pins by the last full read (command() or
    read_state()) are in acc, sat, sptr0, ack, q_lo, q_hi. The tests assert
    hard-coded numbers on these; the mirrored model is compared on every sample.
    """

    def __init__(self, dut, nw=None):
        self.dut = dut
        self.nw = tau_config.NW if nw is None else nw
        self.m = TauSeedModel(self.nw)
        # what the driver currently drives
        self.rst_n = 0
        self.strobe = 0
        self.cmd = 0
        self.bsel = 0
        self.data = 0
        # last observation from the pins
        self.acc = None
        self.sat = None
        self.sptr0 = None
        self.ack = None
        self.q_lo = None
        self.q_hi = None
        self.uio_oe = None
        self.uio_out_low = None
        # bookkeeping
        self.edges = 0  # falling edges seen since start()
        self.commands = 0
        self.ack_toggles = 0
        self.ack_edge = None  # falling edge after STROBE at which ACK toggled (always 3)
        self.samples = 0

    # ------------------------------------------------------------------
    # clock, edges, drive, sample
    # ------------------------------------------------------------------
    async def start(self):
        """1 us clock, ena = 1, all inputs 0 (rst_n too). Ends at a falling edge."""
        dut = self.dut
        dut.ena.value = 1
        dut.ui_in.value = 0
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        self.rst_n = 0
        self.strobe = self.cmd = self.bsel = self.data = 0
        cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start(start_high=False))
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        self.edges = 0

    async def fall(self):
        """Wait for the next falling edge (one rising edge passes)."""
        await FallingEdge(self.dut.clk)
        self.edges += 1

    def drive(self, data=None, strobe=None, cmd=None, bsel=None, rst_n=None):
        """Write inputs. Call it right after fall() (at a falling edge) only."""
        if data is not None:
            self.data = data & 0xFF
            self.dut.ui_in.value = self.data
        if strobe is not None:
            self.strobe = strobe & 1
        if cmd is not None:
            self.cmd = cmd & 7
        if bsel is not None:
            self.bsel = bsel & 1
        if rst_n is not None:
            self.rst_n = rst_n & 1
            self.dut.rst_n.value = self.rst_n
        self.dut.uio_in.value = pack_uio_in(self.strobe, self.cmd, self.bsel)

    def _rd(self, handle, name):
        v = handle.value
        if not v.is_resolvable:
            fail("%s is not resolvable (X or Z): %s" % (name, v))
        return v.to_unsigned()

    async def sample(self):
        """Wait SETTLE_NS, read all outputs, fail on X or Z."""
        await Timer(SETTLE_NS, unit="ns")
        dut = self.dut
        q = self._rd(dut.uo_out, "uo_out")
        uio_out = self._rd(dut.uio_out, "uio_out")
        uio_oe = self._rd(dut.uio_oe, "uio_oe")
        self.samples += 1
        return {
            "q": q,
            "uio_out": uio_out,
            "uio_oe": uio_oe,
            "sat": (uio_out >> 5) & 1,
            "sptr0": (uio_out >> 6) & 1,
            "ack": (uio_out >> 7) & 1,
        }

    def compare(self, got, exp, where):
        """Compare a sample with a TauSeedModel.outputs() dict. Fails with details."""
        bad = []
        if got["q"] != exp["q"]:
            bad.append("Q 0x%02X != 0x%02X" % (got["q"], exp["q"]))
        if got["sat"] != exp["sat"]:
            bad.append("SAT %d != %d" % (got["sat"], exp["sat"]))
        if got["sptr0"] != exp["sptr0"]:
            bad.append("SPTR0 %d != %d" % (got["sptr0"], exp["sptr0"]))
        if got["ack"] != exp["ack"]:
            bad.append("ACK %d != %d" % (got["ack"], exp["ack"]))
        if got["uio_oe"] != exp["uio_oe"]:
            bad.append("uio_oe 0x%02X != 0x%02X" % (got["uio_oe"], exp["uio_oe"]))
        if (got["uio_out"] & 0x1F) != 0:
            bad.append("uio_out[4:0] = 0x%02X != 0" % (got["uio_out"] & 0x1F))
        if bad:
            fail(
                "%s (bsel=%d, edge %d): got != model: %s"
                % (where, self.bsel, self.edges, "; ".join(bad))
            )

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------
    async def reset(self, edges=5, strobe=0, idle=3):
        """rst_n low for `edges` rising edges (STROBE as given), release, `idle` edges.

        With idle = 0 the method returns at the release edge, with STROBE still at
        the given value, and does not sample. Otherwise the D1 state is checked
        after the release and after every idle edge.
        """
        self.drive(rst_n=0, strobe=strobe, cmd=0, bsel=0, data=0)
        for _ in range(edges):
            await self.fall()
        self.drive(rst_n=1)
        self.m.reset()
        for _ in range(idle):
            got = await self.sample()
            self.compare(got, self.m.outputs(self.bsel), "after reset")
            await self.fall()

    # ------------------------------------------------------------------
    # observation without a command
    # ------------------------------------------------------------------
    async def hold(self, edges, where="hold"):
        """Let `edges` rising edges pass; every output must equal the model each time.

        Returns the last sample.
        """
        got = None
        exp = self.m.outputs(self.bsel)
        for _ in range(edges):
            got = await self.sample()
            self.compare(got, exp, where)
            await self.fall()
        return got

    async def read_state(self, where="read_state"):
        """Read Q at BSEL 0 and BSEL 1 (one edge each) and the flags. Ends at a falling edge."""
        self.drive(bsel=0)
        got0 = await self.sample()
        self.compare(got0, self.m.outputs(0), where + " bsel0")
        await self.fall()
        self.drive(bsel=1)
        got1 = await self.sample()
        self.compare(got1, self.m.outputs(1), where + " bsel1")
        await self.fall()
        self._observe(got0, got1)
        return self.snapshot()

    def _observe(self, got0, got1):
        self.q_lo = got0["q"]
        self.q_hi = got1["q"]
        self.acc = to_s16((got1["q"] << 8) | got0["q"])
        self.sat = got0["sat"]
        self.sptr0 = got0["sptr0"]
        self.ack = got0["ack"]
        if (got1["sat"], got1["sptr0"], got1["ack"]) != (got0["sat"], got0["sptr0"], got0["ack"]):
            fail("flags differ between the BSEL 0 and BSEL 1 reads")
        self.uio_oe = got0["uio_oe"]
        self.uio_out_low = got0["uio_out"] & 0x1F

    def snapshot(self):
        return {
            "acc": self.acc,
            "sat": self.sat,
            "sptr0": self.sptr0,
            "ack": self.ack,
            "q_lo": self.q_lo,
            "q_hi": self.q_hi,
            "uio_oe": self.uio_oe,
            "uio_out_low": self.uio_out_low,
        }

    # ------------------------------------------------------------------
    # one command
    # ------------------------------------------------------------------
    async def command(
        self, cmd, data, gap=3, same_write=False, bsel=0, strobe_edges=5, junk=None
    ):
        """Run one command through the pins and compare with the mirrored model.

        gap          rising edges with STROBE low after the command (outputs unchanged)
        same_write   write CMD, DATA and STROBE in one write (STROBE rises at Fa = F0)
        bsel         BSEL value while the command runs and during the gap
        strobe_edges STROBE is lowered at falling edge F<strobe_edges> (minimum 5);
                     the outputs must stay unchanged while it is held high
        junk         (cmd, data) written at F3, right after ACK, while STROBE is still high
        """
        if strobe_edges < 5:
            fail("strobe_edges must be >= 5")
        cmd &= 7
        data &= 0xFF
        pre = self.m.outputs(bsel)

        # Fa: CMD, DATA, BSEL (and STROBE in the same write)
        self.drive(data=data, cmd=cmd, bsel=bsel, strobe=1 if same_write else 0)
        if not same_write:
            got = await self.sample()
            self.compare(got, pre, "cmd %d before STROBE" % cmd)
            await self.fall()
            # F0: STROBE
            self.drive(strobe=1)

        # F0, F1, F2: no output may change (the execute edge is the 3rd rising edge)
        for n in range(ACK_EDGES):
            got = await self.sample()
            self.compare(got, pre, "cmd %d wait F%d" % (cmd, n))
            await self.fall()

        # F3: the command has executed. BSEL 0 is read here; junk (if any) is
        # written while STROBE is still high.
        self.m.command(cmd, data)
        post0 = self.m.outputs(0)
        post1 = self.m.outputs(1)
        self.drive(bsel=0, **self._junk_kw(junk))
        got0 = await self.sample()
        self.compare(got0, post0, "cmd %d F3 bsel0" % cmd)
        if got0["ack"] == pre["ack"]:
            fail("cmd %d: ACK did not toggle at edge %d after STROBE" % (cmd, ACK_EDGES))
        self.ack_edge = ACK_EDGES  # ACK was unchanged at F0..F2 and toggled at F3
        self.ack_toggles += 1
        self.commands += 1
        await self.fall()

        # F4: BSEL 1
        self.drive(bsel=1)
        got1 = await self.sample()
        self.compare(got1, post1, "cmd %d F4 bsel1" % cmd)
        self._observe(got0, got1)
        await self.fall()

        # F5 .. F<strobe_edges - 1>: STROBE still high, nothing may change
        n = 5
        while n < strobe_edges:
            got = await self.sample()
            self.compare(got, post1, "cmd %d STROBE held, F%d" % (cmd, n))
            await self.fall()
            n += 1

        # F<strobe_edges>: STROBE low, BSEL back to the op's value; gap edges follow
        self.drive(strobe=0, bsel=bsel)
        post_b = self.m.outputs(bsel)
        for g in range(gap):
            got = await self.sample()
            self.compare(got, post_b, "cmd %d gap %d" % (cmd, g))
            await self.fall()
        return self.snapshot()

    @staticmethod
    def _junk_kw(junk):
        if junk is None:
            return {}
        return {"cmd": junk[0], "data": junk[1]}

    # convenience wrappers ------------------------------------------------
    async def nop(self, data=0, **kw):
        return await self.command(NOP, data, **kw)

    async def clear(self, **kw):
        return await self.command(CLEAR, 0, **kw)

    async def load_w(self, byte, **kw):
        return await self.command(LOAD_W, byte, **kw)

    async def step(self, x, **kw):
        return await self.command(STEP, x, **kw)

    async def step_pos(self, x, **kw):
        return await self.command(STEP_POS, x, **kw)

    async def step_neg(self, x, **kw):
        return await self.command(STEP_NEG, x, **kw)

    async def repeat(self, n, cmd, data, **kw):
        snap = None
        for _ in range(n):
            snap = await self.command(cmd, data, **kw)
        return snap

    # ------------------------------------------------------------------
    # hard-coded value checks on the last pin observation
    # ------------------------------------------------------------------
    def expect(self, where, acc=None, sat=None, sptr0=None, ack=None, q_lo=None, q_hi=None):
        """Assert numbers written in the test against what the pins showed."""
        bad = []
        for name, want in (
            ("acc", acc),
            ("sat", sat),
            ("sptr0", sptr0),
            ("ack", ack),
            ("q_lo", q_lo),
            ("q_hi", q_hi),
        ):
            if want is not None and getattr(self, name) != want:
                bad.append("%s = %r, expected %r" % (name, getattr(self, name), want))
        if bad:
            fail("%s: %s" % (where, "; ".join(bad)))
        if self.uio_oe != UIO_OE or self.uio_out_low != 0:
            fail(
                "%s: uio_oe = 0x%02X (expected 0xE0), uio_out[4:0] = 0x%02X (expected 0)"
                % (where, self.uio_oe, self.uio_out_low)
            )
