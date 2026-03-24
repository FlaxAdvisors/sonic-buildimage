#!/usr/bin/env python3
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
import tempfile
import xml.etree.ElementTree as ET

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


PRETEST_STAGE  = "stage_00_pretest"
POSTTEST_STAGE = "stage_nn_posttest"


def _inject_prepost(stage_names, inject=True):
    """Return stage list with stage_00_pretest prepended and stage_nn_posttest appended.

    If inject=False or stages already contain both bookends, returns as-is.
    Preserves order of everything in between.
    stage_nn_posttest sorts after all digit-prefixed stages (n > 9 in ASCII)
    so no custom ordering logic is needed — alphabetical sort is correct.
    """
    if not inject:
        return list(stage_names)
    result = list(stage_names)
    if PRETEST_STAGE not in result:
        result.insert(0, PRETEST_STAGE)
    if POSTTEST_STAGE not in result:
        result.append(POSTTEST_STAGE)
    return result


# ---------------------------------------------------------------------------
# --report mode: print human-readable summary tables, no pytest
# ---------------------------------------------------------------------------

def _run_report(stage_names, cfg_path):
    # --- TEMPORARY DEBUGGING: Enable verbose paramiko logging ---
    # import logging
    # logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    # paramiko_log = logging.getLogger("paramiko")
    # paramiko_log.setLevel(logging.DEBUG)
    # ------------------------------------------------------------

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

def _should_abort(tests: int, passed: int, skipped: int, xfailed: int) -> bool:
    """Return True if the stage result warrants aborting remaining stages.

    xfail counts as passed. A stage completely fails only when tests > 0,
    effective_passed == 0, and skipped < tests (i.e. some tests actually ran
    and failed or errored).
    """
    if tests == 0:
        return False
    effective_passed = passed + xfailed
    if effective_passed == 0 and skipped < tests:
        return True
    return False


def _parse_junit(xml_path: str) -> dict:
    """Parse a JUnit XML file; return counts dict with keys:
    tests, passed, failed, errored, skipped, xfailed.
    """
    try:
        tree = ET.parse(xml_path)
    except Exception:
        return dict(tests=0, passed=0, failed=0, errored=0, skipped=0, xfailed=0)

    root = tree.getroot()
    # pytest --junitxml produces a <testsuite> as root or nested under <testsuites>
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return dict(tests=0, passed=0, failed=0, errored=0, skipped=0, xfailed=0)

    tests    = int(suite.get("tests",    0))
    failures = int(suite.get("failures", 0))
    errors   = int(suite.get("errors",   0))
    skipped  = int(suite.get("skipped",  0))

    # Count xfail from individual <testcase> elements
    xfailed = sum(
        1 for tc in suite.findall("testcase")
        if tc.find("skipped") is not None
        and "xfail" in (tc.find("skipped").get("message", "") or "").lower()
    )
    # xfail appears as "skipped" in JUnit XML from pytest
    # Adjust: xfailed tests counted above were already counted in skipped
    skipped_real = skipped - xfailed

    passed = tests - failures - errors - skipped

    return dict(
        tests=tests,
        passed=passed,
        failed=failures,
        errored=errors,
        skipped=skipped_real,
        xfailed=xfailed,
    )


def _parse_junit_by_stage(xml_path: str, stage_names: list) -> dict:
    """Parse a JUnit XML from a multi-directory pytest run.

    Groups testcase results by stage directory name, extracted from the
    classname attribute (e.g. 'stage_13_link.test_link' → 'stage_13_link').
    """
    empty = dict(tests=0, passed=0, failed=0, errored=0, skipped=0, xfailed=0)
    by_stage = {s: dict(**empty) for s in stage_names}

    try:
        tree = ET.parse(xml_path)
    except Exception:
        return by_stage

    root  = tree.getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return by_stage

    for tc in suite.findall("testcase"):
        classname = tc.get("classname", "")
        stage     = classname.split(".")[0]
        if stage not in by_stage:
            continue
        c = by_stage[stage]
        c["tests"] += 1
        if tc.find("failure") is not None:
            c["failed"] += 1
        elif tc.find("error") is not None:
            c["errored"] += 1
        elif tc.find("skipped") is not None:
            msg = (tc.find("skipped").get("message", "") or "").lower()
            if "xfail" in msg:
                c["xfailed"] += 1
            else:
                c["skipped"] += 1
        else:
            c["passed"] += 1

    return by_stage


def _run_tests(stage_names, cfg_path, extra_pytest_args, inject_prepost=True):
    stage_names = _inject_prepost(stage_names, inject=inject_prepost)
    available   = set(_available_stages())
    stage_names = [s for s in stage_names if s in available]

    print("=" * 64)
    print("  Wedge 100S-32X SONiC Platform Test Suite")
    print(f"  Stages: {', '.join(stage_names)}")
    print("=" * 64)

    stage_dirs = [os.path.join(TESTS_DIR, s) for s in stage_names]

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        junit_path = tf.name

    # Single pytest invocation — one SSH connection for all stages.
    cmd = (
        [sys.executable, "-m", "pytest", "--target-cfg", cfg_path,
         f"--junitxml={junit_path}"]
        + stage_dirs
        + extra_pytest_args
    )
    result = subprocess.run(cmd, cwd=TESTS_DIR)

    by_stage = _parse_junit_by_stage(junit_path, stage_names)
    try:
        os.unlink(junit_path)
    except OSError:
        pass

    print(f"\n{'─'*64}", flush=True)
    for stage in stage_names:
        c = by_stage[stage]
        print(
            f"  {stage}: "
            f"passed={c['passed']} failed={c['failed']} "
            f"skipped={c['skipped']} xfailed={c['xfailed']}",
            flush=True,
        )

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

    no_prepost = "--no-prepost" in args
    if no_prepost:
        args.remove("--no-prepost")

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
        _run_tests(stage_names, cfg_path, extra_pytest_args, inject_prepost=not no_prepost)


if __name__ == "__main__":
    main()
