#!/usr/bin/env python3.11
"""Top-level test runner for Wedge 100S-32X SONiC platform tests.

Usage:
    ./run_tests.py                              # run all stages (pytest pass/fail)
    ./run_tests.py stage_04_thermal             # run one stage
    ./run_tests.py stage_05_fan stage_06_psu    # run specific stages
    ./run_tests.py --list                       # list available stages
    ./run_tests.py --target-cfg /path/to/cfg    # override config path

    ./run_tests.py --report                     # print summary tables for all stages
    ./run_tests.py --report stage_04_thermal    # summary table for one stage

Any extra flags after a '--' separator are forwarded to pytest verbatim:
    ./run_tests.py stage_04_thermal -- -x --no-header
"""

import glob
import os
import subprocess
import sys

TESTS_DIR   = os.path.dirname(os.path.abspath(__file__))
STAGE_GLOB  = os.path.join(TESTS_DIR, "stage_*")
CFG_DEFAULT = os.path.join(TESTS_DIR, "target.cfg")

sys.path.insert(0, TESTS_DIR)


def _available_stages():
    return sorted(
        os.path.basename(d)
        for d in glob.glob(STAGE_GLOB)
        if os.path.isdir(d)
    )


# ---------------------------------------------------------------------------
# --report mode: print human-readable summary tables, no pytest
# ---------------------------------------------------------------------------

def _run_report(stage_names, cfg_path):
    from lib.ssh_client import SSHClient
    from lib.report import REPORTERS

    if not os.path.exists(cfg_path):
        print(f"Error: config not found: {cfg_path}")
        sys.exit(2)

    print("=" * 64)
    print("  Wedge 100S-32X  --  Hardware State Report")
    print("=" * 64)
    print(f"\nConnecting to target ({cfg_path}) ...", flush=True)

    try:
        client = SSHClient(cfg_path)
        client.connect()
    except Exception as exc:
        print(f"\nError: SSH connection failed: {exc}")
        sys.exit(2)

    out, _, rc = client.run("uname -n && uname -r && date", timeout=10)
    if rc == 0:
        parts = (out.strip().splitlines() + ["", "", ""])[:3]
        print(f"Host   : {parts[0]}")
        print(f"Kernel : {parts[1]}")
        print(f"Time   : {parts[2]}")

    errors = []
    for stage in stage_names:
        reporter = REPORTERS.get(stage)
        if reporter is None:
            print(f"\n  [{stage}] No reporter defined -- skipping")
            continue
        print(f"\n{'=' * 64}")
        print(f"  {stage}")
        print("=" * 64)
        try:
            reporter(client)
        except Exception as exc:
            msg = f"Reporter raised an exception: {exc}"
            print(f"\n  [!] {msg}")
            errors.append((stage, msg))

    client.close()

    print(f"\n{'=' * 64}")
    if errors:
        print(f"  Report complete -- {len(errors)} stage(s) had errors:")
        for stage, msg in errors:
            print(f"    {stage}: {msg}")
    else:
        print(f"  Report complete -- {len(stage_names)} stage(s).")
    print("=" * 64)
    sys.exit(1 if errors else 0)


# ---------------------------------------------------------------------------
# Default mode: pytest pass/fail
# ---------------------------------------------------------------------------

def _run_tests(stage_names, cfg_path, extra_pytest_args):
    test_dirs = [os.path.join(TESTS_DIR, name) for name in stage_names]
    print("=" * 64)
    print("  Wedge 100S-32X SONiC Platform Test Suite")
    print(f"  Stages: {', '.join(stage_names)}")
    print("=" * 64)
    cmd = (
        [sys.executable, "-m", "pytest", "--target-cfg", cfg_path]
        + test_dirs
        + extra_pytest_args
    )
    result = subprocess.run(cmd, cwd=TESTS_DIR)
    sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Argument parsing + dispatch
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    # Split pytest pass-through args (after --)
    extra_pytest_args = []
    if "--" in args:
        sep = args.index("--")
        extra_pytest_args = args[sep + 1:]
        args = args[:sep]

    if "--list" in args:
        stages = _available_stages()
        print("Available stages:")
        for s in stages:
            print(f"  {s}")
        return

    report_mode = "--report" in args
    if report_mode:
        args.remove("--report")

    # Extract --target-cfg
    cfg_path  = CFG_DEFAULT
    remaining = []
    i = 0
    while i < len(args):
        if args[i] == "--target-cfg" and i + 1 < len(args):
            cfg_path = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    args = remaining

    # Resolve stage list from remaining positional args
    if args:
        available = set(_available_stages())
        stage_names = []
        for stage in args:
            name = os.path.basename(stage.rstrip("/"))
            if name not in available:
                print(f"Error: stage '{name}' not found. Use --list to see available stages.")
                sys.exit(1)
            stage_names.append(name)
    else:
        stage_names = _available_stages()
        if not stage_names:
            print(f"No stage_* directories found under {TESTS_DIR}")
            sys.exit(1)

    if report_mode:
        _run_report(stage_names, cfg_path)
    else:
        _run_tests(stage_names, cfg_path, extra_pytest_args)


if __name__ == "__main__":
    main()
