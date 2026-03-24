"""Stage 21 module fixture — capture and restore LP_MODE state."""
import time
import pytest

NUM_PORTS = 32
RUN_DIR = "/run/wedge100s"


def _read_lpmode_states(ssh) -> dict:
    """Return {idx: '0'|'1'} for all ports that have an lpmode state file."""
    states = {}
    for idx in range(NUM_PORTS):
        out, _, rc = ssh.run(
            f"cat {RUN_DIR}/sfp_{idx}_lpmode 2>/dev/null", timeout=5
        )
        if rc == 0 and out.strip() in ("0", "1"):
            states[idx] = out.strip()
    return states


@pytest.fixture(scope="module", autouse=True)
def stage21_lpmode_fixture(ssh):
    """Save LP_MODE state before tests; restore after.

    This ensures optical TX lasers are re-enabled even if a test
    asserts lpmode=1 and fails before its own teardown runs.
    """
    original_states = _read_lpmode_states(ssh)

    yield

    # Restore: write _lpmode_req files and trigger daemon
    for idx, state in original_states.items():
        ssh.run(
            f"echo {state} > {RUN_DIR}/sfp_{idx}_lpmode_req", timeout=5
        )
    if original_states:
        ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)
        time.sleep(1)
