"""Stage 03 — I2C topology and BMC TTY interface.

Validates the CP2112 USB HID I2C bridge, PCA9548 mux tree (i2c-1 → i2c-2..41),
CPLD presence at i2c-1/0x32, and BMC communications over /dev/ttyACM0.

Phase references: Phase 0 (I2C topology), Phase 2 (BMC TTY helper).
"""

import re
import pytest

# CPLD I2C address on bus 1
CPLD_BUS = 1
CPLD_ADDR = 0x32

# Expected minimum number of I2C buses after mux registration
MIN_I2C_BUSES = 40

# PSU status register
CPLD_PSU_STATUS_REG = 0x10

# LED registers
CPLD_SYS1_LED_REG = 0x3E
CPLD_SYS2_LED_REG = 0x3F


# ------------------------------------------------------------------
# I2C bus topology
# ------------------------------------------------------------------

def test_i2c_buses_present(ssh):
    """At least MIN_I2C_BUSES I2C bus devices exist under /dev."""
    out, err, rc = ssh.run("ls /dev/i2c-* 2>/dev/null")
    print(f"\n/dev/i2c-* devices:\n{out}")
    buses = [l.strip() for l in out.splitlines() if l.strip()]
    assert buses, "No /dev/i2c-* devices found — platform init may not have run"
    assert len(buses) >= MIN_I2C_BUSES, (
        f"Expected ≥{MIN_I2C_BUSES} I2C buses, found {len(buses)}: {buses}"
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


def test_i2cdetect_lists_pca9548_mux_buses(ssh):
    """i2cdetect -l shows buses registered by PCA9548 muxes."""
    out, _, _ = ssh.run("sudo i2cdetect -l 2>/dev/null")
    # PCA9548 channel buses appear as i2c-mux type
    mux_lines = [l for l in out.splitlines() if "mux" in l.lower() or "pca" in l.lower()]
    print(f"\nMux-related buses: {mux_lines}")
    assert len(mux_lines) >= 8, (
        f"Expected ≥8 PCA9548 channel buses, found {len(mux_lines)}"
    )


# ------------------------------------------------------------------
# CPLD sanity (via i2cget on host)
# ------------------------------------------------------------------

def test_cpld_reachable(ssh):
    """CPLD at i2c-1/0x32 responds to i2cget."""
    cmd = f"sudo i2cget -y {CPLD_BUS} 0x{CPLD_ADDR:02x} 0x{CPLD_PSU_STATUS_REG:02x}"
    out, err, rc = ssh.run(cmd)
    print(f"\nCPLD PSU status register (0x{CPLD_PSU_STATUS_REG:02x}): {out.strip()}")
    assert rc == 0, (
        f"i2cget to CPLD i2c-{CPLD_BUS}/0x{CPLD_ADDR:02x} failed: {err}\n"
        "Is the platform init service running?"
    )
    assert re.match(r"0x[0-9a-fA-F]{2}", out.strip()), (
        f"Unexpected i2cget output: {out.strip()}"
    )


def test_cpld_led_register_readable(ssh):
    """CPLD SYS LED registers (0x3e, 0x3f) are readable."""
    for reg in (CPLD_SYS1_LED_REG, CPLD_SYS2_LED_REG):
        cmd = f"sudo i2cget -y {CPLD_BUS} 0x{CPLD_ADDR:02x} 0x{reg:02x}"
        out, err, rc = ssh.run(cmd)
        print(f"  CPLD reg 0x{reg:02x}: {out.strip()}")
        assert rc == 0, f"Cannot read CPLD LED reg 0x{reg:02x}: {err}"


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
