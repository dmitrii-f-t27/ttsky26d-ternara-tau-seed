## How it works

Ternara tau seed is a streaming multiply-accumulate unit for ternary weights. It multiplies a signed 8-bit activation `x` (-128..127) by a ternary weight `w` in {-1, 0, +1} and adds the product to a signed 16-bit accumulator `acc`. The accumulator saturates instead of wrapping.

One multiply-accumulate (MAC) does three things:

1. It computes `s = acc + w*x` exactly. `s` is in -32896..32895.
2. It stores `acc = clamp(s, -32768, 32767)`.
3. It sets the sticky flag `SAT` when `s` was outside -32768..32767. Landing exactly on 32767 or on -32768 does not set `SAT`. `CLEAR` resets it.

The weight comes from one of two places:

- Stored weights: an on-chip memory of NW = 32 ternary weights, loaded four per byte by `LOAD_W`. `STEP` uses the weight at the read pointer, then advances the pointer. The pointer wraps after NW steps.
- Streamed weights: `STEP_POS` and `STEP_NEG` apply `w = +1` and `w = -1` directly. They ignore the memory and do not move the pointer.

Weight code, 2 bits per weight:

| Code | Weight |
|---|---|
| `00` | 0 |
| `01` | +1 |
| `11` | -1 |
| `10` | reserved, acts as 0 |

`LOAD_W` writes one byte of four weights: weight 4j+k of byte j sits in `DATA[2k+1:2k]`. The load pointer wraps after NW/4 bytes. A `STEP` with weight 0 still advances the read pointer.

### Commands

A command is selected by `CMD` (3 bits) and executed once per `STROBE` pulse.

| Code | Name | Effect |
|---|---|---|
| 0 | `NOP` | Nothing. `ACK` still toggles |
| 1 | `CLEAR` | `acc = 0`, `SAT = 0`, read pointer = 0, load pointer = 0. The stored weights are kept |
| 2 | `LOAD_W` | Write the four weights in `DATA` at the load pointer, then advance the load pointer |
| 3 | `STEP` | MAC with the stored weight at the read pointer and `x = DATA`, then advance the read pointer |
| 4 | `STEP_POS` | MAC with `w = +1` and `x = DATA` |
| 5 | `STEP_NEG` | MAC with `w = -1` and `x = DATA` |
| 6, 7 | reserved | Same as `NOP` |

### Pins

| Pin | Direction | Name | Meaning |
|---|---|---|---|
| `ui[7:0]` | in | `DATA` | Activation `x` for steps. Four packed weights for `LOAD_W`. Ignored otherwise |
| `uio[0]` | in | `STROBE` | A rising edge executes one command |
| `uio[3:1]` | in | `CMD` | Command code |
| `uio[4]` | in | `BSEL` | Selects the output byte: 0 for `acc[7:0]`, 1 for `acc[15:8]` |
| `uio[5]` | out | `SAT` | Sticky saturation flag |
| `uio[6]` | out | `SPTR0` | 1 when the read pointer is 0 |
| `uio[7]` | out | `ACK` | Toggles once per executed command, `NOP` and reserved codes included |
| `uo[7:0]` | out | `Q` | `acc[15:8]` when `BSEL` is 1, else `acc[7:0]` |

`uio[7:5]` are outputs (`uio_oe` = `0xE0`); `uio[4:0]` are inputs and `uio_out[4:0]` is 0. `uio_in[7:5]` and `ena` are ignored. `rst_n` is an active-low synchronous reset. The target clock is 25 MHz.

### Handshake

`STROBE` goes through a two-flip-flop synchroniser and an edge detector, and the design fires on the rising edge the detector sees. `CMD` and `DATA` are sampled directly at the execute edge, so the host must hold them steady. The command executes on the 3rd rising clock edge after `STROBE` rises (the 4th if `STROBE` changed too close to the first edge). `Q`, `SAT`, `SPTR0` and `ACK` are valid after that edge.

Host rules:

1. Set `CMD` and `DATA`, then raise `STROBE`. The two may be one write.
2. Keep `CMD` and `DATA` unchanged until `ACK` has toggled. Allow at most 8 clock edges.
3. Lower `STROBE`. Keep it low for at least 3 clock edges before the next rise.
4. `Q`, `SAT`, `SPTR0` and `ACK` are valid after the edge that executed the command.
5. The clock may run freely or be single-stepped. With a free-running clock, read `Q` (and change `BSEL`) at least one clock period after `ACK` is seen.
6. To reset: stop any PWM clock, hold `rst_n` low for at least 2 clock edges with `STROBE` low, then release.
7. After `rst_n` rises, keep `STROBE` low for at least 3 clock edges.

Reset clears `acc`, `SAT`, both pointers, `ACK` and all NW stored weights. The reset needs clock edges because it is synchronous. A `STROBE` that is high through reset, or high at the first edges after release, never fires: the first command needs a low-to-high transition seen after reset.

## How to test

### Simulation

```
cd test
pip install -r requirements.txt
make
```

This needs Icarus Verilog. `make` checks that `NW`, the clock period, `clock_hz` and this datasheet agree, runs the cocotb tests on the RTL, and then runs a guard that fails unless exactly the 24 expected tests ran and none failed, errored or was skipped. A good run ends with `tau_guard: PASS mode=rtl tests=24/24`.

The tests drive only the top-level pins. After every command they read `Q` at `BSEL` 0 and 1, `SAT`, `SPTR0`, `ACK` and `uio_oe`, and compare them with a Python model (`test/model.py`). They also check that no output changes while the host waits for `ACK` or holds `STROBE` low, and that every read is free of X and Z. Some of the directed vectors:

| Test | Result |
|---|---|
| NW/4 x `LOAD_W` with `0x55` (all weights +1), then NW x `STEP` with x = 127 | acc = 127 x NW, which is 4064 for NW = 32 |
| NW/4 x `LOAD_W` with `0xFF` (all weights -1), then NW x `STEP` with x = -128 | acc = 128 x NW, which is 4096 for NW = 32 |
| 255 x `STEP_NEG` with x = -128, then one more | 32640 with `SAT` 0, then 32767 with `SAT` 1 |
| 256 x `STEP_POS` with x = -128, then one more | -32768 with `SAT` 0, then -32768 with `SAT` 1 |
| 9 x `STEP_POS` with x = -128, then `STEP_POS` with x = -82 | acc = -1234: `Q` = `0x2E` at `BSEL` 0, `0xFB` at `BSEL` 1 |
| `STROBE` held high for 20 edges | exactly one `ACK` toggle |

There are also random tests with fixed seeds, which cover saturation in both directions, the exact limits and pointer wraps.

### Neuron vector

The demo and the tests share one 32-input vector, called D18. Weights and activations for i = 0..31:

```
w[i] = (i mod 3) - 1
x[i] = 100*w[i] + ((5*i) mod 11) - 5
```

The weights packed for `LOAD_W` are `D3 34 4D D3 34 4D D3 34`; the negated weights are `71 1C C7 71 1C C7 71 1C`. For NW = 32 a stored run is 8 x `LOAD_W` with these bytes, then 32 x `STEP` with x[i]. The expected sums are:

| Run | acc | `Q` at `BSEL` 0 / 1 |
|---|---|---|
| after reset: stored run with the weights | 2099 (`0x0833`) | `0x33` / `0x08` |
| `CLEAR`, stored run with the negated weights | -2099 (`0xF7CD`) | `0xCD` / `0xF7` |
| `CLEAR`, then streamed weights: `STEP_POS` for +1, `STEP_NEG` for -1, no command for 0 | 2099 | `0x33` / `0x08` |

### Demo board

`demo/tau_seed_demo.py` is a MicroPython script for the Tiny Tapeout demo board. Copy it to the board and run it at the MicroPython prompt:

```
import tau_seed_demo
tau_seed_demo.main()
```

The board needs a firmware release whose shuttle list includes TTSKY26d. The script enables `tt_um_ternara_tau_seed`, puts the microcontroller in `ASIC_RP_CONTROL` mode so that it drives `ui_in`, and sets `uio_oe_pico` to `0x1F` so that it drives `uio[4:0]`. It stops the clock, resets the chip with single clock pulses, then runs the D18 vector three ways (stored, negated, streamed) and the saturation sequence of 255 steps and one more. It follows the host rules above and waits at most 8 clock pulses for each `ACK`. The last line it prints is `TAU SEED DEMO: PASS`, or `TAU SEED DEMO: FAIL` with a reason.

Keep all input DIP switches of the demo board off while the microcontroller drives `ui_in`.

On a computer, `pytest demo/test_demo_host.py`, run from the repository root, runs the same script against a mock board that is backed by the Python model and that rejects steps breaking the host rules.

### Manual steps

The same commands can be typed one by one at the MicroPython prompt. This resets the chip, sends one `STEP_POS` with x = 5 and reads the result:

```python
from ttboard.demoboard import DemoBoard
from ttboard.mode import RPMode

tt = DemoBoard.get()
tt.shuttle.tt_um_ternara_tau_seed.enable()
tt.mode = RPMode.ASIC_RP_CONTROL
tt.uio_oe_pico.value = 0x1F          # the microcontroller drives uio[4:0]

# Reset: no PWM clock, rst_n low for 3 pulses, release, 3 pulses with STROBE low.
tt.clock_project_stop()
tt.ui_in.value = 0
tt.uio_in.value = 0
tt.reset_project(True)
for _ in range(3):
    tt.clock_project_once()
tt.reset_project(False)
for _ in range(3):
    tt.clock_project_once()

# One command: STEP_POS (code 4) with x = 5. CMD and DATA first, then STROBE.
tt.ui_in.value = 5
tt.uio_in.value = 4 << 1
tt.uio_in.value = (4 << 1) | 1
for _ in range(3):
    tt.clock_project_once()          # the command executes on the 3rd pulse
tt.uio_in.value = 0                  # lower STROBE
for _ in range(3):
    tt.clock_project_once()          # STROBE stays low for 3 pulses

# Read the result: low byte at BSEL 0, high byte at BSEL 1, flags from uio_out.
lo = tt.uo_out.value
tt.uio_in.value = 0x10
hi = tt.uo_out.value
flags = tt.uio_out.value
print("acc =", (hi << 8) | lo, "ACK =", (flags >> 7) & 1)
```

The last line prints `acc = 5 ACK = 1`.

## External hardware

None.
