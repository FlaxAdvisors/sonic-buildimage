"""Stage 15 module fixture — capture and restore FEC and admin state on TEST_PORT."""
import time
import pytest

TEST_PORT = "Ethernet4"  # disconnected 100G port, safe for FEC config-change tests


@pytest.fixture(scope="module", autouse=True)
def stage15_fec_fixture(ssh):
    """Save TEST_PORT FEC and admin_status before tests; restore after.

    Brings TEST_PORT admin=up so ASIC_DB attributes are programmed during tests.
    Restores the original admin state and FEC after all tests complete.
    """
    # Read original state
    out, _, _ = ssh.run(
        f"redis-cli -n 4 hget 'PORT|{TEST_PORT}' fec", timeout=10
    )
    original_fec = out.strip() or "none"

    out, _, _ = ssh.run(
        f"redis-cli -n 4 hget 'PORT|{TEST_PORT}' admin_status", timeout=10
    )
    original_admin = out.strip() or "down"

    # Ensure admin=up so orchagent programs ASIC_DB attributes during FEC/autoneg tests
    if original_admin != "up":
        ssh.run(f"sudo config interface startup {TEST_PORT}", timeout=15)
        time.sleep(1)

    yield

    # Restore original FEC
    ssh.run(
        f"sudo config interface fec {TEST_PORT} {original_fec}", timeout=15
    )
    # Clear any leftover autoneg/adv_speeds state
    ssh.run(
        f"redis-cli -n 4 hdel 'PORT|{TEST_PORT}' autoneg adv_speeds adv_interface_types",
        timeout=10,
    )
    # Restore original admin state
    if original_admin != "up":
        ssh.run(f"sudo config interface shutdown {TEST_PORT}", timeout=15)
    time.sleep(1)
