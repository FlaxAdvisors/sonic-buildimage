"""Stage NN — Post-Test: Health check.

Verifies the switch is in a healthy state after all test stages complete.
Does NOT restore config — tests run against operational state established
by tools/deploy.py and should not require cleanup.
"""

import pytest
import re


def test_pmon_running(ssh):
    """pmon service is still active after all test stages."""
    out, _, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active after tests: {out.strip()}"


def test_ssh_responsive(ssh):
    """SSH shell is responsive (basic sanity)."""
    out, _, rc = ssh.run("echo pong", timeout=10)
    assert rc == 0 and "pong" in out, "SSH shell not responding"


def test_no_crashed_containers(ssh):
    """No Docker containers are in Exited or Error state."""
    out, _, rc = ssh.run(
        "docker ps --format '{{.Names}} {{.Status}}'", timeout=15
    )
    assert rc == 0, f"docker ps failed: {out}"
    crashed = [
        line for line in out.splitlines()
        if re.search(r'\b(Exited|Error)\b', line, re.IGNORECASE)
    ]
    assert not crashed, (
        f"Crashed containers detected after test run:\n"
        + "\n".join(f"  {c}" for c in crashed)
    )


def test_portchannel1_still_active(ssh):
    """PortChannel1 still present in CONFIG_DB after tests."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    assert out.strip() == "1", (
        "PortChannel1 disappeared from CONFIG_DB — a test may have deleted it"
    )
