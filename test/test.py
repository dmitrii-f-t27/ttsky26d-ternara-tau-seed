# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""cocotb tests for tt_um_ternara_tau_seed.

The directed tests below use the vectors of docs/info.md, How to test. Every test:

* drives only the top-level ports (see tau_driver.py),
* compares every sample with the mirrored command-level model (model.py),
* and asserts the numbers written here from the datasheet.

Numbers are given for NW = 32 and, where they depend on NW, as formulas. The
same tests run on RTL and on the gate-level netlist (GATES=yes).

The random tests (docs/info.md, How to test) replay the seeded operation streams of
randgen.py. RTL: 4 seeds x 5000 operations. Gate level: seeds A and B, the
first 1000 operations (a prefix of the RTL stream). test/expected_tests.json
lists the names that must show up in results.xml; test/tau_guard.py checks it.
"""

import os

import cocotb

import randgen
import tau_config
import vectors
from model import TauSeedModel
from tau_driver import (
    LOAD_W,
    NOP,
    STEP,
    STEP_NEG,
    STEP_POS,
    TauDriver,
    fail,
)

GATES = os.environ.get("GATES") == "yes"
NW = tau_config.NW
N4 = NW // 4  # LOAD_W bytes needed to fill the weight memory

# Test timeout: simulation time, protection against a deadlock only.
# One operation takes at most 11 clock periods of 1 us, so 5000 operations
# need at most 55 ms of simulation time.
TIMEOUT_MS = 200

# Random tests: operations per run
N_OPS_RTL = 5000
N_OPS_GL = 1000


def pick(nw32, formula):
    """The number written in the datasheet for NW = 32, else the NW formula."""
    return nw32 if NW == 32 else formula


async def new_driver(dut, name):
    """Start the clock and reset (rst_n low 5 edges, STROBE low, 3 idle edges)."""
    dut._log.info("%s: %s, NW = %d", name, "gate level" if GATES else "RTL", NW)
    drv = TauDriver(dut)
    await drv.start()
    await drv.reset()
    return drv


async def run_neuron_stored(drv, load_bytes):
    """D18 (a)/(b): load the weights and step through all 32 activations.

    With NW = 32 this is 8 LOAD_W then 32 STEP. With NW = 16 it runs as two
    halves with a reload; the pointers wrap to 0 after each half.
    """
    for half in range(32 // NW):
        for b in load_bytes[half * N4:(half + 1) * N4]:
            await drv.load_w(b)
        for x in vectors.D18_X[half * NW:(half + 1) * NW]:
            await drv.step(x)


# ---------------------------------------------------------------------------
# D1, D2: reset
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d01_reset_state(dut):
    drv = await new_driver(dut, "d01")
    await drv.read_state("d01")
    drv.expect("d01 reset state", acc=0, sat=0, sptr0=1, ack=0, q_lo=0x00, q_hi=0x00)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d02_strobe_high_through_reset(dut):
    dut._log.info("d02: STROBE high through reset and after release, NW = %d", NW)
    drv = TauDriver(dut)
    await drv.start()
    # STROBE = 1 during the 5 reset edges and still 1 after the release
    await drv.reset(edges=5, strobe=1, idle=0)
    # 10 edges after release with STROBE = 1: no ACK, state as D1 (BSEL 0, then BSEL 1)
    got = await drv.hold(5, "d02 STROBE high after release, BSEL 0")
    drv.drive(bsel=1)
    got = await drv.hold(5, "d02 STROBE high after release, BSEL 1")
    assert got["ack"] == 0, "ACK toggled while STROBE was high through reset"
    assert got["q"] == 0x00 and got["sat"] == 0 and got["sptr0"] == 1
    # STROBE low for exactly 3 edges, then a NOP in one write with STROBE
    drv.drive(strobe=0, bsel=0)
    got = await drv.hold(3, "d02 STROBE low")
    assert got["ack"] == 0
    await drv.nop(0x00, same_write=True)
    drv.expect("d02 after NOP", acc=0, sat=0, sptr0=1, ack=1)
    assert drv.ack_toggles == 1


# ---------------------------------------------------------------------------
# D3 .. D6: stored weights
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d03_zero_weights(dut):
    drv = await new_driver(dut, "d03")
    await drv.repeat(N4, LOAD_W, 0x55)  # all +1 first, so zero weights must really be written
    await drv.repeat(N4, LOAD_W, 0x00)
    await drv.step(127)
    drv.expect("d03 after 1 step", acc=0, sat=0, sptr0=0 if NW > 0 else 1)
    await drv.repeat(NW - 1, STEP, 127)
    drv.expect("d03 after NW steps", acc=0, sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d04_all_plus(dut):
    drv = await new_driver(dut, "d04")
    await drv.repeat(N4, LOAD_W, 0x55)
    await drv.repeat(NW, STEP, 127)
    want = pick(4064, 127 * NW)
    drv.expect("d04", acc=want, sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d05_all_minus(dut):
    drv = await new_driver(dut, "d05")
    await drv.repeat(N4, LOAD_W, 0xFF)
    await drv.repeat(NW, STEP, -128)
    want = pick(4096, 128 * NW)
    drv.expect("d05", acc=want, sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d06_reserved_weight_code(dut):
    drv = await new_driver(dut, "d06")
    await drv.repeat(10, STEP_POS, 100)
    drv.expect("d06 preload", acc=1000, sat=0)
    await drv.repeat(N4, LOAD_W, 0xAA)  # weight code 10 everywhere: reserved, acts as 0
    await drv.repeat(NW, STEP, -77)
    drv.expect("d06", acc=1000, sat=0, sptr0=1)


# ---------------------------------------------------------------------------
# D7 .. D10: saturation
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d07_d08_saturate_high(dut):
    drv = await new_driver(dut, "d07_d08")
    await drv.repeat(255, STEP_NEG, -128)
    drv.expect("d07 exact", acc=32640, sat=0, q_lo=0x80, q_hi=0x7F)
    await drv.step_neg(-128)
    drv.expect("d08 saturated", acc=32767, sat=1, q_lo=0xFF, q_hi=0x7F)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d09_d10_saturate_low(dut):
    drv = await new_driver(dut, "d09_d10")
    await drv.repeat(256, STEP_POS, -128)
    drv.expect("d09 exact limit", acc=-32768, sat=0, q_lo=0x00, q_hi=0x80)
    await drv.step_pos(-128)
    drv.expect("d10 saturated", acc=-32768, sat=1, q_lo=0x00, q_hi=0x80)


# ---------------------------------------------------------------------------
# D11 .. D13: pointers
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d11_sptr_wrap(dut):
    drv = await new_driver(dut, "d11")
    await drv.load_w(0x01)  # weight 0 = +1, weights 1..3 = 0
    await drv.repeat(N4 - 1, LOAD_W, 0x00)
    await drv.repeat(NW, STEP, 100)
    drv.expect("d11 one pass", acc=pick(100, 100 if NW > 0 else 0), sat=0, sptr0=1)
    await drv.step(100)
    drv.expect(
        "d11 extra step",
        acc=pick(200, 200 if NW > 0 else 0),
        sat=0,
        sptr0=0 if NW > 0 else 1,
    )


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d12_lptr_wrap(dut):
    drv = await new_driver(dut, "d12")
    await drv.repeat(N4, LOAD_W, 0x00)  # lptr wraps back to 0
    await drv.load_w(0x55)  # must land in slot 0
    await drv.repeat(NW, STEP, 10)
    drv.expect("d12", acc=pick(40, 40 if NW > 0 else 0), sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d13_clear_mid_pass(dut):
    drv = await new_driver(dut, "d13")
    for b in vectors.D18_BYTES[:N4]:
        await drv.load_w(b)
    await drv.load_w(0xD3)  # lptr -> 1
    for x in vectors.D18_X[:7]:
        await drv.step(x)
    await drv.clear()
    drv.expect("d13 after CLEAR", acc=0, sat=0, sptr0=1)
    await drv.load_w(0x55)  # lands in slot 0 only if CLEAR reset lptr; weights 4.. are kept
    for x in vectors.D18_X[:NW]:
        await drv.step(x)
    # first four weights +1, the others as D18 (CLEAR keeps the weights)
    by_hand = sum(vectors.D18_X[:min(4, NW)])
    by_hand += sum(vectors.D18_W[i] * vectors.D18_X[i] for i in range(4, NW))
    want = pick(1687, by_hand)
    if NW == 32 and by_hand != 1687:
        fail("d13: hand sum %d != 1687" % by_hand)
    drv.expect("d13 end", acc=want, sat=0, sptr0=1)


# ---------------------------------------------------------------------------
# D14, D15, D16: handshake corners, byte select, NOP and reserved codes
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d14_strobe_held_high(dut):
    drv = await new_driver(dut, "d14")
    await drv.step_pos(5, strobe_edges=20)  # STROBE high for 20 edges
    drv.expect("d14", acc=5, sat=0, ack=1)
    assert drv.ack_toggles == 1, "more than one ACK toggle for one STROBE pulse"


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d15_byte_select(dut):
    drv = await new_driver(dut, "d15")
    await drv.repeat(9, STEP_POS, -128)
    drv.expect("d15 preload", acc=-1152, sat=0)
    await drv.step_pos(-82)
    # -1234 = 0xFB2E: BSEL 0 reads 0x2E, BSEL 1 reads 0xFB
    drv.expect("d15", acc=-1234, sat=0, q_lo=0x2E, q_hi=0xFB)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d16_nop_and_reserved(dut):
    drv = await new_driver(dut, "d16")
    await drv.repeat(3, STEP_POS, 100)
    drv.expect("d16 preload", acc=300, sat=0, sptr0=1, ack=1)
    toggles0 = drv.ack_toggles
    for code, ack_after in ((NOP, 0), (6, 1), (7, 0)):
        await drv.command(code, 0xFF)
        drv.expect("d16 code %d" % code, acc=300, sat=0, sptr0=1, ack=ack_after)
    assert drv.ack_toggles - toggles0 == 3, "ACK must toggle once for each of codes 0, 6, 7"


# ---------------------------------------------------------------------------
# D17, D18: reset in the middle of a pass; the neuron
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d17_reset_mid_pass(dut):
    drv = await new_driver(dut, "d17")
    for b in vectors.D18_BYTES[:N4]:
        await drv.load_w(b)
    await drv.repeat(5, STEP, 100)
    # w[0..4] = -1, 0, +1, -1, 0
    drv.expect("d17 before reset", acc=-100 if NW > 0 else 0, sptr0=0 if NW > 0 else 1)
    await drv.reset(edges=3)  # rst_n low for 3 edges, release, 3 idle edges
    await drv.read_state("d17 after reset")
    drv.expect("d17 D1 state", acc=0, sat=0, sptr0=1, ack=0, q_lo=0x00, q_hi=0x00)
    await drv.repeat(NW, STEP, 100)  # all weights are 00 again
    drv.expect("d17 end", acc=0, sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d18_neuron(dut):
    drv = await new_driver(dut, "d18")
    assert vectors.D18_EXPECT == 2099 and vectors.D18_NEG_EXPECT == -2099
    if NW > 0:
        # (a) stored weights
        await run_neuron_stored(drv, vectors.D18_BYTES)
        drv.expect("d18 (a)", acc=2099, sat=0, q_lo=0x33, q_hi=0x08)
        # (b) the negated weights
        await drv.clear()
        drv.expect("d18 CLEAR", acc=0, sat=0, sptr0=1)
        await run_neuron_stored(drv, vectors.D18_NEG_BYTES)
        drv.expect("d18 (b)", acc=-2099, sat=0, q_lo=0xCD, q_hi=0xF7)
    # (c) streamed weights: +1 -> STEP_POS x, -1 -> STEP_NEG x, 0 -> no command
    await drv.clear()
    drv.expect("d18 CLEAR before (c)", acc=0, sat=0, sptr0=1)
    for w, x in zip(vectors.D18_W, vectors.D18_X):
        if w == 1:
            await drv.step_pos(x)
        elif w == -1:
            await drv.step_neg(x)
    drv.expect("d18 (c)", acc=2099, sat=0, q_lo=0x33, q_hi=0x08)


# ---------------------------------------------------------------------------
# D19, D20, D21: protocol and saturation corners
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d19_inputs_after_ack_ignored(dut):
    drv = await new_driver(dut, "d19")
    # after ACK: STROBE stays high, CMD = 5 (STEP_NEG) and DATA = 0x80 for 10 edges
    await drv.step_pos(10, junk=(STEP_NEG, 0x80), strobe_edges=13)
    drv.expect("d19 first", acc=10, sat=0, ack=1)
    assert drv.ack_toggles == 1, "ACK toggled again while STROBE was held high"
    await drv.step_neg(-128)
    drv.expect("d19 second", acc=138, sat=0, ack=0)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d20_handshake_timing(dut):
    drv = await new_driver(dut, "d20")
    # reset() ends exactly 3 rising edges after the release. The first command
    # is written with STROBE in the same write, so it rises exactly 3 edges after
    # release; later commands follow a gap of exactly 3 edges.
    for i in range(10):
        same = (i % 2 == 0)  # commands 0, 2, 4, 6, 8: CMD and STROBE in one write
        await drv.step_pos(1, gap=3, same_write=same)
        assert drv.ack_edge == 3, "ACK not at exactly the 3rd edge"
        drv.expect("d20 command %d" % i, acc=i + 1, sat=0, ack=(i + 1) & 1)
    drv.expect("d20 end", acc=10, sat=0, sptr0=1)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_d21_saturation_recovery(dut):
    drv = await new_driver(dut, "d21")
    await drv.repeat(255, STEP_NEG, -128)
    drv.expect("d21 start", acc=32640, sat=0)
    steps_high = (
        (STEP_POS, 127, 32767, 0),   # exact limit, no SAT
        (STEP_POS, 1, 32767, 1),     # overflow: SAT set
        (STEP_POS, -1, 32766, 1),    # SAT is sticky
        (STEP_NEG, -1, 32767, 1),
        (STEP_POS, 0, 32767, 1),
        (STEP_NEG, 127, 32640, 1),
    )
    for code, x, acc, sat in steps_high:
        await drv.command(code, x)
        drv.expect("d21 high %d/%d" % (code, x), acc=acc, sat=sat)
    await drv.clear()
    drv.expect("d21 CLEAR", acc=0, sat=0)
    await drv.repeat(255, STEP_POS, -128)
    drv.expect("d21 low start", acc=-32640, sat=0)
    steps_low = (
        (STEP_NEG, 127, -32767, 0),
        (STEP_NEG, 1, -32768, 0),    # exact limit, no SAT
        (STEP_NEG, 1, -32768, 1),    # overflow: SAT set
        (STEP_POS, 1, -32767, 1),
        (STEP_POS, -128, -32768, 1),
    )
    for code, x, acc, sat in steps_low:
        await drv.command(code, x)
        drv.expect("d21 low %d/%d" % (code, x), acc=acc, sat=sat)


# ---------------------------------------------------------------------------
# NW self-check
# ---------------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_nw_probe(dut):
    drv = await new_driver(dut, "nw_probe")
    await drv.read_state("nw_probe")
    drv.expect("nw_probe start", acc=0, sat=0, sptr0=1)
    count = None
    left = False
    for i in range(1, 41):
        await drv.step(0)
        if drv.sptr0 == 0:
            left = True
        elif not left:
            count = 0  # SPTR0 never left 1: no stored weights
            break
        else:
            count = i  # SPTR0 is back at 1 after i steps
            break
    if count is None:
        fail("nw_probe: SPTR0 did not return to 1 within 40 steps")
    dut._log.info("nw_probe: measured NW = %d, tau_config.NW = %d", count, tau_config.NW)
    assert count == tau_config.NW, "probe says NW = %d, tau_config.NW = %d" % (count, tau_config.NW)


# ---------------------------------------------------------------------------
# Random tests (docs/info.md, How to test)
# ---------------------------------------------------------------------------

async def run_random(dut, name, seed):
    """Replay gen_ops(seed, n, NW) through the pins.

    Every CMD operation goes through TauDriver.command(), which compares Q at
    both BSEL values, SAT, SPTR0, ACK, uio_oe and uio_out[4:0] with the
    mirrored model after the command, and checks that nothing moves while the
    design waits and during the STROBE-low gap. A RESET operation resets the
    design (rst_n low for 5 edges, STROBE low, 3 idle edges).
    """
    mode = "gl" if GATES else "rtl"
    n = N_OPS_GL if GATES else N_OPS_RTL
    ops = randgen.gen_ops(seed, n, NW)
    assert len(ops) == n, "generator returned %d operations, expected %d" % (len(ops), n)
    if GATES:
        # the gate-level run is a prefix of the RTL run for the same seed
        assert ops == randgen.gen_ops(seed, N_OPS_RTL, NW)[:N_OPS_GL], (
            "gate-level operations are not a prefix of the RTL operations"
        )

    # Coverage is counted on the model; every minimum in randgen.COVERAGE_MIN must hold.
    cov = randgen.coverage(ops, NW)
    dut._log.info("%s coverage (%s, NW = %d): %s", name, mode, NW, cov)
    unmet = randgen.coverage_failures(cov, mode, NW)
    assert not unmet, "%s: coverage minimums not met: %s" % (name, "; ".join(unmet))

    drv = await new_driver(dut, name)
    cmds = 0
    resets = 0
    hi_seen = 0
    lo_seen = 0
    sat_seen = 0
    for op in ops:
        if op[0] == "RESET":
            await drv.reset()
            resets += 1
            continue
        _, code, data, bsel, gap, same = op
        snap = await drv.command(code, data, gap=gap, same_write=bool(same), bsel=bsel)
        cmds += 1
        if snap["acc"] == 32767:
            hi_seen += 1
        elif snap["acc"] == -32768:
            lo_seen += 1
        sat_seen += snap["sat"]

    # number of executed operations
    assert cmds + resets == n, "executed %d operations, expected %d" % (cmds + resets, n)
    assert cmds == cov["cmds"] and resets == cov["resets"], (
        "executed %d commands and %d resets, generator counted %d and %d"
        % (cmds, resets, cov["cmds"], cov["resets"])
    )
    assert drv.commands == cmds, "driver counted %d commands, test sent %d" % (drv.commands, cmds)
    # the pins really reached both limits and SAT was seen
    assert hi_seen >= 1 and lo_seen >= 1, (
        "the pins never showed +32767 (%d) or -32768 (%d)" % (hi_seen, lo_seen)
    )
    assert sat_seen >= 1, "SAT was never seen on the pins"
    dut._log.info(
        "%s: %d commands, %d resets, pins showed +32767 %d times, -32768 %d times, "
        "SAT in %d samples",
        name, cmds, resets, hi_seen, lo_seen, sat_seen,
    )

    # final state: pins against an independent replay of the same operations
    ref = TauSeedModel(NW)
    for op in ops:
        if op[0] == "RESET":
            ref.reset()
        else:
            ref.command(op[1], op[2])
    await drv.read_state(name + " end")
    drv.expect(
        name + " end",
        acc=ref.acc,
        sat=ref.sat,
        sptr0=ref.outputs(0)["sptr0"],
        ack=ref.ack,
    )


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_random_seed_a(dut):
    await run_random(dut, "random_a", randgen.SEED_A)


@cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
async def test_random_seed_b(dut):
    await run_random(dut, "random_b", randgen.SEED_B)


# Seeds C and D run on RTL only. They are defined (not skipped) only when GATES
# is not "yes", so the gate-level results file holds exactly 22 tests.
if not GATES:

    @cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
    async def test_random_seed_c(dut):
        await run_random(dut, "random_c", randgen.SEED_C)

    @cocotb.test(timeout_time=TIMEOUT_MS, timeout_unit="ms")
    async def test_random_seed_d(dut):
        await run_random(dut, "random_d", randgen.SEED_D)
