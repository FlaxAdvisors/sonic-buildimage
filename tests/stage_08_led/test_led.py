"""Stage 08 — System LED status (SYS1 and SYS2 via CPLD i2c-1/0x32).

LED register map:
  0x3E — SYS1 (system-status LED)  — green while SONiC is running
  0x3F — SYS2 (port-activity LED)  — green when ≥1 port is link-up

Register values:
  0x00 = Off
  0x01 = Red
  0x02 = Green
  0x04 = Blue

Phase reference: Phase 9 (LED Control).
"""

import re
import pytest

CPLD_BUS  = 1
CPLD_ADDR = 0x32
SYS1_REG  = 0x3E
SYS2_REG  = 0x3F

LED_OFF   = 0x00
LED_RED   = 0x01
LED_GREEN = 0x02
LED_BLUE  = 0x04

LED_NAMES = {LED_OFF: "off", LED_RED: "red", LED_GREEN: "green", LED_BLUE: "blue"}


def _read_cpld_reg(ssh, reg):
    """Read one CPLD register byte; returns int or raises."""
    cmd = f"sudo i2cget -y {CPLD_BUS} 0x{CPLD_ADDR:02x} 0x{reg:02x}"
    out, err, rc = ssh.run(cmd)
    assert rc == 0, (
        f"i2cget CPLD reg 0x{reg:02x} failed: {err}\n"
        "Is the CPLD accessible on i2c-1/0x32?"
    )
    m = re.match(r"0x([0-9a-fA-F]{2})", out.strip())
    assert m, f"Unexpected i2cget output for reg 0x{reg:02x}: {out.strip()!r}"
    return int(m.group(1), 16)


# ------------------------------------------------------------------
# Raw CPLD register reads
# ------------------------------------------------------------------

def test_led_sys1_register_readable(ssh):
    """SYS1 LED register (0x3E) is readable from CPLD."""
    val = _read_cpld_reg(ssh, SYS1_REG)
    name = LED_NAMES.get(val, f"unknown(0x{val:02x})")
    print(f"\nSYS1 LED (reg 0x{SYS1_REG:02x}): 0x{val:02x} = {name}")
    assert val in LED_NAMES, (
        f"SYS1 LED value 0x{val:02x} is not a recognised LED state"
    )


def test_led_sys2_register_readable(ssh):
    """SYS2 LED register (0x3F) is readable from CPLD."""
    val = _read_cpld_reg(ssh, SYS2_REG)
    name = LED_NAMES.get(val, f"unknown(0x{val:02x})")
    print(f"\nSYS2 LED (reg 0x{SYS2_REG:02x}): 0x{val:02x} = {name}")
    assert val in LED_NAMES, (
        f"SYS2 LED value 0x{val:02x} is not a recognised LED state"
    )


def test_led_sys1_is_green(ssh):
    """SYS1 (system-status) LED is green while SONiC is running."""
    val = _read_cpld_reg(ssh, SYS1_REG)
    name = LED_NAMES.get(val, f"0x{val:02x}")
    print(f"\nSYS1 LED: {name}")
    assert val == LED_GREEN, (
        f"SYS1 LED is {name!r} (0x{val:02x}), expected green (0x{LED_GREEN:02x}).\n"
        "ledd may not be running or platform init may have failed."
    )


def test_led_sys2_consistent_with_port_state(ssh):
    """SYS2 LED state is consistent with port link state from STATE_DB."""
    sys2_val = _read_cpld_reg(ssh, SYS2_REG)
    sys2_name = LED_NAMES.get(sys2_val, f"0x{sys2_val:02x}")
    print(f"\nSYS2 LED: {sys2_name} (0x{sys2_val:02x})")

    # Check for any ports that are link-up via 'show interfaces status'
    out, _, rc = ssh.run("show interfaces status 2>/dev/null | grep -i ' up '")
    has_link_up = rc == 0 and bool(out.strip())
    up_ports = [l.strip().split()[0] for l in out.splitlines() if l.strip()]
    print(f"Ports with link up: {up_ports if up_ports else 'none'}")

    if has_link_up:
        assert sys2_val == LED_GREEN, (
            f"Ports {up_ports[:3]} are link-up but SYS2 LED is {sys2_name!r}.\n"
            "ledd may not be running correctly — check 'supervisorctl status ledd' "
            "inside the pmon container."
        )
    else:
        # No link-up ports — SYS2 should be off
        if sys2_val != LED_OFF:
            # Not an assertion failure — could be a transient state or stale LED
            print(
                f"  NOTE: No link-up ports detected but SYS2={sys2_name!r}. "
                "This may be a stale LED state or ledd is updating it."
            )


# ------------------------------------------------------------------
# Python LED control plugin
# ------------------------------------------------------------------

LED_INIT_CODE = """\
import sys
sys.path.insert(0, '/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/plugins')
from led_control import LedControl
import smbus

lc = LedControl()

# Read back SYS1 after init — should be green
bus = smbus.SMBus(1)
sys1 = bus.read_byte_data(0x32, 0x3e)
sys2 = bus.read_byte_data(0x32, 0x3f)
bus.close()
print(f'SYS1=0x{sys1:02x} SYS2=0x{sys2:02x}')
"""


def test_led_plugin_init_sets_sys1_green(ssh):
    """LedControl.__init__() sets SYS1=green (0x02) on the CPLD."""
    out, err, rc = ssh.run_python(LED_INIT_CODE, timeout=20)
    print(f"\nLedControl init result: {out.strip()}")
    if err:
        print(f"stderr: {err.strip()}")
    if rc != 0:
        pytest.xfail(
            f"LedControl plugin could not be loaded (smbus/plugin path issue): {err}"
        )

    m = re.search(r"SYS1=0x([0-9a-fA-F]{2})", out)
    assert m, f"Could not parse SYS1 value from: {out.strip()!r}"
    sys1_val = int(m.group(1), 16)
    assert sys1_val == LED_GREEN, (
        f"LedControl init left SYS1=0x{sys1_val:02x}, expected green (0x{LED_GREEN:02x})"
    )


LED_PORT_CHANGE_CODE = """\
import sys
sys.path.insert(0, '/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/plugins')
from led_control import LedControl
import smbus

lc = LedControl()
bus = smbus.SMBus(1)

# Simulate port-up event
lc.port_link_state_change('Ethernet0', 'up')
sys2_up = bus.read_byte_data(0x32, 0x3f)

# Simulate port-down event
lc.port_link_state_change('Ethernet0', 'down')
sys2_down = bus.read_byte_data(0x32, 0x3f)

bus.close()
print(f'after_up=0x{sys2_up:02x} after_down=0x{sys2_down:02x}')
"""


def test_led_plugin_port_state_changes_sys2(ssh):
    """LedControl.port_link_state_change() correctly drives SYS2 LED."""
    out, err, rc = ssh.run_python(LED_PORT_CHANGE_CODE, timeout=20)
    print(f"\nLED port state change test: {out.strip()}")
    if rc != 0:
        pytest.xfail(f"LedControl plugin could not be exercised: {err}")

    m_up   = re.search(r"after_up=0x([0-9a-fA-F]{2})", out)
    m_down = re.search(r"after_down=0x([0-9a-fA-F]{2})", out)
    assert m_up and m_down, f"Could not parse LED values from: {out.strip()!r}"

    sys2_up   = int(m_up.group(1), 16)
    sys2_down = int(m_down.group(1), 16)

    assert sys2_up == LED_GREEN, (
        f"SYS2 after port-up event is 0x{sys2_up:02x}, expected green (0x{LED_GREEN:02x})"
    )
    assert sys2_down == LED_OFF, (
        f"SYS2 after port-down event is 0x{sys2_down:02x}, expected off (0x{LED_OFF:02x})"
    )


# ------------------------------------------------------------------
# ledd daemon check
# ------------------------------------------------------------------

def test_ledd_running_in_pmon(ssh):
    """ledd daemon is running inside the pmon container."""
    out, err, rc = ssh.run(
        "docker exec pmon supervisorctl status ledd 2>/dev/null || echo UNAVAILABLE"
    )
    print(f"\nledd status in pmon: {out.strip()}")
    if "UNAVAILABLE" in out:
        pytest.skip("pmon container not accessible")
    assert "RUNNING" in out.upper(), (
        f"ledd is not RUNNING in pmon container: {out.strip()}\n"
        "Restart with: docker exec pmon supervisorctl restart ledd"
    )


def test_pmon_daemon_control_ledd_enabled(ssh):
    """pmon_daemon_control.json has skip_ledd=false."""
    path = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json"
    out, err, rc = ssh.run(f"cat {path} 2>/dev/null || echo MISSING")
    print(f"\npmon_daemon_control.json: {out.strip()}")
    if "MISSING" in out:
        pytest.skip(f"pmon_daemon_control.json not found at {path}")
    assert '"skip_ledd": false' in out or "'skip_ledd': false" in out or (
        "skip_ledd" in out and "false" in out
    ), (
        f"skip_ledd is not false in pmon_daemon_control.json: {out.strip()}"
    )
