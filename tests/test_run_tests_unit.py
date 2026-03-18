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
