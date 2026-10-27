# Sample testbench for a Tiny Tapeout project

This is a sample testbench for a Tiny Tapeout project. It uses [cocotb](https://docs.cocotb.org/en/stable/) to drive the DUT and check the outputs.
See below to get started or for more information, check the [website](https://tinytapeout.com/hdl/testing/).

## Setting up

1. Edit [Makefile](Makefile) and modify `PROJECT_SOURCES` to point to your Verilog files.
2. Edit [tb.v](tb.v) and replace `tt_um_example` with your module name.

## How to run

To run the RTL simulation:

```sh
make -B
```

To run gatelevel simulation, first harden your project and copy `../runs/wokwi/results/final/verilog/gl/{your_module_name}.v` to `gate_level_netlist.v`.

Then run:

```sh
make -B GATES=yes
```

If you wish to save the waveform in VCD format instead of FST format, edit tb.v to use `$dumpfile("tb.vcd");` and then run:

```sh
make -B FST=
```

This will generate `tb.vcd` instead of `tb.fst`.

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```

## Guard against a false green

`make` in this directory runs `tau_guarded` (see the end of [Makefile](Makefile)), which

1. checks that `NW`, `CLOCK_PERIOD` and `clock_hz` agree (`tau_check_config.py`),
2. runs the cocotb simulation (`make sim`),
3. runs `tau_guard.py`, which fails unless `results.xml` holds exactly the test names of
   [expected_tests.json](expected_tests.json) (24 for RTL, 22 for gate level), each once, and none failed,
   errored or was skipped.

The last line of a good RTL run is `tau_guard: PASS mode=rtl tests=24/24`. With `GATES=yes` it is
`tau_guard: PASS mode=gl tests=22/22`. Use `make WAVES=0` to leave the waveform dump out.
Run `cd test && make`, not `make -C test`: the Makefile uses `$(PWD)`.

The pytest checks of the guard run with `python -m pytest -q test/test_tau_guard.py`.
