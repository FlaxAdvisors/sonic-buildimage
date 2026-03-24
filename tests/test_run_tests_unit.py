"""Unit tests for run_tests.py stage injection logic."""
import sys, os
# run_tests.py lives in the tests/ directory; add it to path directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import run_tests

def test_inject_wraps_stages():
    """stage_00 prepended and stage_nn appended when running a middle stage."""
    stages = run_tests._inject_prepost(["stage_12_counters"])
    assert stages[0] == "stage_00_pretest"
    assert stages[-1] == "stage_nn_posttest"
    assert "stage_12_counters" in stages

def test_inject_full_suite_no_duplicate():
    """Full suite already has stage_00 and stage_nn — no duplicates added."""
    all_stages = [
        "stage_00_pretest", "stage_01_eeprom", "stage_12_counters",
        "stage_17_report", "stage_nn_posttest"
    ]
    stages = run_tests._inject_prepost(all_stages)
    assert stages.count("stage_00_pretest") == 1
    assert stages.count("stage_nn_posttest") == 1

def test_no_prepost_flag_skips_injection():
    stages = run_tests._inject_prepost(["stage_12_counters"], inject=False)
    assert "stage_00_pretest" not in stages
    assert "stage_nn_posttest" not in stages

def test_abort_on_all_failed():
    """All tests failed (none passed/skipped) → should abort."""
    assert run_tests._should_abort(tests=3, passed=0, skipped=0, xfailed=0) is True

def test_no_abort_when_some_passed():
    """Some passed → continue."""
    assert run_tests._should_abort(tests=3, passed=1, skipped=1, xfailed=0) is False

def test_no_abort_when_all_skipped():
    """All skipped → continue (host stage when hosts unreachable)."""
    assert run_tests._should_abort(tests=3, passed=0, skipped=3, xfailed=0) is False

def test_no_abort_when_zero_tests():
    """No tests collected → treat as skip."""
    assert run_tests._should_abort(tests=0, passed=0, skipped=0, xfailed=0) is False

def test_no_abort_when_xfail_fills_passed():
    """xfail counts as passed — stage with only xfail is not a failure."""
    assert run_tests._should_abort(tests=2, passed=0, skipped=0, xfailed=2) is False

def test_partial_failure_continues():
    """Some failed, some passed → continue."""
    assert run_tests._should_abort(tests=5, passed=2, skipped=0, xfailed=0) is False
