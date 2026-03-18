"""Stage 00 — Pre-Test: Save user config and apply clean-boot template.

This stage performs the config save + reload, then verifies the resulting
state matches the clean-boot specification. Any failure calls pytest.exit()
to abort the entire test suite before any test stage runs.

Run by: run_tests.py (injected as first stage for any stage selection).
"""

import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.prepost import save_and_reload_clean, SNAPSHOT_PATH

NUM_PORTS = 32
EXPECTED_SPEED = "100000"


@pytest.fixture(scope="session", autouse=True)
def pretest_setup(ssh):
    """Save config and apply clean-boot template. Aborts suite on failure."""
    try:
        save_and_reload_clean(ssh, timeout=120)
    except Exception as exc:
        pytest.exit(
            f"\n[stage_00] Pre-test setup failed: {exc}\n"
            "Cannot continue — target is in unknown state.",
            returncode=3,
        )


def test_snapshot_exists(ssh):
    """Pre-test snapshot file was created."""
    out, err, rc = ssh.run(f"test -f {SNAPSHOT_PATH} && echo OK", timeout=10)
    assert rc == 0 and "OK" in out, f"Snapshot not found at {SNAPSHOT_PATH}"


def test_all_ports_100g(ssh):
    """All 32 ports have speed=100000 in CONFIG_DB after clean reload."""
    out, err, rc = ssh.run(
        "sonic-cfggen -d --var-json PORT 2>&1", timeout=30
    )
    assert rc == 0, f"sonic-cfggen failed: {err}"
    import json
    ports = json.loads(out)
    assert len(ports) == NUM_PORTS, f"Expected {NUM_PORTS} PORT entries, got {len(ports)}"
    wrong = {k: v.get("speed") for k, v in ports.items() if v.get("speed") != EXPECTED_SPEED}
    assert not wrong, f"Ports not at {EXPECTED_SPEED} speed: {wrong}"


def test_no_portchannel(ssh):
    """No PortChannel interfaces exist in clean state."""
    out, err, rc = ssh.run("show interfaces portchannel 2>&1", timeout=30)
    assert "PortChannel" not in out, (
        f"PortChannel found in clean state:\n{out}\n"
        "stage_16 is responsible for creating PortChannel1."
    )


def test_no_port_fec(ssh):
    """No port-level FEC is configured in clean state."""
    out, err, rc = ssh.run(
        "redis-cli -n 4 keys 'PORT|*' | xargs -I{} redis-cli -n 4 hget {} fec 2>/dev/null",
        timeout=30,
    )
    fec_values = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "none"]
    assert not fec_values, f"Unexpected FEC in clean state: {fec_values}"


def test_breakout_cfg_seeded(ssh):
    """BREAKOUT_CFG is populated for all 32 ports."""
    out, err, rc = ssh.run(
        "redis-cli -n 4 keys 'BREAKOUT_CFG|*' | wc -l", timeout=15
    )
    count = int(out.strip()) if out.strip().isdigit() else 0
    assert count >= NUM_PORTS, (
        f"BREAKOUT_CFG has {count} entries, expected >= {NUM_PORTS}"
    )


def test_pmon_running(ssh):
    """pmon service is active after config reload."""
    out, err, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active: {out.strip()}"


def test_suite_active_marker(ssh):
    """Test suite active marker file exists."""
    out, err, rc = ssh.run("test -f /run/wedge100s/test_suite_active && echo OK", timeout=5)
    assert rc == 0, "Suite active marker /run/wedge100s/test_suite_active not found"
