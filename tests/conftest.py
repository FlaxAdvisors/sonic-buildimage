"""Shared pytest fixtures for Wedge 100S-32X SONiC platform tests."""

import logging
import os
import sys
import pytest

# Silence noisy paramiko transport/sftp INFO chatter — our tests emit their
# own structured output via print(); we don't need SSH negotiation details.
logging.getLogger("paramiko").setLevel(logging.WARNING)

sys.path.insert(0, os.path.dirname(__file__))

from lib.ssh_client import SSHClient

TARGET_CFG_DEFAULT = os.path.join(os.path.dirname(__file__), "target.cfg")

# Module-level singleton — set once in pytest_sessionstart, used by all fixtures.
_SSH_CLIENT = None  # type: SSHClient | None


def pytest_addoption(parser):
    parser.addoption(
        "--target-cfg",
        default=TARGET_CFG_DEFAULT,
        help="Path to target.cfg (default: tests/target.cfg)",
    )


class _DurationPlugin:
    """Write per-test elapsed times to tests/timing.log.

    Each line: "<elapsed_s>\t<outcome>\t<nodeid>"
    Sorted fastest-to-slowest for easy diagnosis of slow tests.
    """

    def __init__(self, config):
        self._config = config
        log_path = os.path.join(os.path.dirname(__file__), "timing.log")
        self._log = open(log_path, "w", buffering=1)  # line-buffered
        self._log.write("# elapsed   outcome  nodeid\n")

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if report.when != "call":
            return
        outcome = (
            "PASSED"  if report.passed  else
            "FAILED"  if report.failed  else
            "SKIPPED"
        )
        self._log.write(f"{report.duration:7.2f}s  {outcome:<7}  {report.nodeid}\n")

    def pytest_sessionfinish(self, session, exitstatus):
        self._log.close()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow-running")
    config.pluginmanager.register(_DurationPlugin(config), "duration_plugin")


def pytest_sessionstart(session):
    """Connect to the target once before any test is collected or run.

    Uses pytest.exit() on any failure so that no individual test is attempted
    when the target is unreachable.
    """
    global _SSH_CLIENT

    cfg_path = session.config.getoption("--target-cfg", default=TARGET_CFG_DEFAULT)

    if not os.path.exists(cfg_path):
        pytest.exit(
            f"\n[target] No config file found at: {cfg_path}\n"
            "Copy target.cfg.example to target.cfg and fill in device credentials.\n",
            returncode=2,
        )

    try:
        client = SSHClient(cfg_path)
        client.connect()
    except Exception as exc:
        pytest.exit(
            f"\n[target] SSH connection failed: {exc}\n"
            "Verify host/port/credentials in target.cfg and that the device is reachable.\n",
            returncode=2,
        )

    out, _, rc = client.run("uname -n && uname -r", timeout=10)
    if rc != 0:
        client.close()
        pytest.exit(
            "\n[target] Connected but shell command returned non-zero. "
            "Check that the target is running SONiC.\n",
            returncode=2,
        )

    # ── Assertive pre-checks ──────────────────────────────────────────────
    # These fail fast before any test collects, directing the user to
    # run tools/deploy.py if the switch is not in operational state.

    # 1. pmon running
    out, _, rc = client.run("sudo systemctl is-active pmon 2>&1", timeout=15)
    if rc != 0 or "active" not in out:
        client.close()
        pytest.exit(
            "\n[target] pmon is not active.\n"
            "Run: sudo systemctl start pmon\n",
            returncode=2,
        )

    # 2. mgmt VRF present
    out, _, rc = client.run("ip vrf show", timeout=10)
    if "mgmt" not in out:
        client.close()
        pytest.exit(
            "\n[target] mgmt VRF missing — run: tools/deploy.py\n",
            returncode=3,
        )

    # 3. Breakout sub-ports in COUNTERS_PORT_NAME_MAP (ASIC_DB DB2)
    _EXPECTED_SUBPORTS = [
        "Ethernet0","Ethernet1","Ethernet2","Ethernet3",
        "Ethernet64","Ethernet65","Ethernet66","Ethernet67",
        "Ethernet80","Ethernet81","Ethernet82","Ethernet83",
    ]
    out, _, _ = client.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
    )
    missing_subports = [p for p in _EXPECTED_SUBPORTS if p not in out.split()]
    if missing_subports:
        client.close()
        pytest.exit(
            f"\n[target] breakout sub-ports missing: {missing_subports}\n"
            "Run: tools/deploy.py --task breakout\n",
            returncode=3,
        )

    # 4. PortChannel1 present in CONFIG_DB
    out, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    if out.strip() != "1":
        client.close()
        pytest.exit(
            "\n[target] PortChannel1 missing — run: tools/deploy.py\n",
            returncode=3,
        )

    # 5. VLAN 10 and VLAN 999 present in CONFIG_DB
    out10, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'VLAN|Vlan10'", timeout=10
    )
    out999, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'VLAN|Vlan999'", timeout=10
    )
    if out10.strip() != "1" or out999.strip() != "1":
        client.close()
        pytest.exit(
            "\n[target] VLANs missing (need Vlan10 and Vlan999) — run: tools/deploy.py\n",
            returncode=3,
        )

    # Pre-checks passed — no banner; failures above already call pytest.exit()

    _SSH_CLIENT = client


def pytest_sessionfinish(session, exitstatus):
    """Close the SSH connection and run end-of-session health check."""
    global _SSH_CLIENT
    if _SSH_CLIENT is not None:
        client = _SSH_CLIENT
        # Health check
        out, _, rc = client.run("sudo systemctl is-active pmon 2>&1", timeout=10)
        if rc != 0:
            print("\n[target] WARNING: pmon is not active at session end.", flush=True)
        # Check for crashed containers
        out, _, _ = client.run(
            "docker ps --format '{{.Names}} {{.Status}}' | grep -E 'Exited|Error' || true",
            timeout=10,
        )
        if out.strip():
            print(f"\n[target] WARNING: crashed containers:\n{out.strip()}", flush=True)
        client.close()
        _SSH_CLIENT = None


@pytest.fixture(scope="session")
def ssh():
    """Return the single session-wide SSH connection to the target.

    The connection is established in pytest_sessionstart; if it failed the
    session would have already been aborted via pytest.exit().
    """
    assert _SSH_CLIENT is not None, (
        "SSH client is not initialised — this should never happen if "
        "pytest_sessionstart ran correctly."
    )
    return _SSH_CLIENT


@pytest.fixture(scope="session")
def platform_api(ssh):
    """Thin wrapper: run a Python expression against sonic_platform on the target."""
    def run_expr(expr, timeout=30):
        """
        Run a Python one-liner that imports Platform and evaluates expr.
        expr should reference `chassis` (already obtained) and may use print().
        Example: run_expr("print(chassis.get_name())")
        """
        setup = (
            "from sonic_platform.platform import Platform; "
            "chassis = Platform().get_chassis(); "
        )
        return ssh.run_python(setup + expr, timeout=timeout)

    return run_expr
