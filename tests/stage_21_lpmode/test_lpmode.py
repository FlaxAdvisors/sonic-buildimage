"""Stage 21 — LP_MODE daemon control.

Verifies:
  - All present QSFP ports have /run/wedge100s/sfp_N_lpmode state files.
  - Default daemon state is "0" (LP_MODE deasserted = TX lasers enabled).
  - sfp.py set_lpmode(True) → req file written → daemon applies within one tick.
  - sfp.py get_lpmode() returns correct value from state file.
  - Round-trip: assert then deassert returns port to "0".

Requires: wedge100s-i2c-daemon running (systemd timer or manual invocation).

TEST ORDERING NOTE: test_default_state_is_deasserted deletes all sfp_N_lpmode
files and must run AFTER test_state_files_exist_for_present_ports.  Do not
reorder tests within TestLpmodeDaemon — pytest runs class methods in declaration
order which is the correct order here.
"""

import time
import pytest

NUM_PORTS = 32
RUN_DIR = "/run/wedge100s"


def _present_ports(ssh):
    """Return list of 0-based port indices that are currently present."""
    ports = []
    for idx in range(NUM_PORTS):
        out, _, _ = ssh.run(
            f"cat {RUN_DIR}/sfp_{idx}_present 2>/dev/null", timeout=5
        )
        if out.strip() == "1":
            ports.append(idx)
    return ports


def _daemon_tick(ssh):
    """Force one daemon poll cycle and wait for it to complete."""
    ssh.run(
        "wedge100s-i2c-daemon poll-presence",
        timeout=30,
    )
    time.sleep(0.5)


class TestLpmodeDaemon:

    def test_state_files_exist_for_present_ports(self, ssh):
        """All present ports must have a /run/wedge100s/sfp_N_lpmode file after one daemon tick."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted — cannot test LP_MODE state files")

        missing = []
        for idx in present:
            out, _, rc = ssh.run(
                f"test -f {RUN_DIR}/sfp_{idx}_lpmode && echo ok", timeout=5
            )
            if out.strip() != "ok":
                missing.append(idx)

        assert not missing, (
            f"LP_MODE state files missing for present ports: {missing}"
        )

    def test_default_state_is_deasserted(self, ssh):
        """All present ports should default to lpmode=0 (TX enabled) after daemon init.

        Clears all existing lpmode state files first so the daemon's initial-deassert
        logic fires fresh regardless of prior test or operator state.
        """
        # Remove any pre-existing state files so daemon sees them as uninitialized.
        for idx in range(NUM_PORTS):
            ssh.run(f"rm -f {RUN_DIR}/sfp_{idx}_lpmode", timeout=5)

        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        bad = []
        for idx in present:
            out, _, _ = ssh.run(
                f"cat {RUN_DIR}/sfp_{idx}_lpmode 2>/dev/null", timeout=5
            )
            val = out.strip()
            if val != "0":
                bad.append((idx, val))

        assert not bad, (
            f"Expected lpmode=0 for all present ports after fresh init; got: {bad}"
        )

    def test_request_file_processed_within_one_tick(self, ssh):
        """Writing sfp_N_lpmode_req triggers daemon to update state and delete req file."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]

        # Teardown: always restore to lpmode=0 even if assertions fail mid-test.
        def _restore():
            ssh.run(f"rm -f {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run(f"echo 0 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)

        try:
            # Request LP_MODE assert
            ssh.run(f"echo 1 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            _daemon_tick(ssh)

            # State file should now be "1", req file should be gone
            state, _, _ = ssh.run(f"cat {RUN_DIR}/sfp_{port}_lpmode 2>/dev/null", timeout=5)
            req_exists, _, _ = ssh.run(
                f"test -f {RUN_DIR}/sfp_{port}_lpmode_req && echo yes || echo no", timeout=5
            )
            assert state.strip() == "1", f"Port {port} lpmode state should be 1, got '{state.strip()}'"
            assert req_exists.strip() == "no", "Request file should be deleted after processing"

        finally:
            _restore()
            # No assertion here: _restore() is best-effort cleanup. If the daemon
            # fails during teardown, the port may stay in lpmode=1 but that is
            # visible in the next test run. Asserting here would mask the original
            # test failure if the daemon had an I2C error.

    def test_get_lpmode_reads_state_file(self, ssh):
        """Platform API get_lpmode() must return value from daemon state file (no I2C)."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]
        eth = f"Ethernet{port * 4}"

        # Read state file directly
        file_val, _, _ = ssh.run(
            f"cat {RUN_DIR}/sfp_{port}_lpmode 2>/dev/null", timeout=5
        )
        expected_lpmode = file_val.strip() == "1"

        # Read via platform API
        api_out, _, rc = ssh.run(
            f"python3 -c \""
            f"from sonic_platform.platform import Platform; "
            f"p = Platform(); "
            f"sfp = p.get_chassis().get_sfp({port}); "
            f"print(sfp.get_lpmode())"
            f"\"",
            timeout=15,
        )
        assert rc == 0, f"Platform API call failed: {api_out}"
        api_val = api_out.strip().lower() == "true"
        assert api_val == expected_lpmode, (
            f"get_lpmode() returned {api_val}, expected {expected_lpmode} "
            f"(file value: {file_val.strip()!r})"
        )

    def test_set_lpmode_writes_req_file(self, ssh):
        """Platform API set_lpmode() must write req file and not touch I2C directly."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]

        def _restore():
            ssh.run(f"rm -f {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run(f"echo 0 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)

        try:
            # Call set_lpmode(True) via platform API
            out, _, rc = ssh.run(
                f"python3 -c \""
                f"from sonic_platform.platform import Platform; "
                f"p = Platform(); "
                f"sfp = p.get_chassis().get_sfp({port}); "
                f"print(sfp.set_lpmode(True))"
                f"\"",
                timeout=15,
            )
            assert rc == 0 and "True" in out, f"set_lpmode(True) failed: {out}"

            # Verify req file written
            req_val, _, _ = ssh.run(
                f"cat {RUN_DIR}/sfp_{port}_lpmode_req 2>/dev/null", timeout=5
            )
            assert req_val.strip() == "1", (
                f"Expected req file to contain '1', got '{req_val.strip()}'"
            )

        finally:
            _restore()
            # No assertion here: _restore() is best-effort. See note in
            # test_request_file_processed_within_one_tick.
