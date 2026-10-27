# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Seeded, version-independent random operation generator.

PRNG: xorshift32 (not the `random` module), so a seed gives the same
operations on every Python version. The generator drives its own
TauSeedModel, so each operation can depend on the state left by the previous
one. gen_ops(s, n, nw) is a stream: gen_ops(s, 1000, nw) is always the
prefix of gen_ops(s, 5000, nw).

Operations:
    ("CMD", code, data, bsel, gap, same_write)
    ("RESET",)

Modes (PLAN 1.4):
    MIX   STEP 30 %, STEP_POS 15 %, STEP_NEG 15 %, LOAD_W 15 %, CLEAR 5 %,
          codes 0/6/7 6 % in total, the remaining 14 % spread over all codes.
          x is uniform with 10 % extremes (127, -127, -128).
    UP / DOWN
          85 % of the operations push the accumulator towards +32767 / -32768
          (STEP_POS, STEP_NEG, or STEP when the next stored weight has the
          right sign); the other 15 % are MIX operations. Within 128 of the
          limit 25 % of the pushes pick the x that lands exactly on the limit.
          After 20 operations at the limit the mode returns to MIX.

Documented refinements, needed so that the coverage minimums hold for the
fixed seeds (and for other seeds with margin):
    R1  The two directions alternate after the first (random) phase.
    R2  In UP / DOWN the 15 % MIX operations draw CLEAR as LOAD_W, so a phase
        is not wiped out half way; entering a phase with |acc| > 1024 issues
        one CLEAR first.
    R3  Inside 128 of the limit, pushes that are not the exact landing use a
        smaller x, and a MIX operation that would overshoot gets x = 0, so
        the accumulator cannot overshoot before it has landed on the limit.
    R4  Opening: until two phases are finished, a phase starts after exactly
        60 MIX operations and no RESET is drawn, so the first 1000 operations
        visit both limits. After that a phase starts with probability 1/200
        per MIX operation and RESET has probability 1/1500 per operation.
    R5  Push size is uniform in 96..128 (inside the plan's |x| >= 64 band).
        STEP is used in 3 of 4 pushes whenever the next stored weight allows;
        when that weight is 0 / reserved, 1 push in 6 is a STEP that only
        advances sptr (so pointer wraps happen inside a phase).
    R6  The generator discards its first 16 PRNG outputs (seeds A..D differ in
        a few low bits only).
"""

from model import (
    TauSeedModel, dec, to_s8, CLEAR, LOAD_W, STEP, STEP_POS, STEP_NEG, ACC_MAX, ACC_MIN,
)

SEED_A = 0x7A050001
SEED_B = 0x7A050002
SEED_C = 0x7A050003
SEED_D = 0x7A050004
SEEDS = {"A": SEED_A, "B": SEED_B, "C": SEED_C, "D": SEED_D}

_MASK = 0xFFFFFFFF

PUSH_PCT = 85
EXACT_PCT = 25
NEAR = 128
LIMIT_OPS = 20
OPEN_PHASES = 2
OPEN_DWELL = 60
SWITCH_ONE_IN = 200
RESET_ONE_IN = 1500
ENTER_CLEAR_ABOVE = 1024
WARMUP = 16


def xs32(s):
    s ^= (s << 13) & 0xFFFFFFFF
    s ^= s >> 17
    s ^= (s << 5) & 0xFFFFFFFF
    return s


class _Gen(object):
    def __init__(self, seed, nw):
        self.s = (seed & _MASK) or 1
        for _ in range(WARMUP):
            self.s = xs32(self.s)
        self.nw = nw
        self.m = TauSeedModel(nw)
        self.mode = "MIX"
        self.mix_run = 0
        self.at_limit = 0
        self.phases_done = 0
        self.last_dir = 0

    def rnd(self, n):
        self.s = xs32(self.s)
        return (self.s >> 8) % n

    # -- field generators -------------------------------------------------
    def _x(self):
        if self.rnd(10) == 0:
            return (127, -127, -128)[self.rnd(3)]
        return self.rnd(256) - 128

    def _mix_fields(self, no_clear):
        r = self.rnd(100)
        if r < 30:
            code = STEP
        elif r < 45:
            code = STEP_POS
        elif r < 60:
            code = STEP_NEG
        elif r < 75:
            code = LOAD_W
        elif r < 80:
            code = CLEAR
        elif r < 86:
            code = (0, 6, 7)[self.rnd(3)]
        else:
            code = self.rnd(8)
        if code == CLEAR and no_clear:
            code = LOAD_W
        if code in (STEP, STEP_POS, STEP_NEG):
            data = self._x() & 0xFF
        else:
            data = self.rnd(256)
        return code, data

    def _push(self, up):
        m = self.m
        sgn = 1 if up else -1
        limit = ACC_MAX if up else ACC_MIN
        d = (limit - m.acc) * sgn  # distance to the limit, >= 0
        if d == 0 or d > NEAR:
            a = 96 + self.rnd(33)
        elif d == 1 or self.rnd(100) < EXACT_PCT:
            a = d
        else:
            a = 1 + self.rnd(d - 1)
        delta = sgn * a
        opts = []
        if -128 <= delta <= 127:
            opts.append((STEP_POS, delta))
        if -127 <= delta <= 128:
            opts.append((STEP_NEG, -delta))
        step_opt = None
        ws = dec(m.codes[m.sptr]) if self.nw else 0
        if ws:
            xs = delta * ws
            if -128 <= xs <= 127:
                step_opt = (STEP, xs)
        if step_opt is not None and self.rnd(4) != 0:
            return step_opt[0], step_opt[1] & 0xFF
        if self.nw and ws == 0 and self.rnd(6) == 0:
            # the next stored weight is 0 / reserved: a STEP leaves acc alone
            # but moves sptr on, so the pointer can wrap during a phase
            return STEP, self.rnd(256)
        code, x = opts[self.rnd(len(opts))]
        return code, x & 0xFF

    def _no_overshoot(self, code, data, up):
        """R3: inside the approach zone a MIX MAC that would overshoot gets x = 0."""
        if code not in (STEP, STEP_POS, STEP_NEG):
            return data
        m = self.m
        sgn = 1 if up else -1
        d = ((ACC_MAX if up else ACC_MIN) - m.acc) * sgn
        if not 0 < d <= NEAR:
            return data
        if code == STEP_POS:
            w = 1
        elif code == STEP_NEG:
            w = -1
        else:
            w = dec(m.codes[m.sptr]) if self.nw else 0
        if (d - sgn * w * to_s8(data)) < 0:
            return 0
        return data

    def _finish(self, code, data):
        bsel = self.rnd(2)
        gap = 3 + self.rnd(3)
        same = self.rnd(4) == 0
        self.m.command(code, data)
        return ("CMD", code, data, bsel, gap, same)

    # -- one operation ----------------------------------------------------
    def next_op(self):
        opening = self.phases_done < OPEN_PHASES
        if not opening and self.rnd(RESET_ONE_IN) == 0:
            self.m.reset()
            self.at_limit = 0
            return ("RESET",)

        if self.mode != "MIX" and self.at_limit >= LIMIT_OPS:
            self.mode = "MIX"
            self.mix_run = 0
            self.at_limit = 0
            self.phases_done += 1
            opening = self.phases_done < OPEN_PHASES

        if self.mode == "MIX":
            self.mix_run += 1
            if opening:
                go = self.mix_run >= OPEN_DWELL
            else:
                go = self.rnd(SWITCH_ONE_IN) == 0
            if go:
                if self.last_dir == 0:
                    self.last_dir = 1 if self.rnd(2) else -1
                else:
                    self.last_dir = -self.last_dir
                self.mode = "UP" if self.last_dir > 0 else "DOWN"
                self.at_limit = 0
                if abs(self.m.acc) > ENTER_CLEAR_ABOVE:
                    return self._finish(CLEAR, self.rnd(256))

        if self.mode == "MIX":
            code, data = self._mix_fields(False)
            return self._finish(code, data)

        up = self.mode == "UP"
        limit = ACC_MAX if up else ACC_MIN
        if self.m.acc == limit:
            self.at_limit += 1
        if self.rnd(100) < PUSH_PCT:
            code, data = self._push(up)
        else:
            code, data = self._mix_fields(True)
            data = self._no_overshoot(code, data, up)
        return self._finish(code, data)


def gen_ops(seed, n, nw):
    """Return the first n operations for (seed, nw). A prefix-stable stream."""
    g = _Gen(seed, nw)
    return [g.next_op() for _ in range(n)]


# -- coverage, counted on the model -------------------------------------------

COVERAGE_MIN = {
    "rtl": {
        "over_hi": 3, "over_lo": 3, "exact_hi": 2, "exact_lo": 2,
        "sptr_wraps": 3, "lptr_wraps": 3,
        "each_code": 10, "clear": 20, "resets": 1, "step_code10": 10,
    },
    "gl": {
        "over_hi": 1, "over_lo": 1, "exact_hi": 1, "exact_lo": 1,
        "sptr_wraps": 1, "lptr_wraps": 1,
        "each_code": 3,
    },
}


def coverage(ops, nw):
    """Run ops on a fresh model and count the coverage items.

    over_hi / over_lo : MAC with s > 32767 / s < -32768
    exact_hi / exact_lo : MAC that lands exactly on 32767 / -32768 from a
                          different accumulator value
    sptr_wraps / lptr_wraps : STEP / LOAD_W that wrapped the pointer (NW > 0)
    codes : executed commands per code 0..7;  resets : RESET operations
    step_code10 : STEP that used the reserved weight code 10
    """
    m = TauSeedModel(nw)
    c = {
        "ops": len(ops), "cmds": 0, "resets": 0, "codes": [0] * 8,
        "over_hi": 0, "over_lo": 0, "exact_hi": 0, "exact_lo": 0,
        "sptr_wraps": 0, "lptr_wraps": 0, "step_code10": 0,
    }
    for op in ops:
        if op[0] == "RESET":
            m.reset()
            c["resets"] += 1
            continue
        code, data = op[1], op[2]
        c["cmds"] += 1
        c["codes"][code] += 1
        m.command(code, data)
        info = m.last
        if info["kind"] == "mac":
            s = info["s"]
            before = info["before"]
            if s > ACC_MAX:
                c["over_hi"] += 1
            elif s < ACC_MIN:
                c["over_lo"] += 1
            elif s == ACC_MAX and before != ACC_MAX:
                c["exact_hi"] += 1
            elif s == ACC_MIN and before != ACC_MIN:
                c["exact_lo"] += 1
            if code == STEP:
                if info["wcode"] == 2:
                    c["step_code10"] += 1
                if info.get("wrap_s"):
                    c["sptr_wraps"] += 1
        elif info["kind"] == "load" and info.get("wrap_l"):
            c["lptr_wraps"] += 1
    return c


def coverage_failures(cov, mode, nw):
    """Return a list of strings, one per unmet minimum (empty = all met)."""
    mins = COVERAGE_MIN[mode]
    bad = []
    for key in ("over_hi", "over_lo", "exact_hi", "exact_lo"):
        if cov[key] < mins[key]:
            bad.append("%s=%d < %d" % (key, cov[key], mins[key]))
    if nw > 0:
        for key in ("sptr_wraps", "lptr_wraps"):
            if cov[key] < mins[key]:
                bad.append("%s=%d < %d" % (key, cov[key], mins[key]))
    for code in range(8):
        if cov["codes"][code] < mins["each_code"]:
            bad.append("code%d=%d < %d" % (code, cov["codes"][code], mins["each_code"]))
    if mode == "rtl":
        if cov["codes"][CLEAR] < mins["clear"]:
            bad.append("clear=%d < %d" % (cov["codes"][CLEAR], mins["clear"]))
        if cov["resets"] < mins["resets"]:
            bad.append("resets=%d < %d" % (cov["resets"], mins["resets"]))
        if nw > 0 and cov["step_code10"] < mins["step_code10"]:
            bad.append("step_code10=%d < %d" % (cov["step_code10"], mins["step_code10"]))
    return bad
