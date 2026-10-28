# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""pytest checks for model.py, vectors.py and randgen.py.

Expected numbers are written out here from the datasheet (docs/info.md) and
are not computed by the model under test.
Run:  cd <repo> && python -B -m pytest -q test/test_model.py
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

import randgen  # noqa: E402
import tau_config  # noqa: E402
import vectors  # noqa: E402
from model import TauSeedCycle, TauSeedModel, dec, mac, pack_uio_in, to_s8  # noqa: E402

# Command codes, written out from docs/info.md, Commands (not imported from model.py).
NOP, CLEAR, LOAD_W, STEP, STEP_POS, STEP_NEG = 0, 1, 2, 3, 4, 5

D18_BYTES_SPEC = [0xD3, 0x34, 0x4D, 0xD3, 0x34, 0x4D, 0xD3, 0x34]
D18_NEG_BYTES_SPEC = [0x71, 0x1C, 0xC7, 0x71, 0x1C, 0xC7, 0x71, 0x1C]
D18_X_HEAD_SPEC = [-105, 0, 105, -101, 4]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def send(m, code, data, n=1):
    for _ in range(n):
        m.command(code, data & 0xFF)


def view(m):
    """Read the model through its pins; returns (acc, sat, sptr0, ack, q_lo, q_hi)."""
    o0 = m.outputs(0)
    o1 = m.outputs(1)
    assert o0["sat"] == o1["sat"]
    assert o0["sptr0"] == o1["sptr0"]
    assert o0["ack"] == o1["ack"]
    assert o0["uio_oe"] == 0xE0 and o1["uio_oe"] == 0xE0
    assert o0["uio_out"] & 0x1F == 0
    assert o0["uio_out"] >> 5 == (o0["ack"] << 2) | (o0["sptr0"] << 1) | o0["sat"]
    v = (o1["q"] << 8) | o0["q"]
    acc = v - 65536 if v >= 32768 else v
    return acc, o0["sat"], o0["sptr0"], o0["ack"], o0["q"], o1["q"]


def d13_expect_by_hand(nw):
    """D13 end value from plain arithmetic: first four weights +1, the rest as D18."""
    x = vectors.D18_X
    w = vectors.D18_W
    return sum(x[i] for i in range(4)) + sum(w[i] * x[i] for i in range(4, nw))


class Host(object):
    """Drives TauSeedCycle like the testbench: inputs change between edges."""

    def __init__(self, nw):
        self.c = TauSeedCycle(nw)

    def pins(self):
        c = self.c
        return (c.uo_out(), c.uio_out(), c.uio_oe())

    def reset(self, edges=5, idle=3):
        c = self.c
        c.set_inputs(ui_in=0, uio_in=pack_uio_in(0), rst_n=0)
        for _ in range(edges):
            c.clock()
        c.set_inputs(rst_n=1)
        for _ in range(idle):
            c.clock()

    def command(self, cmd, data, gap=3, same_write=False, bsel=0):
        """One command. ACK must stay put for 2 edges and toggle at exactly the 3rd."""
        c = self.c
        ack0 = c.uio_out() >> 7
        if same_write:
            c.set_inputs(ui_in=data, uio_in=pack_uio_in(1, cmd, bsel))
        else:
            c.set_inputs(ui_in=data, uio_in=pack_uio_in(0, cmd, bsel))
            c.clock()
            c.set_inputs(uio_in=pack_uio_in(1, cmd, bsel))
        before = (c.model.acc, c.model.sat, c.model.sptr)
        for edge in (1, 2, 3):
            c.clock()
            ack = c.uio_out() >> 7
            if edge < 3:
                assert ack == ack0
                assert (c.model.acc, c.model.sat, c.model.sptr) == before
            else:
                assert ack == ack0 ^ 1
        c.set_inputs(uio_in=pack_uio_in(0, cmd, bsel))
        after = self.pins()
        for _ in range(gap):
            c.clock()
            assert self.pins() == after

    def hold(self, edges):
        for _ in range(edges):
            self.c.clock()

    def view(self):
        return view(self.c.model)


# --------------------------------------------------------------------------
# arithmetic
# --------------------------------------------------------------------------

def test_dec_table():
    assert [dec(c) for c in (0, 1, 2, 3)] == [0, 1, 0, -1]
    assert dec(0b101) == 1  # only two bits are looked at


def test_to_s8():
    assert [to_s8(b) for b in (0, 1, 127, 128, 129, 255, 256, 0x1FF)] == [0, 1, 127, -128, -127, -1, 0, -1]


def test_mac_windows():
    windows = (
        list(range(-32768, -32000 + 1)),
        list(range(-300, 300 + 1)),
        list(range(32000, 32767 + 1)),
    )
    n = 0
    for win in windows:
        for a in win:
            for w in (-1, 0, 1):
                for x in range(-128, 128):
                    s = a + w * x
                    want = max(-32768, min(32767, s))
                    want_ovf = not (-32768 <= s <= 32767)
                    got = mac(a, w, x)
                    if got != (want, want_ovf):
                        pytest.fail("mac(%d,%d,%d) = %r, expected %r" % (a, w, x, got, (want, want_ovf)))
                    n += 1
    assert n == (769 + 601 + 768) * 3 * 256


# --------------------------------------------------------------------------
# D18 data
# --------------------------------------------------------------------------

def test_d18_bytes():
    assert vectors.D18_BYTES == D18_BYTES_SPEC
    assert vectors.D18_NEG_BYTES == D18_NEG_BYTES_SPEC
    assert vectors.D18_X[:5] == D18_X_HEAD_SPEC
    assert vectors.D18_EXPECT == 2099
    assert vectors.D18_NEG_EXPECT == -2099
    assert 2099 == 0x0833
    assert -2099 & 0xFFFF == 0xF7CD
    assert len(vectors.D18_W) == 32 and len(vectors.D18_X) == 32
    # independent re-derivation of the formulas
    code = {-1: 3, 0: 0, 1: 1}
    total = 0
    for i in range(32):
        w = (i % 3) - 1
        x = 100 * w + ((5 * i) % 11) - 5
        assert vectors.D18_W[i] == w and vectors.D18_X[i] == x
        total += w * x
        byte = vectors.D18_BYTES[i // 4]
        assert (byte >> (2 * (i % 4))) & 3 == code[w]
        nbyte = vectors.D18_NEG_BYTES[i // 4]
        assert (nbyte >> (2 * (i % 4))) & 3 == code[-w]
    assert total == 2099


# --------------------------------------------------------------------------
# directed table, command level
# --------------------------------------------------------------------------

def run_dtable(nw, e):
    """Directed vectors D1, D3..D13, D15..D18, D21 for one NW. e holds the NW-dependent numbers."""
    q = nw // 4

    # D1
    m = TauSeedModel(nw)
    assert view(m) == (0, 0, 1, 0, 0x00, 0x00)

    # D3
    m = TauSeedModel(nw)
    send(m, LOAD_W, 0x55, q)
    send(m, LOAD_W, 0x00, q)
    send(m, STEP, 127)
    assert view(m)[:3] == (0, 0, 0)
    send(m, STEP, 127, nw - 1)
    assert view(m)[:3] == (0, 0, 1)

    # D4
    m = TauSeedModel(nw)
    send(m, LOAD_W, 0x55, q)
    send(m, STEP, 127, nw)
    assert view(m)[:3] == (e["d04"], 0, 1)
    assert view(m)[4:] == e["d04_q"]

    # D5
    m = TauSeedModel(nw)
    send(m, LOAD_W, 0xFF, q)
    send(m, STEP, -128, nw)
    assert view(m)[:3] == (e["d05"], 0, 1)
    assert view(m)[4:] == e["d05_q"]

    # D6
    m = TauSeedModel(nw)
    send(m, STEP_POS, 100, 10)
    send(m, LOAD_W, 0xAA, q)
    send(m, STEP, -77, nw)
    assert view(m)[:3] == (1000, 0, 1)

    # D7 / D8
    m = TauSeedModel(nw)
    send(m, STEP_NEG, -128, 255)
    assert view(m)[:3] == (32640, 0, 1)
    assert view(m)[4:] == (0x80, 0x7F)
    send(m, STEP_NEG, -128)
    assert view(m)[:3] == (32767, 1, 1)
    assert view(m)[4:] == (0xFF, 0x7F)

    # D9 / D10
    m = TauSeedModel(nw)
    send(m, STEP_POS, -128, 256)
    assert view(m)[:3] == (-32768, 0, 1)
    assert view(m)[4:] == (0x00, 0x80)
    send(m, STEP_POS, -128)
    assert view(m)[:3] == (-32768, 1, 1)
    assert view(m)[4:] == (0x00, 0x80)

    # D11
    m = TauSeedModel(nw)
    send(m, LOAD_W, 0x01)
    send(m, LOAD_W, 0x00, q - 1)
    send(m, STEP, 100, nw)
    assert view(m)[:3] == (100, 0, 1)
    send(m, STEP, 100)
    assert view(m)[:3] == (200, 0, 0)

    # D12
    m = TauSeedModel(nw)
    send(m, LOAD_W, 0x00, q)
    send(m, LOAD_W, 0x55)
    send(m, STEP, 10, nw)
    assert view(m)[:3] == (40, 0, 1)

    # D13
    m = TauSeedModel(nw)
    for b in vectors.D18_BYTES[:q]:
        send(m, LOAD_W, b)
    send(m, LOAD_W, 0xD3)
    for i in range(7):
        send(m, STEP, vectors.D18_X[i])
    send(m, CLEAR, 0)
    assert view(m)[:3] == (0, 0, 1)
    send(m, LOAD_W, 0x55)
    for i in range(nw):
        send(m, STEP, vectors.D18_X[i])
    assert view(m)[:3] == (e["d13"], 0, 1)

    # D15
    m = TauSeedModel(nw)
    send(m, STEP_POS, -128, 9)
    send(m, STEP_POS, -82)
    assert view(m)[:3] == (-1234, 0, 1)
    assert view(m)[4:] == (0x2E, 0xFB)
    assert -1234 & 0xFFFF == 0xFB2E

    # D16
    m = TauSeedModel(nw)
    send(m, STEP_POS, 100, 3)
    assert view(m) == (300, 0, 1, 1, 300 & 0xFF, 300 >> 8)
    acks = []
    for code in (0, 6, 7):
        send(m, code, 0xFF)
        v = view(m)
        assert v[:3] == (300, 0, 1)
        acks.append(v[3])
    assert acks == [0, 1, 0]  # started at 1: toggles three times

    # D17
    m = TauSeedModel(nw)
    for b in vectors.D18_BYTES[:q]:
        send(m, LOAD_W, b)
    send(m, STEP, 100, 5)
    m.reset()
    assert view(m) == (0, 0, 1, 0, 0x00, 0x00)
    send(m, STEP, 100, nw)
    assert view(m)[:3] == (0, 0, 1)

    # D18
    m = TauSeedModel(nw)

    def stored(byte_list, xs):
        if nw == 32:
            for b in byte_list:
                send(m, LOAD_W, b)
            for x in xs:
                send(m, STEP, x)
        else:  # nw == 16: two halves with a reload
            for half in (0, 1):
                for b in byte_list[4 * half:4 * half + 4]:
                    send(m, LOAD_W, b)
                for x in xs[16 * half:16 * half + 16]:
                    send(m, STEP, x)

    stored(D18_BYTES_SPEC, vectors.D18_X)
    assert view(m)[:3] == (2099, 0, 1)
    assert view(m)[4:] == (0x33, 0x08)
    send(m, CLEAR, 0)
    stored(D18_NEG_BYTES_SPEC, vectors.D18_X)
    assert view(m)[:3] == (-2099, 0, 1)
    assert view(m)[4:] == (0xCD, 0xF7)
    send(m, CLEAR, 0)
    for i in range(32):
        w = (i % 3) - 1
        if w == 1:
            send(m, STEP_POS, vectors.D18_X[i])
        elif w == -1:
            send(m, STEP_NEG, vectors.D18_X[i])
    assert view(m)[:3] == (2099, 0, 1)

    # D21: saturation recovery
    m = TauSeedModel(nw)
    send(m, STEP_NEG, -128, 255)
    assert view(m)[:2] == (32640, 0)
    trace_hi = [
        (STEP_POS, 127, 32767, 0), (STEP_POS, 1, 32767, 1), (STEP_POS, -1, 32766, 1),
        (STEP_NEG, -1, 32767, 1), (STEP_POS, 0, 32767, 1), (STEP_NEG, 127, 32640, 1),
    ]
    for code, x, acc, sat in trace_hi:
        send(m, code, x)
        assert view(m)[:2] == (acc, sat), (code, x)
    send(m, CLEAR, 0)
    assert view(m)[:2] == (0, 0)
    send(m, STEP_POS, -128, 255)
    assert view(m)[:2] == (-32640, 0)
    trace_lo = [
        (STEP_NEG, 127, -32767, 0), (STEP_NEG, 1, -32768, 0), (STEP_NEG, 1, -32768, 1),
        (STEP_POS, 1, -32767, 1), (STEP_POS, -128, -32768, 1),
    ]
    for code, x, acc, sat in trace_lo:
        send(m, code, x)
        assert view(m)[:2] == (acc, sat), (code, x)


def test_dtable_nw32():
    # D4 4064 = 0x0FE0, D5 4096 = 0x1000, D13 1687
    run_dtable(32, {
        "d04": 4064, "d04_q": (0xE0, 0x0F),
        "d05": 4096, "d05_q": (0x00, 0x10),
        "d13": 1687,
    })
    assert 127 * 32 == 4064 and 128 * 32 == 4096


def test_dtable_nw16():
    # D4 127*16 = 2032 = 0x07F0, D5 128*16 = 2048 = 0x0800
    run_dtable(16, {
        "d04": 2032, "d04_q": (0xF0, 0x07),
        "d05": 2048, "d05_q": (0x00, 0x08),
        "d13": d13_expect_by_hand(16),
    })
    assert d13_expect_by_hand(32) == 1687


def test_d13_lptr_kept_would_give_1906():
    """Cross-check for mutant M16 (CLEAR keeps lptr): the end value changes to 1906."""
    m = TauSeedModel(32)
    for b in D18_BYTES_SPEC:
        send(m, LOAD_W, b)
    send(m, LOAD_W, 0xD3)
    for i in range(7):
        send(m, STEP, vectors.D18_X[i])
    lptr = m.lptr
    send(m, CLEAR, 0)
    m.lptr = lptr  # emulate the mutant
    send(m, LOAD_W, 0x55)
    for i in range(32):
        send(m, STEP, vectors.D18_X[i])
    assert view(m)[0] == 1906


def test_nw0():
    m = TauSeedModel(0)
    assert m.codes == []
    assert view(m) == (0, 0, 1, 0, 0, 0)
    # STEP and LOAD_W are NOP, ACK still toggles, SPTR0 stays 1
    for i, code in enumerate((LOAD_W, STEP, LOAD_W, STEP, STEP)):
        send(m, code, 0x55)
        v = view(m)
        assert v[:3] == (0, 0, 1)
        assert v[3] == (i + 1) % 2
    # streamed weights still work, CLEAR still works
    send(m, STEP_POS, 100, 3)
    send(m, STEP_NEG, 50)
    assert view(m)[:3] == (250, 0, 1)
    send(m, STEP, 77)
    assert view(m)[:3] == (250, 0, 1)
    send(m, CLEAR, 0)
    assert view(m)[:3] == (0, 0, 1)
    # saturation works at NW = 0 as well
    send(m, STEP_NEG, -128, 256)
    assert view(m)[:3] == (32767, 1, 1)


def test_invalid_nw():
    for nw in (-4, 1, 4, 8, 24, 33, 64):
        with pytest.raises(ValueError):
            TauSeedModel(nw)
        with pytest.raises(ValueError):
            TauSeedCycle(nw)


def test_nw_probe():
    def probe(nw):
        m = TauSeedModel(nw)
        send(m, STEP, 0)
        count = 1
        if view(m)[2] == 1:
            return 0  # SPTR0 never left 1
        while count < 40:
            send(m, STEP, 0)
            count += 1
            if view(m)[2] == 1:
                return count
        return None

    assert probe(32) == 32
    assert probe(16) == 16
    assert probe(0) == 0
    assert tau_config.NW == 32


# --------------------------------------------------------------------------
# cycle model: protocol behaviour and the timing vectors D2, D14, D19, D20
# --------------------------------------------------------------------------

def test_cycle_ack_latency_3():
    h = Host(32)
    h.reset()
    assert h.view() == (0, 0, 1, 0, 0, 0)
    h.command(STEP_POS, 10)  # asserts ACK steady for 2 edges, toggled at the 3rd
    assert h.view()[:4] == (10, 0, 1, 1)
    h.command(STEP_POS, 5, same_write=True)
    assert h.view()[:4] == (15, 0, 1, 0)
    # inputs are sampled at the executing edge: change DATA after edge 2, before edge 3
    c = h.c
    c.set_inputs(ui_in=7, uio_in=pack_uio_in(0, STEP_POS))
    c.clock()
    c.set_inputs(uio_in=pack_uio_in(1, STEP_POS))
    c.clock()
    c.clock()
    c.set_inputs(ui_in=9)
    c.clock()
    assert h.view()[:4] == (24, 0, 1, 1)


def test_cycle_strobe_through_reset():
    # D2: STROBE high through reset and for 10 edges after release
    h = Host(32)
    c = h.c
    c.set_inputs(ui_in=0, uio_in=pack_uio_in(1, NOP), rst_n=0)
    for _ in range(5):
        c.clock()
    assert h.view() == (0, 0, 1, 0, 0, 0)
    c.set_inputs(rst_n=1)
    for _ in range(10):
        c.clock()
        assert h.view() == (0, 0, 1, 0, 0, 0)
    # STROBE low for 3 edges, then a NOP
    c.set_inputs(uio_in=pack_uio_in(0, NOP))
    for _ in range(3):
        c.clock()
    assert h.view() == (0, 0, 1, 0, 0, 0)
    h.command(NOP, 0, same_write=True)
    assert h.view() == (0, 0, 1, 1, 0, 0)


def test_cycle_held_strobe_one_command():
    # D14: STROBE held high for 20 edges gives exactly one command
    h = Host(32)
    h.reset()
    c = h.c
    c.set_inputs(ui_in=5, uio_in=pack_uio_in(1, STEP_POS))
    toggles = 0
    ack = c.uio_out() >> 7
    for _ in range(20):
        c.clock()
        a = c.uio_out() >> 7
        toggles += a != ack
        ack = a
    assert toggles == 1
    assert h.view()[:4] == (5, 0, 1, 1)
    assert c.fired == 1
    # D19: after ACK, CMD and DATA change with STROBE still high: ignored
    h = Host(32)
    h.reset()
    c = h.c
    c.set_inputs(ui_in=10, uio_in=pack_uio_in(1, STEP_POS))
    for _ in range(3):
        c.clock()
    assert h.view()[:4] == (10, 0, 1, 1)
    c.set_inputs(ui_in=0x80, uio_in=pack_uio_in(1, STEP_NEG))
    for _ in range(10):
        c.clock()
        assert h.view()[:4] == (10, 0, 1, 1)
    c.set_inputs(uio_in=pack_uio_in(0, STEP_NEG))
    for _ in range(3):
        c.clock()
    h.command(STEP_NEG, -128 & 0xFF)
    assert h.view()[:3] == (138, 0, 1)


def test_cycle_reset_fixed_point():
    c = TauSeedCycle(32)
    # the reset state, with STROBE high, stays put and never fires
    c.set_inputs(ui_in=0x55, uio_in=pack_uio_in(1, STEP_POS), rst_n=0)
    c.clock()
    assert c.sync_state() == (1, 1, 1)
    assert view(c.model) == (0, 0, 1, 0, 0, 0)
    c.set_inputs(rst_n=1)
    for _ in range(20):
        c.clock()
        assert c.sync_state() == (1, 1, 1)
        assert view(c.model) == (0, 0, 1, 0, 0, 0)
    assert c.fired == 0
    # reset applied again is idempotent
    c.set_inputs(rst_n=0)
    c.clock()
    assert c.sync_state() == (1, 1, 1)
    # with STROBE low the synchroniser drains: (0,1,1) -> (0,0,1) -> (0,0,0)
    c.set_inputs(rst_n=1, uio_in=pack_uio_in(0, STEP_POS))
    seen = []
    for _ in range(3):
        c.clock()
        seen.append(c.sync_state())
    assert seen == [(0, 1, 1), (0, 0, 1), (0, 0, 0)]
    assert c.fired == 0


def test_cycle_reset_mid_command():
    # a reset during the handshake kills the command and puts every output at reset
    h = Host(32)
    h.reset()
    c = h.c
    c.set_inputs(ui_in=9, uio_in=pack_uio_in(1, STEP_POS))
    c.clock()
    c.clock()
    c.set_inputs(rst_n=0)
    c.clock()
    assert h.view() == (0, 0, 1, 0, 0, 0)
    assert c.sync_state() == (1, 1, 1)
    c.set_inputs(rst_n=1)
    for _ in range(10):
        c.clock()
    assert h.view() == (0, 0, 1, 0, 0, 0)


def test_dtable_cycle_d20_handshake_timing():
    # D20: first command 3 edges after release; 10 x STEP_POS x=1, gap exactly 3,
    # five of them with CMD and STROBE in one write. Host.command asserts ACK at edge 3.
    h = Host(32)
    h.reset(edges=5, idle=3)
    for i in range(10):
        h.command(STEP_POS, 1, gap=3, same_write=(i % 2 == 0))
        assert h.view()[:3] == (i + 1, 0, 1)
    assert h.view()[:4] == (10, 0, 1, 0)
    assert h.c.fired == 10


def test_cycle_matches_command_model_random():
    # Run generator ops through the pin-level host and compare with a separate command-level model.
    h = Host(32)
    h.reset()
    ref = TauSeedModel(32)
    for op in randgen.gen_ops(randgen.SEED_A, 1500, 32):
        if op[0] == "RESET":
            h.reset()
            ref.reset()
        else:
            _, code, data, bsel, gap, same = op
            h.command(code, data, gap=gap, same_write=same, bsel=bsel)
            ref.command(code, data)
        assert view(h.c.model) == view(ref)
    assert view(ref)[3] in (0, 1)


# --------------------------------------------------------------------------
# random generator
# --------------------------------------------------------------------------

# Pinned from the first implementation of randgen.py (2026-10-27): sha256 of
# repr(gen_ops(seed, 5000, 32)). A change in any of these means the generated
# operations changed and every recorded random run is no longer comparable.
GOLDEN = {
    0x7A050001: "88f69da7e0800c27f7e3d2be9a5d22ece2b72d24ba86a7fb9ae6ada3a5e6bd55",
    0x7A050002: "93c2d1dc3a8f41b09e18588b79d5f29983d1c151d332c9f7a731a7f889565f99",
    0x7A050003: "e06cac99037f88b24255589488309734da6246a3b1a5db9e5c1026f1db29d02a",
    0x7A050004: "10f6049d6bc8233b857ceb0cae458063a51163efea7e2e08b03c9d91b36c5c3b",
}

# Coverage minimums, written out here (not imported from randgen.COVERAGE_MIN).
MIN_RTL = {"over_hi": 3, "over_lo": 3, "exact_hi": 2, "exact_lo": 2,
           "sptr_wraps": 3, "lptr_wraps": 3, "each_code": 10, "clear": 20,
           "resets": 1, "step_code10": 10}
MIN_GL = {"over_hi": 1, "over_lo": 1, "exact_hi": 1, "exact_lo": 1,
          "sptr_wraps": 1, "lptr_wraps": 1, "each_code": 3}


def test_xs32():
    # textbook xorshift32 (13, 17, 5) sequence from seed 1
    s = 1
    got = []
    for _ in range(6):
        s = randgen.xs32(s)
        got.append(s)
    assert got == [270369, 67634689, 2647435461, 307599695, 2398689233, 745495504]
    assert 0 < randgen.xs32(0x7A050001) <= 0xFFFFFFFF


def test_randgen_seeds():
    assert randgen.SEEDS == {"A": 0x7A050001, "B": 0x7A050002, "C": 0x7A050003, "D": 0x7A050004}


def test_randgen_golden():
    for seed, want in GOLDEN.items():
        ops = randgen.gen_ops(seed, 5000, 32)
        assert len(ops) == 5000
        got = hashlib.sha256(repr(ops).encode("ascii")).hexdigest()
        assert got == want, "seed %#x" % seed


def test_randgen_prefix():
    for seed in GOLDEN:
        full = randgen.gen_ops(seed, 5000, 32)
        assert randgen.gen_ops(seed, 1000, 32) == full[:1000]
        assert randgen.gen_ops(seed, 1, 32) == full[:1]
    # the stream is deterministic and seed dependent
    assert randgen.gen_ops(0x7A050001, 200, 32) == randgen.gen_ops(0x7A050001, 200, 32)
    assert randgen.gen_ops(0x7A050001, 200, 32) != randgen.gen_ops(0x7A050002, 200, 32)


def test_randgen_wellformed():
    for nw in (32, 16, 0):
        ops = randgen.gen_ops(0x7A050001, 5000, nw)
        gaps = set()
        same = 0
        n = 0
        for op in ops:
            if op[0] == "RESET":
                assert op == ("RESET",)
                continue
            tag, code, data, bsel, gap, sw = op
            assert tag == "CMD"
            assert 0 <= code <= 7 and 0 <= data <= 255 and bsel in (0, 1)
            assert gap in (3, 4, 5) and sw in (True, False)
            gaps.add(gap)
            same += sw
            n += 1
        assert gaps == {3, 4, 5}
        assert 0.15 * n < same < 0.35 * n


def check_cov(cov, mins, nw, mode):
    assert cov["over_hi"] >= mins["over_hi"], cov
    assert cov["over_lo"] >= mins["over_lo"], cov
    assert cov["exact_hi"] >= mins["exact_hi"], cov
    assert cov["exact_lo"] >= mins["exact_lo"], cov
    if nw > 0:
        assert cov["sptr_wraps"] >= mins["sptr_wraps"], cov
        assert cov["lptr_wraps"] >= mins["lptr_wraps"], cov
    for code in range(8):
        assert cov["codes"][code] >= mins["each_code"], (code, cov)
    if mode == "rtl":
        assert cov["codes"][CLEAR] >= mins["clear"], cov
        assert cov["resets"] >= mins["resets"], cov
        if nw > 0:
            assert cov["step_code10"] >= mins["step_code10"], cov
    assert randgen.coverage_failures(cov, mode, nw) == []


def test_randgen_coverage():
    assert randgen.COVERAGE_MIN["rtl"] == MIN_RTL
    assert randgen.COVERAGE_MIN["gl"] == MIN_GL
    for seed in GOLDEN:
        ops = randgen.gen_ops(seed, 5000, 32)
        cov = randgen.coverage(ops, 32)
        assert cov["ops"] == 5000 and cov["cmds"] + cov["resets"] == 5000
        check_cov(cov, MIN_RTL, 32, "rtl")
    for seed in (0x7A050001, 0x7A050002):
        ops = randgen.gen_ops(seed, 1000, 32)
        cov = randgen.coverage(ops, 32)
        assert cov["ops"] == 1000
        check_cov(cov, MIN_GL, 32, "gl")


def test_randgen_coverage_other_nw():
    for nw in (16, 0):
        for seed in GOLDEN:
            cov = randgen.coverage(randgen.gen_ops(seed, 5000, nw), nw)
            check_cov(cov, MIN_RTL, nw, "rtl")
        for seed in (0x7A050001, 0x7A050002):
            cov = randgen.coverage(randgen.gen_ops(seed, 1000, nw), nw)
            check_cov(cov, MIN_GL, nw, "gl")
