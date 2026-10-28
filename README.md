![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Ternara τ seed

A [Tiny Tapeout](https://tinytapeout.com) design (shuttle TTSKY26d, 1x1 tile, Verilog): a streaming multiply-accumulate unit that adds int8 activations times ternary weights {-1, 0, +1} into a saturating 16-bit accumulator.

- Pins, commands, protocol and how to test: [docs/info.md](docs/info.md)
- Simulate (cocotb with Icarus Verilog): `cd test && make`
- Board demo (MicroPython): [demo/tau_seed_demo.py](demo/tau_seed_demo.py)
- Author: Dmitrii Fedorov. License: Apache-2.0, see [LICENSE](LICENSE)
