# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Reference model of the Ternara tau seed (docs/info.md).

Two layers:

* TauSeedModel - command level. One call to command() is one executed command.
* TauSeedCycle - pin and cycle level. It adds the STROBE synchroniser
  (s1, s2, p), the synchronous reset and the ACK latency, and it drives a
  TauSeedModel when the design would fire.

Pure Python, standard library only. It must also run under CPython for the
demo mock, so it keeps to plain integers and lists.
"""

# Command codes (docs/info.md, Commands)
NOP = 0
CLEAR = 1
LOAD_W = 2
STEP = 3
STEP_POS = 4
STEP_NEG = 5

ACC_MIN = -32768
ACC_MAX = 32767
VALID_NW = (0, 16, 32)
UIO_OE = 0xE0


def to_s8(b):
    """Interpret the low 8 bits of b as a two's complement int8."""
    b &= 0xFF
    return b - 256 if b & 0x80 else b


def to_s16(v):
    """Interpret the low 16 bits of v as a two's complement int16."""
    v &= 0xFFFF
    return v - 65536 if v & 0x8000 else v


def dec(code):
    """Weight code to weight: 00 -> 0, 01 -> +1, 11 -> -1, 10 (reserved) -> 0."""
    code &= 3
    if code == 1:
        return 1
    if code == 3:
        return -1
    return 0


def mac(acc, w, x):
    """One saturating multiply-accumulate. Returns (acc_next, ovf).

    s = acc + w*x exactly; the result is clamped to int16; ovf is true when s
    was outside the int16 range. Exact limits are not an overflow.
    """
    s = acc + w * x
    if s > ACC_MAX:
        return ACC_MAX, True
    if s < ACC_MIN:
        return ACC_MIN, True
    return s, False


def pack_uio_in(strobe=0, cmd=0, bsel=0, upper=0):
    """Pack the uio_in byte: [0] STROBE, [3:1] CMD, [4] BSEL, [7:5] ignored."""
    return (strobe & 1) | ((cmd & 7) << 1) | ((bsel & 1) << 4) | ((upper & 7) << 5)


class TauSeedModel(object):
    """Command-level model.

    Attributes: acc (int16 value), sat (0/1), sptr, lptr, ack (0/1),
    codes (list of NW raw 2-bit weight codes) and last (info about the last
    command, used by the coverage counter).
    """

    def __init__(self, nw):
        if nw not in VALID_NW:
            raise ValueError("NW must be one of %r, got %r" % (VALID_NW, nw))
        self.nw = nw
        self.slots = nw // 4
        self.reset()

    def reset(self):
        self.acc = 0
        self.sat = 0
        self.sptr = 0
        self.lptr = 0
        self.ack = 0
        self.codes = [0] * self.nw
        self.last = {"kind": "reset"}

    def command(self, cmd, data):
        """Execute one command. ACK toggles for every code, including NOP."""
        cmd &= 7
        data &= 0xFF
        self.ack ^= 1
        info = {"kind": "nop", "cmd": cmd}
        if cmd == CLEAR:
            self.acc = 0
            self.sat = 0
            self.sptr = 0
            self.lptr = 0
            info["kind"] = "clear"
        elif cmd == LOAD_W:
            if self.nw:
                for k in range(4):
                    self.codes[4 * self.lptr + k] = (data >> (2 * k)) & 3
                nxt = (self.lptr + 1) % self.slots
                info["kind"] = "load"
                info["wrap_l"] = nxt == 0
                self.lptr = nxt
        elif cmd in (STEP, STEP_POS, STEP_NEG):
            if cmd == STEP_POS:
                w = 1
                wcode = None
            elif cmd == STEP_NEG:
                w = -1
                wcode = None
            elif self.nw:
                wcode = self.codes[self.sptr]
                w = dec(wcode)
            else:
                w = None  # NW = 0: STEP is a NOP
                wcode = None
            if w is not None:
                x = to_s8(data)
                before = self.acc
                s = before + w * x
                self.acc, ovf = mac(before, w, x)
                self.sat |= int(ovf)
                info.update(kind="mac", s=s, ovf=ovf, before=before, w=w, wcode=wcode)
                if cmd == STEP:
                    nxt = (self.sptr + 1) % self.nw
                    info["wrap_s"] = nxt == 0
                    self.sptr = nxt
        self.last = info

    def outputs(self, bsel):
        """Pin-level view for a given BSEL value."""
        acc16 = self.acc & 0xFFFF
        q = (acc16 >> 8) if bsel else (acc16 & 0xFF)
        sptr0 = int(self.sptr == 0)
        return {
            "q": q,
            "sat": self.sat,
            "sptr0": sptr0,
            "ack": self.ack,
            "uio_out": (self.ack << 7) | (sptr0 << 6) | (self.sat << 5),
            "uio_oe": UIO_OE,
        }


class TauSeedCycle(object):
    """Pin and cycle-level model of tt_um_ternara_tau_seed.

    Use set_inputs() to present the inputs for the next rising edge, then
    clock() once per edge. The state follows the RTL:

        reset (rst_n = 0, synchronous): s1 = s2 = p = 1 and all state cleared
        otherwise: fire = s2 & ~p (old values); s1 <= STROBE; s2 <= s1; p <= s2;
                   on fire the command in uio_in[3:1] and ui_in is executed.
    """

    def __init__(self, nw):
        self.model = TauSeedModel(nw)
        self.nw = nw
        self.ui_in = 0
        self.uio_in = 0
        self.rst_n = 0
        self.s1 = self.s2 = self.p = 1
        self.edges = 0
        self.fired = 0

    def set_inputs(self, ui_in=None, uio_in=None, rst_n=None):
        if ui_in is not None:
            self.ui_in = ui_in & 0xFF
        if uio_in is not None:
            self.uio_in = uio_in & 0xFF
        if rst_n is not None:
            self.rst_n = 1 if rst_n else 0

    def clock(self):
        """One rising clock edge."""
        if not self.rst_n:
            self.model.reset()
            self.s1 = self.s2 = self.p = 1
        else:
            fire = bool(self.s2 and not self.p)
            n1 = self.uio_in & 1
            n2 = self.s1
            np_ = self.s2
            if fire:
                self.model.command((self.uio_in >> 1) & 7, self.ui_in)
                self.fired += 1
            self.s1, self.s2, self.p = n1, n2, np_
        self.edges += 1

    def sync_state(self):
        return (self.s1, self.s2, self.p)

    def uo_out(self):
        return self.model.outputs((self.uio_in >> 4) & 1)["q"]

    def uio_out(self):
        return self.model.outputs(0)["uio_out"]

    def uio_oe(self):
        return UIO_OE
