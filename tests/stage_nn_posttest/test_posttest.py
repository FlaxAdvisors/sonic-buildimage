"""Stage NN — Post-Test: Restore user config from pre-test snapshot.

Non-fatal: failures here are reported as test failures but do not affect
the exit code of stages 01–20 (those results are already recorded).

Run by: run_tests.py (injected as last stage for any stage selection).
Named stage_nn_posttest so 'n' > any digit — always sorts last regardless
of how many numbered test stages are added.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.prepost import restore_user_config, SNAPSHOT_PATH, SUITE_ACTIVE_PATH


_RESTORE_OK = None  # set by fixture, read by test


@pytest.fixture(scope="session", autouse=True)
def posttest_restore(ssh):
    """Restore config from snapshot. Runs as first thing in stage_nn_posttest."""
    global _RESTORE_OK
    _RESTORE_OK = restore_user_config(ssh, timeout=120)


def test_restore_succeeded(ssh):
    """config reload from snapshot returned True (no errors)."""
    assert _RESTORE_OK is True, (
        "restore_user_config() returned False — config reload or save failed. "
        "Check switch state manually."
    )


def test_snapshot_was_present(ssh):
    """Snapshot file is still on disk after restore."""
    out, err, rc = ssh.run(f"test -f {SNAPSHOT_PATH} && echo OK", timeout=10)
    assert rc == 0 and "OK" in out, f"Snapshot missing at {SNAPSHOT_PATH} post-restore"


def test_suite_active_marker_removed(ssh):
    """Suite active marker removed after restore."""
    out, err, rc = ssh.run(
        f"test -f {SUITE_ACTIVE_PATH} && echo EXISTS || echo GONE", timeout=5
    )
    assert "GONE" in out, "Suite active marker still present after posttest"


def test_pmon_running_after_restore(ssh):
    """pmon is active after config restore."""
    out, err, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active after restore: {out.strip()}"


def test_connected_ports_admin_up_after_restore(ssh):
    """Connected ports are admin-up after config restore."""
    connected = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
    out, err, rc = ssh.run("show interfaces status 2>&1", timeout=30)
    assert rc == 0
    for port in connected:
        line = next((l for l in out.splitlines() if port in l), None)
        assert line is not None, f"{port} not found in interfaces status"
        assert "up" in line.lower(), f"{port} is not admin-up after restore: {line}"
