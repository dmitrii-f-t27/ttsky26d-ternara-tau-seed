#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Consistency check of NW, CLOCK_PERIOD and clock_hz (SPEC section 5).

The same numbers live in several files. This script fails when they drift:

  * ../src/project.v has exactly one `localparam integer NW = <n>;`,
    n is 0, 16 or 32, and n equals NW in tau_config.py and NW in
    ../demo/tau_seed_demo.py;
  * CLOCK_PERIOD in ../src/config.json is 40 or 100 (ns), and
    clock_hz in ../info.yaml times CLOCK_PERIOD is 1e9.

Two checks are SKIPPED, loudly, while their file is still a placeholder:

  * the demo script, while there is no ../demo directory;
  * clock_hz, while ../info.yaml still names the template module tt_um_example.

A SKIPPED item is printed in the PASS line. `--strict` turns each SKIPPED item
into a failure; use it for the final review and for CI once the demo and
info.yaml exist.

Usage (from test/):  python3 tau_check_config.py [--root DIR] [--strict]
Prints one `tau_check_config: PASS ...` or `tau_check_config: FAIL ...` line.
Standard library only; nothing is imported from the repository.
"""

import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True

VALID_NW = (0, 16, 32)
VALID_PERIOD_NS = (40, 100)
TEMPLATE_TOP = "tt_um_example"

NW_PROJECT = re.compile(r"localparam\s+integer\s+NW\s*=\s*(\d+)\s*;")
NW_PY = re.compile(r"^NW\s*=\s*(\d+)\s*(?:#.*)?$", re.MULTILINE)
CLOCK_HZ = re.compile(r"^\s*clock_hz:\s*(\d+)\s*(?:#.*)?$", re.MULTILINE)
TOP_MODULE = re.compile(r'^\s*top_module:\s*"([^"]*)"', re.MULTILINE)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _one_int(pattern, text, what, problems):
    """The single integer captured by `pattern` in `text`, else a problem and None."""
    found = pattern.findall(text)
    if len(found) != 1:
        problems.append("%s: expected exactly one match, found %d" % (what, len(found)))
        return None
    return int(found[0])


def check(root, strict=False):
    """Return (problems, values). `values` maps item name to its value or 'SKIPPED(...)'."""
    problems = []
    values = {}

    # --- NW -------------------------------------------------------------
    project_v = os.path.join(root, "src", "project.v")
    text = _read(project_v)
    nw = None
    if text is None:
        problems.append("src/project.v: cannot read")
    else:
        nw = _one_int(NW_PROJECT, text, "src/project.v localparam integer NW", problems)
        if nw is not None and nw not in VALID_NW:
            problems.append("src/project.v: NW = %d is not one of %s" % (nw, VALID_NW))
    values["NW"] = nw

    cfg_py = os.path.join(root, "test", "tau_config.py")
    text = _read(cfg_py)
    if text is None:
        problems.append("test/tau_config.py: cannot read")
    else:
        cfg_nw = _one_int(NW_PY, text, "test/tau_config.py NW", problems)
        if cfg_nw is not None and nw is not None and cfg_nw != nw:
            problems.append("test/tau_config.py NW = %d but src/project.v NW = %d" % (cfg_nw, nw))

    demo_dir = os.path.join(root, "demo")
    demo_py = os.path.join(demo_dir, "tau_seed_demo.py")
    if not os.path.isdir(demo_dir):
        values["demo"] = "SKIPPED(no demo directory yet)"
        if strict:
            problems.append("demo/tau_seed_demo.py: no demo directory (strict)")
    else:
        text = _read(demo_py)
        if text is None:
            problems.append("demo/tau_seed_demo.py: cannot read")
        else:
            demo_nw = _one_int(NW_PY, text, "demo/tau_seed_demo.py NW", problems)
            values["demo"] = demo_nw
            if demo_nw is not None and nw is not None and demo_nw != nw:
                problems.append(
                    "demo/tau_seed_demo.py NW = %d but src/project.v NW = %d" % (demo_nw, nw)
                )

    # --- clock ----------------------------------------------------------
    period = None
    text = _read(os.path.join(root, "src", "config.json"))
    if text is None:
        problems.append("src/config.json: cannot read")
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            problems.append("src/config.json: not valid JSON: %s" % exc)
        else:
            period = data.get("CLOCK_PERIOD") if isinstance(data, dict) else None
            if isinstance(period, bool) or not isinstance(period, int):
                problems.append("src/config.json: CLOCK_PERIOD missing or not an integer (%r)" % (period,))
                period = None
            elif period not in VALID_PERIOD_NS:
                problems.append(
                    "src/config.json: CLOCK_PERIOD = %d is not one of %s" % (period, VALID_PERIOD_NS)
                )
    values["CLOCK_PERIOD"] = period

    text = _read(os.path.join(root, "info.yaml"))
    if text is None:
        problems.append("info.yaml: cannot read")
        values["clock_hz"] = None
    else:
        top = TOP_MODULE.findall(text)
        if len(top) == 1 and top[0] == TEMPLATE_TOP:
            values["clock_hz"] = "SKIPPED(info.yaml still names %s)" % TEMPLATE_TOP
            if strict:
                problems.append("info.yaml: still the template (strict)")
        else:
            hz = _one_int(CLOCK_HZ, text, "info.yaml clock_hz", problems)
            values["clock_hz"] = hz
            if hz is not None and period is not None and hz * period != 10 ** 9:
                problems.append(
                    "info.yaml clock_hz = %d times CLOCK_PERIOD = %d ns is %d, expected 1000000000"
                    % (hz, period, hz * period)
                )
    return problems, values


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Check NW, CLOCK_PERIOD and clock_hz agree.")
    parser.add_argument("--root", default=os.path.dirname(here), help="repository root (default: parent of test/)")
    parser.add_argument("--strict", action="store_true", help="a SKIPPED item is a failure")
    args = parser.parse_args(argv)

    problems, values = check(args.root, args.strict)
    if problems:
        print("tau_check_config: FAIL " + "; ".join(problems))
        return 1
    print(
        "tau_check_config: PASS NW=%s CLOCK_PERIOD=%s clock_hz=%s demo=%s"
        % (values["NW"], values["CLOCK_PERIOD"], values["clock_hz"], values["demo"])
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
