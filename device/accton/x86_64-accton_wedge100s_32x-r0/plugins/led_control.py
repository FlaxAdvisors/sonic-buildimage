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
# Writes to /run/wedge100s/led_sys{1,2}; wedge100s-i2c-daemon applies those
# values to the wedge100s_cpld sysfs attributes on its 3-second poll tick.
#

import os

try:
    from sonic_led.led_control_base import LedControlBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# wedge100s-i2c-daemon applies writes to CPLD sysfs on its 3-second tick.
_RUN_DIR = '/run/wedge100s'

# Register values
_LED_OFF   = 0x00
_LED_GREEN = 0x02

# Interface → LEDUP1 DATA_RAM index (LED_port = (first_serdes - 1) / 4).
# Must be kept in sync with _IFACE_TO_LED_PORT in wedge100s-ledup-linkstate.
# Derived from th-wedge100s-32x-flex.config.bcm portmap entries.
_IFACE_TO_LED_PORT = {
    'Ethernet0':   29,
    'Ethernet4':   28,
    'Ethernet8':   31,
    'Ethernet12':  30,
    'Ethernet16':   1,
    'Ethernet20':   0,
    'Ethernet24':   3,
    'Ethernet28':   2,
    'Ethernet32':   5,
    'Ethernet36':   4,
    'Ethernet40':   7,
    'Ethernet44':   6,
    'Ethernet48':   9,
    'Ethernet52':   8,
    'Ethernet56':  11,
    'Ethernet60':  10,
    'Ethernet64':  13,
    'Ethernet68':  12,
    'Ethernet72':  15,
    'Ethernet76':  14,
    'Ethernet80':  17,
    'Ethernet84':  16,
    'Ethernet88':  19,
    'Ethernet92':  18,
    'Ethernet96':  21,
    'Ethernet100': 20,
    'Ethernet104': 23,
    'Ethernet108': 22,
    'Ethernet112': 25,
    'Ethernet116': 24,
    'Ethernet120': 27,
    'Ethernet124': 26,
}


def _led_write(attr, val):
    """Write LED value to /run/wedge100s/; daemon handles CPLD write-through."""
    try:
        os.makedirs(_RUN_DIR, exist_ok=True)
        with open('{}/{}'.format(_RUN_DIR, attr), 'w') as f:
            f.write('{}\n'.format(val))
    except Exception:
        pass


def _state_db_port_states():
    """
    Read current netdev_oper_status for all PORT_TABLE entries from STATE_DB.
    Returns a dict of {port_name: bool} (True = up).
    Falls back to an empty dict if swsscommon is unavailable.

    SONiC's ledd only fires port_link_state_change() on transitions, so
    without this initial scan SYS2 stays off whenever ledd starts after
    ports are already up (e.g. after a pmon restart).
    """
    try:
        from swsscommon.swsscommon import DBConnector, Table
        db  = DBConnector('STATE_DB', 0)
        tbl = Table(db, 'PORT_TABLE')
        states = {}
        for key in tbl.getKeys():
            _, fvs = tbl.get(key)
            for field, value in fvs:
                if field == 'netdev_oper_status':
                    states[key] = (value == 'up')
        return states
    except Exception:
        return {}


class LedControl(LedControlBase):
    """
    LED control plugin for Accton Wedge 100S-32X.

    On init  : SYS1 = green (system running), SYS2 reflects current STATE_DB
    On link-up   : SYS2 → green
    On last link-down: SYS2 → off
    SYS1 remains green for the lifetime of ledd.
    """

    def __init__(self):
        self._port_states = _state_db_port_states()
        _led_write('led_sys1', _LED_GREEN)
        any_up = any(self._port_states.values())
        _led_write('led_sys2', _LED_GREEN if any_up else _LED_OFF)

    def port_link_state_change(self, port, state):
        self._port_states[port] = (state == 'up')
        any_up = any(self._port_states.values())
        _led_write('led_sys2', _LED_GREEN if any_up else _LED_OFF)
        # Fast-path LEDUP1 DATA_RAM coordination: write a .set file so
        # wedge100s-ledup-linkstate picks up the change on its next poll
        # iteration rather than waiting up to POLL_INTERVAL_S.
        # The daemon is authoritative; this is only a hint to reduce latency.
        led_port = _IFACE_TO_LED_PORT.get(port)
        if led_port is not None:
            _led_write('ledup1_port_%d.set' % led_port,
                       '1' if state == 'up' else '0')
