"""Stage 27 — Port LED CPLD diagnostic via bmc-daemon path.

Exercises CPLD register 0x3c (LED control) through the wedge100s-bmc-daemon
.set file dispatch. Each test writes a pattern value, waits for the daemon
to process it (single SSH call with inline poll), and asserts readback match.

Tests run in order (off → solid0-3 → rainbow → walk → passthrough) and
always end with passthrough to restore normal operation.

Visual verification column:
  These tests only confirm the register write/readback path. Visual
  confirmation of front-panel LED behavior requires a human at the device.
  See notes/led-diag-operator-guide.md for the visual checklist.
"""

import time
import pytest

RUN_DIR = "/run/wedge100s"

# (label, cpld_0x3c_value)
PATTERNS = [
    ("off",          0x00),
    ("solid_steam0", 0x80),
    ("solid_steam1", 0x90),
    ("solid_steam2", 0xA0),
    ("solid_steam3", 0xB0),
    ("rainbow",      0xE0),
    ("walk",         0x08),
    ("passthrough",  0x02),
]

# Single SSH call: snapshot mtime, write .set, poll until mtime changes, print result.
# Runs entirely on the target — no SSH round-trip per poll iteration.
# Write + readback with retry. The bmc-daemon's 10s sensor poll can
# coalesce our inotify event, so we retry the write up to 3 times.
_WRITE_AND_READ_SCRIPT = r"""
RD={run_dir}
VAL={value}
for ATTEMPT in 1 2 3; do
  OLD_MT=$(stat -c %Y $RD/cpld_led_ctrl 2>/dev/null || echo 0)
  printf '0x%02x\n' $VAL > $RD/led_ctrl_write.set
  for i in $(seq 1 24); do
    MT=$(stat -c %Y $RD/cpld_led_ctrl 2>/dev/null || echo 0)
    if [ "$MT" -gt "$OLD_MT" ]; then
      GOT=$(cat $RD/cpld_led_ctrl)
      if [ "$GOT" = "$VAL" ]; then
        echo "$GOT"
        exit 0
      fi
    fi
    sleep 0.5
  done
  sleep 2
done
echo TIMEOUT
exit 1
"""

# Trigger read + poll with retry.
_READ_SCRIPT = r"""
RD={run_dir}
F={filename}
for ATTEMPT in 1 2 3; do
  OLD_MT=$(stat -c %Y $RD/$F 2>/dev/null || echo 0)
  touch $RD/{setfile}
  for i in $(seq 1 24); do
    MT=$(stat -c %Y $RD/$F 2>/dev/null || echo 0)
    if [ "$MT" -gt "$OLD_MT" ]; then
      cat $RD/$F
      exit 0
    fi
    sleep 0.5
  done
  sleep 2
done
echo TIMEOUT
exit 1
"""


def _write_and_readback(ssh, value):
    """Write CPLD 0x3c, poll readback in one SSH call. Returns (actual_int, raw_output)."""
    script = _WRITE_AND_READ_SCRIPT.format(run_dir=RUN_DIR, value=value)
    out, err, rc = ssh.run(f"sudo bash -c '{script}'", timeout=60)
    text = out.strip()
    if rc != 0 or text == "TIMEOUT":
        return None, text
    try:
        return int(text), text
    except ValueError:
        return None, text


@pytest.fixture(scope="module", autouse=True)
def check_bmc_daemon(ssh):
    """Verify bmc-daemon is running before any LED tests."""
    out, _, _ = ssh.run("systemctl is-active wedge100s-bmc-daemon", timeout=10)
    if out.strip() != "active":
        pytest.skip(
            f"wedge100s-bmc-daemon is {out.strip()}, not active. "
            "Start with: sudo systemctl start wedge100s-bmc-daemon"
        )


@pytest.fixture(scope="module", autouse=True)
def restore_passthrough(ssh):
    """Ensure LEDs return to passthrough mode after all tests, even on failure."""
    yield
    script = _WRITE_AND_READ_SCRIPT.format(run_dir=RUN_DIR, value=0x02)
    ssh.run(f"sudo bash -c '{script}'", timeout=60)


def test_led_diag_status_readable(ssh):
    """bmc-daemon can read CPLD 0x3c via cpld_led_ctrl.set trigger."""
    script = _READ_SCRIPT.format(
        run_dir=RUN_DIR, filename="cpld_led_ctrl", setfile="cpld_led_ctrl.set"
    )
    out, err, rc = ssh.run(f"sudo bash -c '{script}'", timeout=60)
    assert rc == 0, f"Cannot read CPLD 0x3c via daemon (timeout): {err}"
    val = int(out.strip())
    print(f"\nCurrent CPLD 0x3c = 0x{val:02x}")
    assert 0 <= val <= 255


@pytest.mark.parametrize("label,value", PATTERNS, ids=[p[0] for p in PATTERNS])
def test_led_pattern_write_readback(ssh, label, value):
    """Write CPLD 0x3c pattern via bmc-daemon, verify readback matches."""
    actual, raw = _write_and_readback(ssh, value)

    print(f"\n  {label}: intended=0x{value:02x} actual={'0x%02x' % actual if actual is not None else raw}")

    assert actual is not None, (
        f"Readback timeout for {label} (0x{value:02x}). "
        f"Check: sudo journalctl -u wedge100s-bmc-daemon -n 20"
    )
    assert actual == value, (
        f"Readback mismatch for {label}: intended=0x{value:02x} actual=0x{actual:02x}"
    )


def test_led_diag_ends_in_passthrough(ssh):
    """Final state after pattern tests is passthrough (0x02)."""
    out, _, rc = ssh.run(f"sudo cat {RUN_DIR}/cpld_led_ctrl", timeout=5)
    assert rc == 0
    val = int(out.strip())
    assert val == 0x02, (
        f"CPLD 0x3c is 0x{val:02x} after tests, expected 0x02 (passthrough)"
    )


def test_led_color_register_readable(ssh):
    """bmc-daemon can read CPLD 0x3d (test color register)."""
    script = _READ_SCRIPT.format(
        run_dir=RUN_DIR, filename="cpld_led_color", setfile="led_color_read.set"
    )
    out, err, rc = ssh.run(f"sudo bash -c '{script}'", timeout=60)
    assert rc == 0, f"Cannot read CPLD 0x3d via daemon (timeout): {err}"
    val = int(out.strip())
    print(f"\nCPLD 0x3d (test color) = 0x{val:02x}")
    assert 0 <= val <= 255
