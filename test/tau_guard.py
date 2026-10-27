#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Guard against a false green simulation (SPEC section 6.4).

TT's own gate-level job only checks that results.xml exists and holds no
<failure> or <error>. A run with zero tests, a renamed test, a duplicated test
or a skipped test would pass that check. This guard reads the cocotb results
file and fails unless

  * the file exists and parses as XML,
  * the set of testcase names is exactly the expected set for the mode
    (test/expected_tests.json: "rtl" = 24 names, "gl" = 22 names),
  * no name occurs twice,
  * no testcase has a <failure>, <error> or <skipped> child, and no testsuite
    counts a failure, an error or a skip.

Usage:
    python3 tau_guard.py --results results.xml --expected expected_tests.json --mode rtl

It prints exactly one line and sets the exit code:
    tau_guard: PASS mode=<m> tests=<n>/<n>        exit 0
    tau_guard: FAIL <reason>                      exit 1

Standard library only.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

MODES = ("rtl", "gl")
BAD_CHILDREN = ("failure", "error", "skipped")
BAD_COUNTERS = ("failures", "errors", "skipped")
SHOW = 6  # names listed per problem before "+N more"


class GuardError(Exception):
    """A reason for FAIL that stops the check at once."""


def _names(items):
    """Short, sorted, one-line list of names."""
    items = sorted(items)
    text = ", ".join(items[:SHOW])
    if len(items) > SHOW:
        text += ", +%d more" % (len(items) - SHOW)
    return text


def load_expected(path, mode):
    """Return the list of expected testcase names for `mode`."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise GuardError("cannot read expected file %s: %s" % (path, exc))
    if not isinstance(data, dict) or mode not in data:
        raise GuardError("expected file %s has no list for mode %s" % (path, mode))
    names = data[mode]
    if not isinstance(names, list) or not names:
        raise GuardError("expected list for mode %s is empty or not a list" % mode)
    if not all(isinstance(n, str) and n for n in names):
        raise GuardError("expected list for mode %s holds a non-string or empty name" % mode)
    if len(set(names)) != len(names):
        raise GuardError("expected list for mode %s holds a duplicate name" % mode)
    return names


def load_results(path):
    """Parse the results file and return the root element."""
    if not os.path.isfile(path):
        raise GuardError("results file missing: %s" % path)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, ValueError) as exc:
        raise GuardError("results file cannot be parsed: %s" % exc)
    if root.tag not in ("testsuites", "testsuite"):
        raise GuardError("unexpected root element <%s> in results file" % root.tag)
    return root


def check(results, expected_file, mode):
    """Run all checks. Returns (ok, message, n_found, n_expected)."""
    if mode not in MODES:
        return False, "unknown mode %r" % (mode,), 0, 0
    try:
        expected = load_expected(expected_file, mode)
        root = load_results(results)
    except GuardError as exc:
        return False, str(exc), 0, 0

    cases = list(root.iter("testcase"))
    problems = []

    names = []
    for tc in cases:
        name = tc.get("name")
        if not name:
            problems.append("testcase without a name")
        else:
            names.append(name)

    if not cases:
        problems.append("0 testcases in results file")

    seen = {}
    for name in names:
        seen[name] = seen.get(name, 0) + 1
    dups = [n for n, c in seen.items() if c > 1]
    if dups:
        problems.append("duplicate testcase names: %s" % _names(dups))

    found = set(names)
    want = set(expected)
    missing = want - found
    extra = found - want
    if missing and cases:
        problems.append("missing testcases: %s" % _names(missing))
    if extra:
        problems.append("unexpected testcases: %s" % _names(extra))

    bad = []
    for tc in cases:
        kinds = sorted(set(c.tag for c in tc if c.tag in BAD_CHILDREN))
        if kinds:
            bad.append("%s (%s)" % (tc.get("name") or "?", "/".join(kinds)))
    if bad:
        problems.append("testcases with failure, error or skipped: %s" % _names(bad))

    for suite in root.iter("testsuite"):
        for attr in BAD_COUNTERS:
            raw = suite.get(attr, "0")
            try:
                count = int(raw)
            except ValueError:
                problems.append("testsuite %s has a non-numeric %s=%r" % (suite.get("name"), attr, raw))
                continue
            if count != 0:
                problems.append("testsuite %s counts %s=%d" % (suite.get("name"), attr, count))

    if problems:
        return False, "; ".join(problems), len(found), len(want)
    return True, "", len(found), len(want)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail a cocotb run that is green only by accident.")
    parser.add_argument("--results", required=True, help="cocotb results.xml")
    parser.add_argument("--expected", required=True, help="expected_tests.json")
    parser.add_argument("--mode", required=True, choices=MODES)
    args = parser.parse_args(argv)

    try:
        ok, message, n_found, n_want = check(args.results, args.expected, args.mode)
    except Exception as exc:  # any surprise is a FAIL, never a traceback-only exit
        ok, message, n_found, n_want = False, "internal error: %s: %s" % (type(exc).__name__, exc), 0, 0
    if ok:
        print("tau_guard: PASS mode=%s tests=%d/%d" % (args.mode, n_found, n_want))
        return 0
    print("tau_guard: FAIL %s" % " ".join(message.split()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
