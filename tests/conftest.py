"""Shared pytest fixtures for Wedge 100S-32X SONiC platform tests."""

import os
import sys
import pytest

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


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow-running")


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

    print(f"\n[target] Connecting to device using {cfg_path} …", flush=True)
    try:
        client = SSHClient(cfg_path)
        client.connect()
    except Exception as exc:
        pytest.exit(
            f"\n[target] SSH connection failed: {exc}\n"
            "Verify host/port/credentials in target.cfg and that the device is reachable.\n",
            returncode=2,
        )

    # Quick sanity — confirm the shell is alive and print a banner.
    out, _, rc = client.run("uname -n && uname -r", timeout=10)
    if rc != 0:
        client.close()
        pytest.exit(
            "\n[target] Connected but shell command returned non-zero. "
            "Check that the target is running SONiC.\n",
            returncode=2,
        )

    hostname, kernel = (out.strip().splitlines() + ["", ""])[:2]
    print(f"[target] Connected  host={hostname}  kernel={kernel}\n", flush=True)

    _SSH_CLIENT = client


def pytest_sessionfinish(session, exitstatus):
    """Close the SSH connection after all tests complete."""
    global _SSH_CLIENT
    if _SSH_CLIENT is not None:
        _SSH_CLIENT.close()
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
