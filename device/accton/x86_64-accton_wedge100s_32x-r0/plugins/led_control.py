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
# ledd calls port_link_state_change(port, state) on every port up/down event.
# i2c-1 is passed into the pmon container via the /dev/i2c-1 device node
# (added to pmon.sh as part of Phase 5 / Phase 10 build integration).
#

import subprocess

try:
    from sonic_led.led_control_base import LedControlBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# CPLD i2c coordinates
_BUS      = '1'
_ADDR     = '0x32'
_REG_SYS1 = '0x3e'   # Chassis LED 1
_REG_SYS2 = '0x3f'   # Chassis LED 2

# Register values
_LED_OFF   = '0x00'
_LED_GREEN = '0x02'


def _cpld_write(reg, val):
    """Write one byte to the CPLD via i2cset; silently ignore errors."""
    try:
        subprocess.call(
            ['i2cset', '-f', '-y', _BUS, _ADDR, reg, val],
            timeout=2,
        )
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
        _cpld_write(_REG_SYS1, _LED_GREEN)   # system running
        _cpld_write(_REG_SYS2, _LED_OFF)     # no ports up yet

    def port_link_state_change(self, port, state):
        self._port_states[port] = (state == 'up')
        any_up = any(self._port_states.values())
        _cpld_write(_REG_SYS2, _LED_GREEN if any_up else _LED_OFF)
