"""Stage 14 module fixture — break out Ethernet4 for test, restore after."""
import time
import pytest

BREAKOUT_TEST_PORT = "Ethernet4"
RESTORE_MODE = "1x100G[40G]"
BREAKOUT_MODE = "4x25G[10G]"
# Sub-ports that appear when Ethernet4 is broken out (excluding Ethernet4 itself,
# which is the first sub-port in both broken-out and restored states)
BREAKOUT_INDICATOR_SUBPORTS = ["Ethernet5", "Ethernet6", "Ethernet7"]
POLL_INTERVAL = 3
RESTORE_TIMEOUT = 120


@pytest.fixture(scope="module", autouse=True)
def stage14_breakout_fixture(ssh):
    """Break out Ethernet4 before tests; restore to 1x100G after.

    Teardown polls COUNTERS_PORT_NAME_MAP until Ethernet5/6/7 disappear,
    preventing a race condition with stage_15 if portmgrd hasn't finished.
    """
    # Ensure clean starting state
    ssh.run(
        f"sudo config interface breakout {BREAKOUT_TEST_PORT} '{RESTORE_MODE}' -y -f",
        timeout=60,
    )
    time.sleep(5)

    yield

    # Restore Ethernet4 to 1x100G
    ssh.run(
        f"sudo config interface breakout {BREAKOUT_TEST_PORT} '{RESTORE_MODE}' -y -f",
        timeout=60,
    )

    # Wait for sub-ports to disappear from COUNTERS_PORT_NAME_MAP
    deadline = time.time() + RESTORE_TIMEOUT
    while time.time() < deadline:
        out, _, _ = ssh.run(
            "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
        )
        present = set(out.split())
        still_present = [p for p in BREAKOUT_INDICATOR_SUBPORTS if p in present]
        if not still_present:
            return
        time.sleep(POLL_INTERVAL)
    print(
        f"  [stage14 teardown] WARNING: sub-ports still in ASIC_DB after "
        f"{RESTORE_TIMEOUT}s: {still_present}"
    )
