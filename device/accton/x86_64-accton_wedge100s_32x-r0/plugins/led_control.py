#!/usr/bin/env python3
#
# led_control.py — Platform-specific LED control for Accton Wedge 100S-32X
#
# Hardware: two system LEDs via CPLD at host i2c-1 / 0x32
#
#   SYS1  reg 0x3e  — system-status indicator (green while SONiC is running)
#   SYS2  reg 0x3f  — port-activity indicator  (green when ≥1 port is link-up)
#
# Register encoding (from ledi.c):
#   0x00 = off          0x08 = off (blinking)
#   0x01 = red          0x09 = red blinking
#   0x02 = green        0x0a = green blinking
#   0x04 = blue         0x0c = blue blinking
#
# Phase R26: writes via wedge100s_cpld sysfs attributes (led_sys1, led_sys2)
# instead of i2cset subprocess.  Sysfs path: /sys/bus/i2c/devices/1-0032/
#

try:
    from sonic_led.led_control_base import LedControlBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# CPLD sysfs attributes from wedge100s_cpld driver (Phase R26)
_CPLD_SYSFS = '/sys/bus/i2c/devices/1-0032'

# Register values
_LED_OFF   = 0x00
_LED_GREEN = 0x02


def _cpld_write(attr, val):
    """Write one byte to a wedge100s_cpld sysfs LED attribute."""
    try:
        with open('{}/{}'.format(_CPLD_SYSFS, attr), 'w') as f:
            f.write(str(val))
    except Exception:
        pass


class LedControl(LedControlBase):
    """
    LED control plugin for Accton Wedge 100S-32X.

    On init  : SYS1 = green (system running), SYS2 = off (no links yet)
    On link-up   : SYS2 → green
    On last link-down: SYS2 → off
    SYS1 remains green for the lifetime of ledd.
    """

    def __init__(self):
        self._port_states = {}          # port_name → bool (True = up)
        _cpld_write('led_sys1', _LED_GREEN)   # system running
        _cpld_write('led_sys2', _LED_OFF)     # no ports up yet

    def port_link_state_change(self, port, state):
        self._port_states[port] = (state == 'up')
        any_up = any(self._port_states.values())
        _cpld_write('led_sys2', _LED_GREEN if any_up else _LED_OFF)
