# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Test data for the neuron vector D18 (SPEC section 6.2).

32 ternary weights, packed four per LOAD_W byte: DATA[2k+1:2k] holds weight
4j+k of byte j. Weight codes: 00 = 0, 01 = +1, 11 = -1.

  w[i] = (i mod 3) - 1
  x[i] = 100*w[i] + ((5*i) mod 11) - 5     for i = 0..31
"""

D18_W = [(i % 3) - 1 for i in range(32)]
D18_X = [100 * D18_W[i] + ((5 * i) % 11) - 5 for i in range(32)]

_CODE = {0: 0, 1: 1, -1: 3}


def pack_weights(ws):
    """Pack weights (-1, 0, +1), four per byte, weight 4j+k in DATA[2k+1:2k]."""
    assert len(ws) % 4 == 0
    out = []
    for j in range(len(ws) // 4):
        b = 0
        for k in range(4):
            b |= _CODE[ws[4 * j + k]] << (2 * k)
        out.append(b)
    return out


D18_BYTES = pack_weights(D18_W)
D18_NEG_BYTES = pack_weights([-w for w in D18_W])

D18_EXPECT = sum(w * x for w, x in zip(D18_W, D18_X))
D18_NEG_EXPECT = -D18_EXPECT
