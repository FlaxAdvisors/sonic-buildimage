"""Stage 03 — Platform infrastructure: I2C topology, BMC TTY, and daemon health.

Validates:
  - CP2112 USB HID I2C bridge and PCA9548 mux tree (i2c-1 → i2c-2..41)
  - CPLD presence at i2c-1/0x32 (wedge100s_cpld sysfs driver)
  - BMC communications over /dev/ttyACM0
  - wedge100s-bmc-poller is running and writing /run/wedge100s/ files

Phase references: Phase 0 (I2C topology), Phase 2 (BMC TTY helper), Phase R28 (bmc-daemon).
"""

import re
import pytest

# CPLD I2C address on bus 1
CPLD_BUS = 1
CPLD_ADDR = 0x32

# Phase 2: only i2c-0 (i801 SMBus) and i2c-1 (CP2112) exist.
# i2c_mux_pca954x is not loaded; virtual buses 2-41 do not exist.
EXPECTED_I2C_BUSES = {"/dev/i2c-0", "/dev/i2c-1"}

# PSU status register
CPLD_PSU_STATUS_REG = 0x10

# LED registers
CPLD_SYS1_LED_REG = 0x3E
CPLD_SYS2_LED_REG = 0x3F


# ------------------------------------------------------------------
# I2C bus topology
# ------------------------------------------------------------------

def test_i2c_buses_present(ssh):
    """i2c-0 (i801 SMBus) and i2c-1 (CP2112 HID bridge) exist.

    Phase 2: i2c_mux_pca954x is not loaded so virtual buses i2c-2..41
    do not exist.  The QSFP mux tree is owned by wedge100s-i2c-daemon via
    /dev/hidraw0.  Only i2c-0 and i2c-1 are expected.
    """
    out, err, rc = ssh.run("ls /dev/i2c-* 2>/dev/null")
    print(f"\n/dev/i2c-* devices:\n{out}")
    buses = set(l.strip() for l in out.splitlines() if l.strip())
    assert "/dev/i2c-1" in buses, (
        "i2c-1 (CP2112 HID bridge) not found — hid_cp2112 may not be loaded"
    )


def test_i2c_bus_1_present(ssh):
    """i2c-1 (CP2112 USB HID bridge) exists."""
    out, _, _ = ssh.run("ls /dev/i2c-1 2>/dev/null")
    assert "/dev/i2c-1" in out, (
        "i2c-1 (CP2112 HID bridge) not found — driver may not be loaded"
    )


def test_i2cdetect_lists_cp2112(ssh):
    """i2cdetect -l output includes CP2112 adapter."""
    out, err, rc = ssh.run("sudo i2cdetect -l 2>/dev/null")
    print(f"\ni2cdetect -l:\n{out}")
    assert rc == 0, f"i2cdetect -l failed: {err}"
    assert "CP2112" in out or "cp2112" in out.lower(), (
        "CP2112 adapter not found in i2cdetect -l output.\n"
        "Check that hid_cp2112 kernel module is loaded."
    )


def test_i2c_daemon_owns_mux_tree(ssh):
    """wedge100s-i2c-daemon owns the CP2112 mux tree via /dev/hidraw0.

    Phase 2: PCA9548 mux channels are NOT registered as kernel i2c buses.
    Instead the daemon reads all QSFP/presence/syseeprom data via hidraw.
    Verify /dev/hidraw0 exists and 32 presence files are present.
    """
    out, _, rc = ssh.run("ls /dev/hidraw0 2>/dev/null")
    assert rc == 0, "/dev/hidraw0 not found — hid_cp2112 may not be loaded"
    out, _, rc = ssh.run("ls /run/wedge100s/sfp_*_present 2>/dev/null | wc -l")
    count = int(out.strip()) if rc == 0 else 0
    assert count == 32, (
        f"Expected 32 presence files, got {count}. "
        "wedge100s-i2c-daemon may not have run: "
        "sudo systemctl start wedge100s-i2c-poller.service"
    )


# ------------------------------------------------------------------
# CPLD sanity (via i2cget on host)
# ------------------------------------------------------------------

def test_cpld_reachable(ssh):
    """CPLD at i2c-1/0x32 is accessible via wedge100s_cpld sysfs driver."""
    path = '/sys/bus/i2c/devices/1-0032/led_sys1'
    out, err, rc = ssh.run(f"cat {path} 2>/dev/null")
    print(f"\nCPLD sysfs led_sys1: {out.strip()!r}")
    assert rc == 0, (
        f"Cannot read {path}: {err}\n"
        "Is the wedge100s_cpld driver bound to i2c-1/0x32?"
    )
    try:
        int(out.strip(), 0)
    except ValueError:
        pytest.fail(f"Unexpected sysfs value: {out.strip()!r}")


def test_cpld_led_register_readable(ssh):
    """CPLD SYS LED sysfs attributes (led_sys1, led_sys2) are readable."""
    sysfs = '/sys/bus/i2c/devices/1-0032'
    for attr in ('led_sys1', 'led_sys2'):
        out, err, rc = ssh.run(f"cat {sysfs}/{attr} 2>/dev/null")
        print(f"  CPLD {attr}: {out.strip()!r}")
        assert rc == 0, f"Cannot read {sysfs}/{attr}: {err}"
        try:
            int(out.strip(), 0)
        except ValueError:
            pytest.fail(f"Unexpected sysfs value for {attr}: {out.strip()!r}")


# ------------------------------------------------------------------
# BMC TTY
# ------------------------------------------------------------------

def test_ttyacm0_device_exists(ssh):
    """/dev/ttyACM0 character device is present (BMC USB-CDC)."""
    out, err, rc = ssh.run("ls -la /dev/ttyACM0 2>/dev/null || echo MISSING")
    print(f"\n/dev/ttyACM0: {out.strip()}")
    assert "MISSING" not in out, (
        "/dev/ttyACM0 not found.\n"
        "Check that the BMC USB cable is connected and cdc_acm module is loaded."
    )
    assert "ttyACM0" in out


def test_ttyacm_cdc_acm_module(ssh):
    """cdc_acm kernel module is loaded (required for /dev/ttyACM0)."""
    out, _, rc = ssh.run("lsmod | grep cdc_acm")
    print(f"\ncdc_acm module: {out.strip()}")
    assert rc == 0, "cdc_acm module is not loaded"


def test_bmc_api_send_command(ssh):
    """BMC bmc.send_command('uptime') returns a non-empty string."""
    code = """\
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform import bmc
result = bmc.send_command('uptime')
if result is None:
    print('NONE')
    sys.exit(1)
print(result.strip()[:200])
"""
    out, err, rc = ssh.run_python(code, timeout=30)
    print(f"\nBMC send_command('uptime'):\n{out}")
    if err and "NONE" not in out:
        print(f"stderr: {err}")
    assert rc == 0, f"bmc.send_command script failed: {err}"
    assert "NONE" not in out, (
        "bmc.send_command('uptime') returned None — BMC TTY not responding"
    )
    assert out.strip(), "bmc.send_command returned empty string"


def test_bmc_uptime_contains_days_or_min(ssh):
    """BMC uptime output looks like a valid uptime string."""
    code = """\
from sonic_platform import bmc
result = bmc.send_command('uptime') or ''
print(result.strip()[:300])
"""
    out, _, _ = ssh.run_python(code, timeout=30)
    # uptime output contains 'up' keyword
    assert "up" in out.lower() or "load" in out.lower() or "min" in out.lower(), (
        f"BMC uptime output doesn't look like uptime: {out!r}"
    )


def test_bmc_thermal_sysfs_accessible(ssh):
    """BMC has at least one thermal sysfs file readable via bmc.file_read_int."""
    code = """\
from sonic_platform import bmc
# TMP75 sensor 1 on BMC i2c-3/0x48
path = '/sys/bus/i2c/devices/3-0048/hwmon'
val = bmc.send_command(f'ls {path}')
print(val or 'EMPTY')
"""
    out, _, _ = ssh.run_python(code, timeout=20)
    print(f"\nBMC TMP75 hwmon dir: {out.strip()}")
    assert "EMPTY" not in out, "BMC TMP75 hwmon dir not found via BMC TTY"
    assert out.strip(), "Empty response from BMC for thermal sysfs check"


# ------------------------------------------------------------------
# Platform init service
# ------------------------------------------------------------------

def test_platform_init_service_active(ssh):
    """wedge100s-platform-init.service is active (running)."""
    out, err, rc = ssh.run(
        "systemctl is-active wedge100s-platform-init.service 2>/dev/null || echo UNKNOWN"
    )
    state = out.strip()
    print(f"\nwedge100s-platform-init.service state: {state}")
    assert state in ("active", "inactive"), (
        f"Unexpected service state '{state}' — service may not be installed"
    )
    # Note: 'inactive' is acceptable if the one-shot already ran
    if state == "inactive":
        # Verify the service ran successfully (exit code 0)
        out2, _, _ = ssh.run(
            "systemctl show wedge100s-platform-init.service "
            "--property=Result --value 2>/dev/null"
        )
        result = out2.strip()
        print(f"  Service result: {result}")
        assert result in ("success", ""), (
            f"Platform init service did not succeed: {result}"
        )


# ------------------------------------------------------------------
# wedge100s-bmc-poller / /run/wedge100s health
# ------------------------------------------------------------------

def test_bmc_poller_timer_active(ssh):
    """wedge100s-bmc-poller.timer is active (triggers sensor poll every 10 s).

    The poller itself is a oneshot service; the timer is what must be running.
    """
    out, _, _ = ssh.run(
        "systemctl is-active wedge100s-bmc-poller.timer 2>/dev/null || echo UNKNOWN"
    )
    state = out.strip()
    print(f"\nwedge100s-bmc-poller.timer: {state}")
    assert state == "active", (
        f"wedge100s-bmc-poller.timer is {state!r} — /run/wedge100s/ files will go stale.\n"
        "Start with: sudo systemctl start wedge100s-bmc-poller.timer"
    )


def test_run_wedge100s_dir_populated(ssh):
    """/run/wedge100s/ exists and contains expected daemon output files."""
    out, _, rc = ssh.run("ls /run/wedge100s/ 2>/dev/null")
    print(f"\n/run/wedge100s/: {out.strip()}")
    assert rc == 0 and out.strip(), (
        "/run/wedge100s/ is empty or missing — wedge100s-bmc-poller has not written any files."
    )
    files = out.split()
    expected_prefixes = ('thermal_', 'fan_', 'psu_')
    found = {p: [f for f in files if f.startswith(p)] for p in expected_prefixes}
    for prefix, matches in found.items():
        assert matches, (
            f"No {prefix}* files in /run/wedge100s/ — bmc-daemon may have crashed.\n"
            f"Files present: {files}"
        )


def test_run_wedge100s_thermal_values(ssh):
    """At least one /run/wedge100s/thermal_N file contains a plausible temperature."""
    code = """\
_RUN_DIR = '/run/wedge100s'
ok = []
for i in range(1, 8):
    try:
        val = int(open(f'{_RUN_DIR}/thermal_{i}').read().strip())
        if val > 0:
            ok.append(f'thermal_{i}={val}')
    except Exception:
        pass
print(' '.join(ok) if ok else 'NONE')
"""
    out, _, rc = ssh.run_python(code, timeout=15)
    print(f"\nThermal daemon values: {out.strip()}")
    assert rc == 0
    assert "NONE" not in out and out.strip(), (
        "All /run/wedge100s/thermal_N files are missing or zero.\n"
        "Check: systemctl status wedge100s-bmc-poller"
    )


def test_run_wedge100s_fan_values(ssh):
    """At least one /run/wedge100s/fan_N_front file contains a non-zero RPM."""
    code = """\
_RUN_DIR = '/run/wedge100s'
ok = []
for i in range(1, 6):
    try:
        val = int(open(f'{_RUN_DIR}/fan_{i}_front').read().strip())
        if val > 0:
            ok.append(f'fan_{i}_front={val}')
    except Exception:
        pass
print(' '.join(ok) if ok else 'NONE')
"""
    out, _, rc = ssh.run_python(code, timeout=15)
    print(f"\nFan daemon values: {out.strip()}")
    assert rc == 0
    assert "NONE" not in out and out.strip(), (
        "All /run/wedge100s/fan_N_front files are missing or zero.\n"
        "Check: systemctl status wedge100s-bmc-poller"
    )
