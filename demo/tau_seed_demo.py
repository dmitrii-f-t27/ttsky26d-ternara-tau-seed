# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
#
# Board demo for tt_um_ternara_tau_seed (Tiny Tapeout demo board, MicroPython,
# firmware SDK `ttboard`). Copy this file to the board and run, at the
# MicroPython prompt:
#
#     import tau_seed_demo
#     tau_seed_demo.main()
#
# It prints one line, `TAU SEED DEMO: PASS` or `TAU SEED DEMO: FAIL <reason>`,
# and main() returns True or False.
#
# The demo follows the host rules of docs/info.md:
#   * the clock is single-stepped (clock_project_once), never a PWM clock;
#   * reset: stop the clock, hold rst_n low for 3 pulses with STROBE low,
#     release, then 3 pulses with STROBE low;
#   * CMD and DATA are set before STROBE rises and stay put until ACK toggles;
#   * after ACK, STROBE goes low and stays low for at least 3 pulses before
#     the next rise;
#   * at most 8 pulses are spent waiting for each ACK.
#
# After every command the demo reads Q at BSEL 0 and 1, SAT, SPTR0 and ACK and
# compares them with its own reference arithmetic (the clamp below) and with
# the constants 2099 and -2099.
#
# Language: a small subset that MicroPython and CPython both run. No f-strings,
# annotations, walrus, async or match. The only imports are the board SDK,
# inside main().

NW = 32  # stored ternary weights; equals NW in src/project.v

# command codes
NOP = 0
CLEAR = 1
LOAD_W = 2
STEP = 3
STEP_POS = 4
STEP_NEG = 5

# uio_in: [0] STROBE, [3:1] CMD, [4] BSEL
UIO_OE_PICO = 0x1F  # RP drives uio[4:0]; uio[7:5] (SAT, SPTR0, ACK) are chip outputs
RESET_EDGES = 3  # clock pulses with rst_n low (at least 2 are needed)
GAP_EDGES = 3  # clock pulses with STROBE low before STROBE may rise
ACK_WAIT = 8  # most clock pulses spent waiting for one ACK

ACC_MIN = -32768
ACC_MAX = 32767

# Neuron vector D18: 32 weights and activations from a formula.
D18_W = [(i % 3) - 1 for i in range(32)]
D18_X = [100 * D18_W[i] + ((5 * i) % 11) - 5 for i in range(32)]
D18_EXPECT = 2099
D18_NEG_EXPECT = -2099


def pack_weights(ws):
    # four weights per byte; weight 4*j+k sits in bits [2k+1:2k] of byte j
    # (code 00 = 0, 01 = +1, 11 = -1)
    out = []
    for j in range(len(ws) // 4):
        b = 0
        for k in range(4):
            w = ws[4 * j + k]
            if w == 1:
                code = 1
            elif w == -1:
                code = 3
            else:
                code = 0
            b |= code << (2 * k)
        out.append(b)
    return out


D18_BYTES = pack_weights(D18_W)
D18_NEG_BYTES = pack_weights([-w for w in D18_W])


def clamp(v):
    if v > ACC_MAX:
        return ACC_MAX
    if v < ACC_MIN:
        return ACC_MIN
    return v


class _DemoFail(Exception):
    pass


class _Ref:
    # the accumulator as the demo expects it
    def __init__(self):
        self.acc = 0
        self.sat = 0

    def clear(self):
        self.acc = 0
        self.sat = 0

    def mac(self, w, x):
        s = self.acc + w * x
        if s > ACC_MAX or s < ACC_MIN:
            self.sat = 1
        self.acc = clamp(s)


class _Host:
    # one object per run: remembers what the demo drives on the pins
    def __init__(self, tt):
        self.tt = tt
        self.strobe = 0
        self.cmd = 0
        self.bsel = 0
        self.ack = 0  # ACK level seen last
        self.low_edges = 0  # clock pulses since STROBE was last high

    def put_uio(self):
        self.tt.uio_in.value = self.strobe | (self.cmd << 1) | (self.bsel << 4)

    def edge(self):
        # one full clock pulse
        self.tt.clock_project_once()
        if self.strobe:
            self.low_edges = 0
        else:
            self.low_edges += 1

    def idle(self, n):
        for _ in range(n):
            self.edge()

    def ack_level(self):
        return (int(self.tt.uio_out.value) >> 7) & 1

    def command(self, cmd, data):
        # host rule 3 / 7: STROBE low for at least GAP_EDGES pulses before it rises
        while self.low_edges < GAP_EDGES:
            self.edge()
        # host rule 1: CMD and DATA first, then STROBE
        self.cmd = cmd & 7
        self.strobe = 0
        self.tt.ui_in.value = data & 0xFF
        self.put_uio()
        self.strobe = 1
        self.put_uio()
        # host rule 2: CMD and DATA stay put until ACK toggles (at most ACK_WAIT pulses)
        old = self.ack
        got = False
        for _ in range(ACK_WAIT):
            self.edge()
            if self.ack_level() != old:
                got = True
                break
        self.strobe = 0
        self.put_uio()
        if not got:
            raise _DemoFail("no ACK within %d clocks (cmd %d)" % (ACK_WAIT, cmd))
        self.ack = old ^ 1

    def read_q(self, bsel):
        self.bsel = bsel
        self.put_uio()
        return int(self.tt.uo_out.value) & 0xFF

    def read(self):
        # returns (acc, sat, sptr0, ack); Q is read at BSEL 0 and BSEL 1
        lo = self.read_q(0)
        flags0 = int(self.tt.uio_out.value) & 0xE0
        hi = self.read_q(1)
        flags1 = int(self.tt.uio_out.value) & 0xE0
        self.bsel = 0
        self.put_uio()
        if flags0 != flags1:
            raise _DemoFail("SAT/SPTR0/ACK changed with BSEL")
        acc = (hi << 8) | lo
        if acc & 0x8000:
            acc -= 65536
        return acc, (flags0 >> 5) & 1, (flags0 >> 6) & 1, (flags0 >> 7) & 1

    def check(self, what, acc, sat, sptr0):
        got_acc, got_sat, got_sptr0, got_ack = self.read()
        if got_acc != acc or got_sat != sat:
            raise _DemoFail(
                "%s: acc %d SAT %d, expected acc %d SAT %d" % (what, got_acc, got_sat, acc, sat)
            )
        if got_sptr0 != sptr0:
            raise _DemoFail("%s: SPTR0 %d, expected %d" % (what, got_sptr0, sptr0))
        if got_ack != self.ack:
            raise _DemoFail("%s: ACK %d, expected %d" % (what, got_ack, self.ack))

    def clear(self, ref, what):
        self.command(CLEAR, 0)
        ref.clear()
        self.check(what + " CLEAR", 0, 0, 1)

    def finish(self):
        # leave the pins idle: STROBE low, CMD 0, BSEL 0
        self.strobe = 0
        self.cmd = 0
        self.bsel = 0
        self.put_uio()
        self.tt.ui_in.value = 0


def _selfcheck():
    # the demo's own arithmetic must reproduce the constants
    r = _Ref()
    for i in range(32):
        r.mac(D18_W[i], D18_X[i])
    if r.acc != D18_EXPECT or r.sat != 0:
        raise _DemoFail("internal: D18 reference is %d, expected %d" % (r.acc, D18_EXPECT))
    r.clear()
    for i in range(32):
        r.mac(-D18_W[i], D18_X[i])
    if r.acc != D18_NEG_EXPECT or r.sat != 0:
        raise _DemoFail("internal: negated D18 reference is %d, expected %d" % (r.acc, D18_NEG_EXPECT))
    if len(D18_BYTES) != 8 or len(D18_NEG_BYTES) != 8:
        raise _DemoFail("internal: D18 weight bytes")


def _reset(host):
    tt = host.tt
    tt.clock_project_stop()
    tt.uio_oe_pico.value = UIO_OE_PICO
    tt.ui_in.value = 0
    host.strobe = 0
    host.cmd = 0
    host.bsel = 0
    host.put_uio()
    tt.reset_project(True)
    host.idle(RESET_EDGES)
    tt.reset_project(False)
    host.low_edges = 0
    host.ack = 0
    host.idle(GAP_EDGES)


def _stored_pass(host, ref, load_bytes, sign, what):
    # D18 (a)/(b): load the weights, step through all 32 activations.
    # With NW = 16 this runs as two halves with a reload.
    n4 = NW // 4
    for h in range(32 // NW):
        for j in range(n4):
            host.command(LOAD_W, load_bytes[h * n4 + j])
            host.check("%s LOAD_W %d" % (what, h * n4 + j), ref.acc, ref.sat, 1)
        for i in range(h * NW, (h + 1) * NW):
            host.command(STEP, D18_X[i])
            ref.mac(sign * D18_W[i], D18_X[i])
            if (i + 1) % NW == 0:
                sptr0 = 1
            else:
                sptr0 = 0
            host.check("%s STEP %d" % (what, i), ref.acc, ref.sat, sptr0)


def _streamed_pass(host, ref, what):
    # D18 (c): +1 -> STEP_POS, -1 -> STEP_NEG, 0 -> no command
    for i in range(32):
        w = D18_W[i]
        if w == 0:
            continue
        if w == 1:
            host.command(STEP_POS, D18_X[i])
        else:
            host.command(STEP_NEG, D18_X[i])
        ref.mac(w, D18_X[i])
        host.check("%s step %d" % (what, i), ref.acc, ref.sat, 1)


def _show(what, acc, sat):
    print("%s: acc = %d (0x%04X), SAT %d" % (what, acc, acc & 0xFFFF, sat))


def _body(host):
    ref = _Ref()
    _reset(host)
    host.check("after reset", 0, 0, 1)
    print("reset: acc = 0, SAT 0, SPTR0 1, ACK 0")

    if NW:
        host.clear(ref, "D18 (a)")
        _stored_pass(host, ref, D18_BYTES, 1, "D18 (a)")
        host.check("D18 (a) result", D18_EXPECT, 0, 1)
        _show("D18 (a) stored weights", ref.acc, ref.sat)

        host.clear(ref, "D18 (b)")
        _stored_pass(host, ref, D18_NEG_BYTES, -1, "D18 (b)")
        host.check("D18 (b) result", D18_NEG_EXPECT, 0, 1)
        _show("D18 (b) negated weights", ref.acc, ref.sat)
    else:
        print("D18 (a), (b): skipped, NW = 0")

    host.clear(ref, "D18 (c)")
    _streamed_pass(host, ref, "D18 (c)")
    host.check("D18 (c) result", D18_EXPECT, 0, 1)
    _show("D18 (c) streamed weights", ref.acc, ref.sat)

    host.clear(ref, "D7")
    for n in range(255):
        host.command(STEP_NEG, -128)
        ref.mac(-1, -128)
        host.check("D7 step %d" % (n + 1), ref.acc, ref.sat, 1)
    host.check("D7 result", 32640, 0, 1)
    _show("D7 after 255 steps of +128", ref.acc, ref.sat)
    host.command(STEP_NEG, -128)
    ref.mac(-1, -128)
    host.check("D8 result", 32767, 1, 1)
    _show("D8 one more step", ref.acc, ref.sat)


def _run(tt):
    _selfcheck()
    host = _Host(tt)
    try:
        _body(host)
    finally:
        host.finish()


def run(tt):
    # tt: the board object (DemoBoard.get()) with the project enabled and the
    # RP in ASIC_RP_CONTROL mode. Returns True for PASS, False for FAIL.
    try:
        _run(tt)
    except _DemoFail as e:
        print("TAU SEED DEMO: FAIL " + str(e))
        return False
    print("TAU SEED DEMO: PASS")
    return True


def main():
    from ttboard.demoboard import DemoBoard
    from ttboard.mode import RPMode

    tt = DemoBoard.get()
    tt.shuttle.tt_um_ternara_tau_seed.enable()
    tt.mode = RPMode.ASIC_RP_CONTROL
    return run(tt)


if __name__ == "__main__":
    main()
