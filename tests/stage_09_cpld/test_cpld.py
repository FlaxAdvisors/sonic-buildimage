"""Stage 09 — CPLD Sysfs Attributes.

Verifies that the wedge100s_cpld kernel driver is loaded and all sysfs
attributes under /sys/bus/i2c/devices/1-0032/ are readable with values
in the expected ranges.

Sysfs attributes (defined in wedge100s_cpld.c):
  cpld_version  (RO) — "major.minor" from regs 0x00/0x01
  psu1_present  (RO) — 0=absent, 1=present
  psu1_pgood    (RO) — 0=not OK, 1=power good
  psu2_present  (RO) — 0=absent, 1=present
  psu2_pgood    (RO) — 0=not OK, 1=power good
  led_sys1      (RW) — 0=off, 1=red, 2=green, 4=blue; +8=blink
  led_sys2      (RW) — same encoding

CPLD hardware: i2c-1/0x32, accessed via CP2112 USB-HID bridge (i2c_dev).
"""

import re
import pytest

# Kernel driver sysfs path — used only for driver-binding tests
CPLD_SYSFS = "/sys/bus/i2c/devices/1-0032"

# Canonical daemon-cache path — used by all services (psu.py, chassis.py, ledd)
RUN_DIR = "/run/wedge100s"

SYSFS_ATTRS = [
    "cpld_version",
    "psu1_present",
    "psu1_pgood",
    "psu2_present",
    "psu2_pgood",
    "led_sys1",
    "led_sys2",
]


# ------------------------------------------------------------------
# Driver presence
# ------------------------------------------------------------------

def test_cpld_sysfs_dir_exists(ssh):
    """wedge100s_cpld sysfs directory exists at /sys/bus/i2c/devices/1-0032."""
    out, _, rc = ssh.run(f"test -d {CPLD_SYSFS} && echo YES || echo NO", timeout=10)
    assert "YES" in out, (
        f"{CPLD_SYSFS} does not exist.\n"
        "Check: lsmod | grep wedge100s_cpld"
    )


def test_cpld_driver_name(ssh):
    """CPLD device shows driver=wedge100s_cpld in sysfs."""
    out, _, rc = ssh.run(
        f"readlink {CPLD_SYSFS}/driver 2>/dev/null | xargs basename", timeout=10
    )
    driver = out.strip()
    print(f"\nCPLD driver: {driver!r}")
    assert driver == "wedge100s_cpld", (
        f"Expected driver 'wedge100s_cpld', got {driver!r}.\n"
        "Check: lsmod | grep wedge100s_cpld"
    )


# ------------------------------------------------------------------
# All attributes readable
# ------------------------------------------------------------------

def test_all_sysfs_attrs_readable(ssh):
    """All 7 CPLD sysfs attributes are readable without error."""
    missing = []
    for attr in SYSFS_ATTRS:
        path = f"{CPLD_SYSFS}/{attr}"
        out, err, rc = ssh.run(f"cat {path} 2>&1", timeout=10)
        if rc != 0 or "No such file" in out or "Permission denied" in out:
            missing.append(f"{attr}: {out.strip() or err.strip()}")
        else:
            print(f"  {attr}: {out.strip()!r}")
    assert not missing, (
        f"CPLD sysfs attributes not readable:\n" + "\n".join(missing)
    )


# ------------------------------------------------------------------
# Daemon cache (/run/wedge100s/)
# ------------------------------------------------------------------

def test_cpld_run_dir_populated(ssh):
    """All CPLD attrs are present in /run/wedge100s/ (daemon cache).

    wedge100s-i2c-daemon copies each wedge100s_cpld sysfs attribute to
    /run/wedge100s/ on every 3-second poll tick.  This test verifies that
    the cache is populated so that psu.py and chassis.py can read from
    the canonical daemon-cache path.
    """
    missing = []
    for attr in SYSFS_ATTRS:
        path = f"{RUN_DIR}/{attr}"
        out, err, rc = ssh.run(f"cat {path} 2>&1", timeout=10)
        if rc != 0 or "No such file" in out or "Permission denied" in out:
            missing.append(f"{attr}: {out.strip() or err.strip()}")
        else:
            print(f"  {RUN_DIR}/{attr}: {out.strip()!r}")
    assert not missing, (
        f"CPLD attrs missing from {RUN_DIR}:\n" + "\n".join(missing) + "\n"
        "Is wedge100s-i2c-daemon running? Check: systemctl status wedge100s-i2c-poller.timer"
    )


# ------------------------------------------------------------------
# cpld_version format
# ------------------------------------------------------------------

def test_cpld_version_format(ssh):
    """cpld_version daemon cache reads as 'major.minor' with both fields numeric."""
    out, _, rc = ssh.run(f"cat {RUN_DIR}/cpld_version", timeout=10)
    assert rc == 0, f"Could not read {RUN_DIR}/cpld_version"
    version = out.strip()
    print(f"\ncpld_version: {version!r}")
    m = re.match(r"^(\d+)\.(\d+)$", version)
    assert m, (
        f"cpld_version format unexpected: {version!r} (expected 'N.N')"
    )
    major = int(m.group(1))
    minor = int(m.group(2))
    assert 0 <= major <= 255, f"cpld_version major={major} out of range [0, 255]"
    assert 0 <= minor <= 255, f"cpld_version minor={minor} out of range [0, 255]"
    print(f"  major={major} minor={minor}")


# ------------------------------------------------------------------
# PSU attributes
# ------------------------------------------------------------------

def _read_int_attr(ssh, attr):
    """Read a CPLD integer attribute from the daemon cache; return int or raise."""
    out, _, rc = ssh.run(f"cat {RUN_DIR}/{attr}", timeout=10)
    assert rc == 0, f"Could not read {RUN_DIR}/{attr} — has wedge100s-i2c-daemon run?"
    return int(out.strip(), 0)


def test_psu1_present_valid(ssh):
    """psu1_present is 0 or 1."""
    val = _read_int_attr(ssh, "psu1_present")
    print(f"\npsu1_present: {val}")
    assert val in (0, 1), f"psu1_present={val}, expected 0 or 1"


def test_psu1_pgood_valid(ssh):
    """psu1_pgood is 0 or 1."""
    val = _read_int_attr(ssh, "psu1_pgood")
    print(f"\npsu1_pgood: {val}")
    assert val in (0, 1), f"psu1_pgood={val}, expected 0 or 1"


def test_psu2_present_valid(ssh):
    """psu2_present is 0 or 1."""
    val = _read_int_attr(ssh, "psu2_present")
    print(f"\npsu2_present: {val}")
    assert val in (0, 1), f"psu2_present={val}, expected 0 or 1"


def test_psu2_pgood_valid(ssh):
    """psu2_pgood is 0 or 1."""
    val = _read_int_attr(ssh, "psu2_pgood")
    print(f"\npsu2_pgood: {val}")
    assert val in (0, 1), f"psu2_pgood={val}, expected 0 or 1"


def test_psu_pgood_implies_present(ssh):
    """A PSU that is power-good must also be present.

    pgood=1 and present=0 is physically impossible; indicates a CPLD read error.
    """
    for n in (1, 2):
        present = _read_int_attr(ssh, f"psu{n}_present")
        pgood   = _read_int_attr(ssh, f"psu{n}_pgood")
        print(f"  PSU{n}: present={present} pgood={pgood}")
        if pgood == 1:
            assert present == 1, (
                f"PSU{n}: pgood=1 but present=0 — physically impossible"
            )


# ------------------------------------------------------------------
# LED attributes
# ------------------------------------------------------------------

# Valid LED values: 0=off, 1=red, 2=green, 4=blue; any of these +8=blink
LED_VALID = {0, 1, 2, 4, 8, 9, 10, 12}


def test_led_sys1_valid(ssh):
    """led_sys1 value is a valid LED encoding."""
    val = _read_int_attr(ssh, "led_sys1")
    print(f"\nled_sys1: {val} (0x{val:02x})")
    assert val in LED_VALID, (
        f"led_sys1={val} is not a valid LED value {sorted(LED_VALID)}"
    )


def test_led_sys2_valid(ssh):
    """led_sys2 value is a valid LED encoding."""
    val = _read_int_attr(ssh, "led_sys2")
    print(f"\nled_sys2: {val} (0x{val:02x})")
    assert val in LED_VALID, (
        f"led_sys2={val} is not a valid LED value {sorted(LED_VALID)}"
    )


def test_led_sys2_write_restore(ssh):
    """led_sys2 is writable; write restores original value correctly.

    Writes to the kernel sysfs attr (hardware effect) and verifies the
    readback via the daemon cache (/run/wedge100s/led_sys2).  The restore
    writes back to both paths so that the cache matches hardware state.
    """
    sysfs_path  = f"{CPLD_SYSFS}/led_sys2"
    run_path    = f"{RUN_DIR}/led_sys2"

    # Read original from daemon cache (canonical read path)
    out, _, rc = ssh.run(f"cat {run_path}", timeout=10)
    assert rc == 0, f"Could not read {run_path}"
    original = int(out.strip(), 0)
    print(f"\nled_sys2 original: {original}")

    # Pick a write target that differs from the original
    test_val = 2 if original != 2 else 1  # green or red

    try:
        # Write test value to hardware (sysfs) and cache
        _, _, rc = ssh.run(
            f"echo {test_val} | sudo tee {sysfs_path} > /dev/null && "
            f"echo {test_val} | sudo tee {run_path} > /dev/null",
            timeout=10,
        )
        assert rc == 0, f"Write to led_sys2 failed (rc={rc})"

        # Read back from daemon cache (canonical path)
        out, _, rc = ssh.run(f"cat {run_path}", timeout=10)
        assert rc == 0
        readback = int(out.strip(), 0)
        print(f"  Wrote {test_val}, read back {readback}")
        assert readback == test_val, (
            f"led_sys2 write/read mismatch: wrote {test_val}, got {readback}"
        )
    finally:
        # Restore original to both paths
        ssh.run(
            f"echo {original} | sudo tee {sysfs_path} > /dev/null && "
            f"echo {original} | sudo tee {run_path} > /dev/null",
            timeout=10,
        )
        print(f"  Restored led_sys2 to {original}")
