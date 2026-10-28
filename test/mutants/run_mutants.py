#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Dmitrii Fedorov
"""Mutation check of the tau seed RTL (SPEC section 6.4).

For every entry of mutants.json this script

  1. copies <design>/src and <design>/test to <work>/<id>/ (without sim_build,
     __pycache__, results*.xml and the mutants directory),
  2. applies the text edits of the mutant to the copied src; every `find` text
     must occur exactly once in its file, otherwise the mutant is INVALID (stale),
  3. runs `make sim WAVES=0` in <work>/<id>/test with the environment variable
     PWD set to that directory (the Makefile builds its source path from $(PWD)),
  4. reads that directory's results.xml.

Status of a mutant:

  KILLED    all expected test names are in results.xml, at least one test failed,
            and at least one of the killer tests listed for the mutant failed
  INVALID   edit not applicable, no results.xml, expected names missing or
            duplicated, or the build did not use the copied src
  SURVIVED  anything else (no test failed, or only unlisted tests failed)

M00 is the unmutated control. It must pass every test (tau_guard.check, mode rtl).
The mutants run one after the other, one simulation at a time. The sha256 of every
file in <design>/src, and the state of the test sources and build products in
<design>, must be the same before and after the whole run.

Usage:
    python3 run_mutants.py --work DIR [--design DIR] [--only M01,M02]
                           [--table FILE] [--json FILE] [--results-dir DIR]
                           [--diff-file FILE]

`make` and `cocotb-config` are found on PATH, with the directory of the running
python first. The last output line of a good run is `M00 PASS; 16/16 KILLED`.
Exit code: 0 good run, 1 control failed or a mutant not killed or the design
changed, 2 usage or configuration error.

Standard library only.
"""

import argparse
import difflib
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DESIGN = os.path.dirname(os.path.dirname(HERE))
ID_RE = re.compile(r"^M[0-9]{2}$")
CONTROL = "M00"

# Not copied into a mutant work directory (by name, anywhere in the tree)
SKIP_NAMES = ("sim_build", "__pycache__", ".pytest_cache", ".DS_Store")
SKIP_GLOBS = ("results*.xml", "._*", "*.fst", "*.vcd")
# Not copied from the top level of test/ only
SKIP_TOP_TEST = ("mutants",)
# Build products: compared by size and mtime, not by content
BUILD_NAMES = ("sim_build", "__pycache__", ".pytest_cache")
BUILD_GLOBS = ("results*.xml", "*.fst", "*.vcd")
# Environment variables that must not leak into the build
DROP_ENV = ("GATES", "WAVES", "SIM", "SIM_BUILD", "TOPLEVEL", "TOPLEVEL_LANG", "VERILOG_SOURCES", "COMPILE_ARGS")

DEFAULT_TIMEOUT_S = 900


class Invalid(Exception):
    """The mutant cannot be evaluated (stale edit, no results, ...)."""


class ConfigError(Exception):
    """The configuration (mutants.json, paths) is wrong: exit code 2."""


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def is_junk(name):
    return name == ".DS_Store" or name.startswith("._")


def matches(name, names, globs):
    return name in names or any(fnmatch.fnmatch(name, g) for g in globs)


def copy_tree(src, dst, skip_top=()):
    """Copy the content of src to dst. Plain data copy: no attributes, no xattrs."""
    os.makedirs(dst)
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        top = rel == "."
        keep = []
        for d in sorted(dirnames):
            if matches(d, SKIP_NAMES, SKIP_GLOBS) or (top and d in skip_top):
                continue
            keep.append(d)
        dirnames[:] = keep
        target = dst if top else os.path.join(dst, rel)
        for d in keep:
            os.makedirs(os.path.join(target, d), exist_ok=True)
        for fn in sorted(filenames):
            if matches(fn, SKIP_NAMES, SKIP_GLOBS):
                continue
            shutil.copyfile(os.path.join(dirpath, fn), os.path.join(target, fn))


def src_hashes(design):
    """{file name: sha256} for every file in <design>/src."""
    root = os.path.join(design, "src")
    out = {}
    for fn in sorted(os.listdir(root)):
        path = os.path.join(root, fn)
        if os.path.isfile(path) and not is_junk(fn):
            out[fn] = sha256_file(path)
    return out


def design_state(design):
    """Everything this run must leave alone: sha256 of the sources in src/ and test/,
    size and mtime of the build products in test/ (sim_build, results*.xml, ...)."""
    state = {}
    for sub in ("src", "test"):
        root = os.path.join(design, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                if is_junk(fn):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, design)
                parts = rel.split(os.sep)
                build = any(p in BUILD_NAMES for p in parts) or any(fnmatch.fnmatch(fn, g) for g in BUILD_GLOBS)
                if build:
                    st = os.stat(path)
                    state[rel] = "build size=%d mtime_ns=%d" % (st.st_size, st.st_mtime_ns)
                else:
                    state[rel] = "sha256=" + sha256_file(path)
    return state


def state_diff(before, after):
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def load_mutants(path, expected_names):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    mutants = data.get("mutants") if isinstance(data, dict) else None
    if not isinstance(mutants, list) or not mutants:
        raise ConfigError("%s has no mutants list" % path)
    seen = set()
    for m in mutants:
        mid = m.get("id", "")
        if not ID_RE.match(mid):
            raise ConfigError("bad mutant id %r" % (mid,))
        if mid in seen:
            raise ConfigError("duplicate mutant id %s" % mid)
        seen.add(mid)
        if not isinstance(m.get("edits"), list) or not isinstance(m.get("killers"), list):
            raise ConfigError("%s: edits and killers must be lists" % mid)
        if (mid == CONTROL) != (not m["edits"]):
            raise ConfigError("%s: only the control %s may have no edits" % (mid, CONTROL))
        if mid != CONTROL and not m["killers"]:
            raise ConfigError("%s: no killer test listed" % mid)
        if mid == CONTROL and m["killers"]:
            raise ConfigError("%s: the control has no killers" % mid)
        for k in m["killers"]:
            if k not in expected_names:
                raise ConfigError("%s: killer %s is not in expected_tests.json" % (mid, k))
        for e in m["edits"]:
            if not all(isinstance(e.get(key), str) and e.get(key) != "" for key in ("file", "find")) \
                    or not isinstance(e.get("replace"), str):
                raise ConfigError("%s: every edit needs file, find and replace" % mid)
            if e["file"] != os.path.basename(e["file"]):
                raise ConfigError("%s: edit file %r must be a plain file name in src/" % (mid, e["file"]))
    return mutants


def load_guard(design):
    """Import test/tau_guard.py of the design under test."""
    path = os.path.join(design, "test", "tau_guard.py")
    spec = importlib.util.spec_from_file_location("tau_guard_for_mutants", path)
    if spec is None or spec.loader is None:
        raise ConfigError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# one mutant
# ---------------------------------------------------------------------------

def apply_edits(srcdir, edits):
    """Apply the edits to the copied src. Returns {file: (old text, new text)}."""
    texts = {}
    originals = {}
    for e in edits:
        name = e["file"]
        if name not in texts:
            path = os.path.join(srcdir, name)
            if not os.path.isfile(path):
                raise Invalid("edit file %s not in src" % name)
            with open(path, "r", encoding="utf-8", newline="") as f:
                texts[name] = f.read()
            originals[name] = texts[name]
        count = texts[name].count(e["find"])
        if count != 1:
            raise Invalid("stale edit: %r occurs %d times in %s, expected exactly 1" % (e["find"], count, name))
        texts[name] = texts[name].replace(e["find"], e["replace"], 1)
    for name in texts:
        if texts[name] == originals[name]:
            raise Invalid("edit leaves %s unchanged" % name)
        with open(os.path.join(srcdir, name), "w", encoding="utf-8", newline="") as f:
            f.write(texts[name])
    return {name: (originals[name], texts[name]) for name in texts}


def parse_results(path):
    """Return (names, failed names). Raises Invalid if the file is missing or unreadable."""
    if not os.path.isfile(path):
        raise Invalid("no results.xml")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise Invalid("results.xml cannot be parsed: %s" % exc)
    names = []
    failed = []
    for tc in root.iter("testcase"):
        name = tc.get("name") or ""
        names.append(name)
        if any(c.tag in ("failure", "error") for c in tc):
            failed.append(name)
    return names, failed


def build_env(testdir, bindir):
    env = dict(os.environ)
    for key in list(env):
        if key in DROP_ENV or key.startswith("COCOTB_"):
            del env[key]
    env["PWD"] = testdir
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_one(m, design, work, expected, bindir, make, timeout, guard, expected_file):
    """Run one mutant. Returns a result dict."""
    mid = m["id"]
    res = {
        "id": mid,
        "description": m["description"],
        "status": "INVALID",
        "note": "",
        "killers": list(m["killers"]),
        "killed_by": [],
        "other_failed": [],
        "failed": 0,
        "tests": 0,
        "seconds": 0.0,
        "make_exit": None,
        "diff": "",
    }
    root = os.path.join(work, mid)
    if os.path.exists(root):
        shutil.rmtree(root)
    srcdir = os.path.join(root, "src")
    testdir = os.path.join(root, "test")
    t0 = time.time()
    try:
        copy_tree(os.path.join(design, "src"), srcdir)
        copy_tree(os.path.join(design, "test"), testdir, skip_top=SKIP_TOP_TEST)
        # the copy of src must equal the design src before any edit
        if src_hashes(design) != src_hashes(root):
            raise Invalid("copied src differs from the design src before the edit")
        changed = apply_edits(srcdir, m["edits"]) if m["edits"] else {}
        diff = []
        for name in sorted(changed):
            old, new = changed[name]
            diff.extend(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                             "a/src/" + name, "b/src/" + name, lineterm="", n=2))
        res["diff"] = "\n".join(diff)

        log_path = os.path.join(root, "make.log")
        env = build_env(testdir, bindir)
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen([make, "sim", "WAVES=0"], cwd=testdir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                res["make_exit"] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)  # make and the simulator it started
                proc.wait()
                raise Invalid("make sim timed out after %d s" % timeout)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            log_text = f.read()
        # the simulator must have been built from the copied src
        needle = os.path.join(testdir, "..", "src", "project.v")
        if needle not in log_text:
            raise Invalid("make log does not show the copied src being compiled")

        names, failed = parse_results(os.path.join(testdir, "results.xml"))
        res["tests"] = len(names)
        res["failed"] = len(failed)
        dups = sorted(set(n for n in names if names.count(n) > 1))
        if dups:
            raise Invalid("duplicate test names: " + ", ".join(dups))
        missing = sorted(set(expected) - set(names))
        if missing:
            raise Invalid("missing test names: " + ", ".join(missing))

        if mid == CONTROL:
            ok, message, n_found, n_want = guard.check(os.path.join(testdir, "results.xml"), expected_file, "rtl")
            res["status"] = "PASS" if ok else "FAIL"
            res["note"] = "%d/%d tests passed" % (n_found - len(failed), n_want) if ok else message
        else:
            res["killed_by"] = [k for k in m["killers"] if k in failed]
            res["other_failed"] = [n for n in failed if n not in m["killers"]]
            if failed and res["killed_by"]:
                res["status"] = "KILLED"
            else:
                res["status"] = "SURVIVED"
                if failed:
                    res["note"] = "failed only in unlisted tests"
                else:
                    res["note"] = "all tests passed"
    except Invalid as exc:
        res["status"] = "INVALID"
        res["note"] = str(exc)
    res["seconds"] = round(time.time() - t0, 1)
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def short(name):
    return name[5:] if name.startswith("test_") else name


def table(results):
    lines = [
        "| Id | Mutation | Status | Killed by (listed killers that failed) | Other failing tests | Failed / run | Seconds |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["id"] == CONTROL:
            killed = "n/a (control)"
            other = ", ".join(short(n) for n in r["other_failed"]) or "none"
            note = r["note"]
        else:
            killed = ", ".join(short(n) for n in r["killed_by"]) or "-"
            other = ", ".join(short(n) for n in r["other_failed"]) or "none"
            note = r["note"]
        desc = r["description"] + ("; " + note if note and r["status"] in ("INVALID", "SURVIVED", "FAIL") else "")
        lines.append("| %s | %s | %s | %s | %s | %d / %d | %.1f |" % (
            r["id"], desc.replace("|", "/"), r["status"], killed, other, r["failed"], r["tests"], r["seconds"]))
    return "\n".join(lines)


def summarize(results):
    control = [r for r in results if r["id"] == CONTROL]
    mutants = [r for r in results if r["id"] != CONTROL]
    killed = sum(1 for r in mutants if r["status"] == "KILLED")
    survived = sum(1 for r in mutants if r["status"] == "SURVIVED")
    invalid = sum(1 for r in results if r["status"] == "INVALID")
    control_ok = bool(control) and control[0]["status"] == "PASS"
    control_text = "PASS" if control_ok else ("FAIL" if control else "NOT RUN")
    line = "%s %s; %d/%d KILLED" % (CONTROL, control_text, killed, len(mutants))
    if survived:
        line += "; %d SURVIVED" % survived
    if invalid:
        line += "; %d INVALID" % invalid
    good = (not control or control_ok) and killed == len(mutants) and invalid == 0
    return line, good


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the RTL mutants against the cocotb tests.")
    parser.add_argument("--work", required=True, help="scratch directory for the mutant copies (outside the design)")
    parser.add_argument("--design", default=DEFAULT_DESIGN, help="design directory with src/ and test/ (default: parent of test/)")
    parser.add_argument("--mutants", default=os.path.join(HERE, "mutants.json"), help="mutant list (default: mutants.json next to this script)")
    parser.add_argument("--only", default="", help="comma separated mutant ids to run (default: all)")
    parser.add_argument("--table", default="", help="write the result table (markdown) to this file")
    parser.add_argument("--json", default="", help="write the results as JSON to this file")
    parser.add_argument("--results-dir", default="", help="copy every mutant's results.xml here as <id>-results.xml")
    parser.add_argument("--diff-file", default="", help="write the unified diff of every mutation to this file")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds allowed for one `make sim`")
    parser.add_argument("--make", default="make", help="make program")
    args = parser.parse_args(argv)

    design = os.path.abspath(args.design)
    work = os.path.abspath(args.work)
    try:
        if not os.path.isdir(os.path.join(design, "src")) or not os.path.isdir(os.path.join(design, "test")):
            raise ConfigError("%s has no src/ and test/" % design)
        for sub in ("src", "test"):
            base = os.path.join(design, sub)
            if work == base or work.startswith(base + os.sep):
                raise ConfigError("--work %s must not be inside %s" % (work, base))
        if work == design or design.startswith(work + os.sep):
            raise ConfigError("--work %s must not contain the design" % work)
        expected_file = os.path.join(design, "test", "expected_tests.json")
        with open(expected_file, "r", encoding="utf-8") as f:
            expected = json.load(f)["rtl"]
        mutants = load_mutants(args.mutants, expected)
        guard = load_guard(design)
        if args.only:
            wanted = [s.strip() for s in args.only.split(",") if s.strip()]
            unknown = [w for w in wanted if w not in [m["id"] for m in mutants]]
            if unknown:
                raise ConfigError("unknown mutant id(s): " + ", ".join(unknown))
            mutants = [m for m in mutants if m["id"] in wanted]
    except (ConfigError, OSError, ValueError, KeyError) as exc:
        print("run_mutants: ERROR %s" % exc)
        return 2

    bindir = os.path.dirname(os.path.abspath(sys.executable))
    os.makedirs(work, exist_ok=True)
    before_src = src_hashes(design)
    before_state = design_state(design)
    print("design: %s" % design)
    print("work:   %s" % work)
    print("src sha256 before:")
    for name, digest in before_src.items():
        print("  %s  src/%s" % (digest, name))
    sys.stdout.flush()

    results = []
    for m in mutants:
        r = run_one(m, design, work, expected, bindir, args.make, args.timeout, guard, expected_file)
        results.append(r)
        detail = r["note"]
        if r["id"] != CONTROL and r["killed_by"]:
            detail = "killed by " + ", ".join(short(n) for n in r["killed_by"])
        print("%s %-8s failed %d/%d, make exit %s, %.1f s  %s" % (
            r["id"], r["status"], r["failed"], r["tests"], r["make_exit"], r["seconds"], detail))
        sys.stdout.flush()
        if args.results_dir:
            src_xml = os.path.join(work, r["id"], "test", "results.xml")
            if os.path.isfile(src_xml):
                os.makedirs(args.results_dir, exist_ok=True)
                shutil.copyfile(src_xml, os.path.join(args.results_dir, "%s-results.xml" % r["id"]))

    after_src = src_hashes(design)
    after_state = design_state(design)
    print("src sha256 after:")
    for name, digest in after_src.items():
        print("  %s  src/%s" % (digest, name))
    src_same = before_src == after_src
    changed = state_diff(before_state, after_state)
    print("src sha256 unchanged: %s" % ("yes" if src_same else "NO"))
    print("design src/ and test/ untouched (sources by sha256, build products by size and mtime): %s"
          % ("yes" if not changed else "NO, changed: " + ", ".join(changed)))

    print()
    print(table(results))
    print()
    line, good = summarize(results)
    good = good and src_same and not changed

    if args.table:
        with open(args.table, "w", encoding="utf-8") as f:
            f.write(table(results) + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": line, "src_sha256_before": before_src, "src_sha256_after": after_src,
                       "src_unchanged": src_same, "design_untouched": not changed, "results": results},
                      f, indent=2, sort_keys=True)
            f.write("\n")
    if args.diff_file:
        with open(args.diff_file, "w", encoding="utf-8") as f:
            for r in results:
                f.write("### %s: %s\n%s\n\n" % (r["id"], r["description"], r["diff"] or "(no change)"))

    print(line)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
