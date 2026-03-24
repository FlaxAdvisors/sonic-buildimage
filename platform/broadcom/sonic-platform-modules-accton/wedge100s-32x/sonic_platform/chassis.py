#!/usr/bin/env python3
"""
sonic_platform/chassis.py — Chassis stub for Accton Wedge 100S-32X.

Subsystem population schedule:
  Phase 3: Thermal (8 sensors)
  Phase 4: Fan (5 fan trays)
  Phase 5: PSU (2 units)
  Phase 6 (this file): SFP/QSFP (32 ports)
  Phase 7: System EEPROM
"""

import os
import time

try:
    from sonic_platform_base.chassis_base import ChassisBase
    from sonic_platform_base.sfp_base import SfpBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

from sonic_platform.thermal import Thermal, NUM_THERMALS
from sonic_platform.fan import FanDrawer, NUM_FANS
from sonic_platform.psu import Psu, NUM_PSUS
from sonic_platform.sfp import Sfp, NUM_SFPS
from sonic_platform.eeprom import SysEeprom
from sonic_platform.watchdog import Watchdog


class Chassis(ChassisBase):
    """Platform-specific Chassis class for Accton Wedge 100S-32X."""

    REBOOT_CAUSE_FILE = "/var/log/sonic/reboot-cause/previous-reboot-cause.txt"

    def __init__(self):
        ChassisBase.__init__(self)
        for i in range(NUM_THERMALS):
            self._thermal_list.append(Thermal(i))
        for i in range(1, NUM_FANS + 1):   # drawer_index is 1-based (1–5)
            self._fan_drawer_list.append(FanDrawer(i))
        for i in range(1, NUM_PSUS + 1):   # psu_index is 1-based (1–2)
            self._psu_list.append(Psu(i))
        # port_config.ini uses 1-based SFP index (1–32).
        # ChassisBase.get_sfp(index) accesses _sfp_list[index] directly (0-based).
        # Prepend a None sentinel at index 0 so that get_sfp(1)→Sfp(0) and
        # get_sfp(32)→Sfp(31) align correctly with the port_config.ini index column.
        self._sfp_list.append(None)         # index 0 — never requested by xcvrd
        for i in range(NUM_SFPS):
            self._sfp_list.append(Sfp(i))  # index 1..32 → port 0..31
        # System EEPROM (24c64 at i2c-40/0x50, ONIE TlvInfo)
        self._eeprom = SysEeprom()
        # Watchdog stub (x86 iTCO_wdt disabled by BIOS; BMC owns HW WDT)
        self._watchdog = Watchdog()
        # Firmware components: CPLD + BIOS
        from sonic_platform.component import COMPONENT_LIST
        self._component_list = list(COMPONENT_LIST)
        # Previous presence state for get_change_event() polling
        self._prev_presence = {}

    # ------------------------------------------------------------------
    # Status LED  (SYS1 — system-status indicator on CPLD reg 0x3e)
    # healthd calls set_status_led(GREEN|RED|AMBER|OFF) to reflect the
    # overall health state.  The CPLD driver exposes the attr led_sys1.
    # led_control.py owns SYS2 (port-activity) independently via ledd.
    #
    # All LED I/O goes through /run/wedge100s/led_sys1.  wedge100s-i2c-daemon
    # picks up the value on its 3-second tick (apply_led_writes) and writes
    # it through to the CPLD sysfs attribute.  No Python code touches the
    # i2c bus or CPLD sysfs directly.
    # ------------------------------------------------------------------
    _RUN_DIR = '/run/wedge100s'
    _LED_ENCODE = {
        'green':         0x02,
        'red':           0x01,
        'amber':         0x01,   # hardware has no amber; map to red
        'blue':          0x04,
        'off':           0x00,
        'green_blink':   0x0a,
        'red_blink':     0x09,
        'blue_blink':    0x0c,
    }
    _LED_DECODE = {
        0x00: 'off',
        0x01: 'red',
        0x02: 'green',
        0x04: 'blue',
        0x08: 'off',
        0x09: 'red_blink',
        0x0a: 'green_blink',
        0x0c: 'blue_blink',
    }

    def set_status_led(self, color):
        val = self._LED_ENCODE.get(color)
        if val is None:
            return False
        try:
            os.makedirs(self._RUN_DIR, exist_ok=True)
            with open('{}/led_sys1'.format(self._RUN_DIR), 'w') as f:
                f.write('{}\n'.format(val))
            return True
        except Exception:
            return False

    def get_status_led(self):
        try:
            with open('{}/led_sys1'.format(self._RUN_DIR)) as f:
                val = int(f.read().strip(), 0)
            return self._LED_DECODE.get(val, self.STATUS_LED_COLOR_OFF)
        except Exception:
            return self.STATUS_LED_COLOR_OFF

    def get_name(self):
        return "Wedge 100S-32X"

    def get_system_eeprom_info(self):
        return self._eeprom.get_eeprom()

    def _bulk_read_presence(self):
        """
        Read all 32 port presence bits from wedge100s-i2c-daemon cache files.

        Reads /run/wedge100s/sfp_N_present for each port.  Ports whose file
        is absent (daemon not yet started — normal for the first few seconds
        after pmon start) are reported as not present.

        Returns dict {port (0-based): bool present}.
        """
        result = {}
        for port in range(NUM_SFPS):
            cache = '/run/wedge100s/sfp_{}_present'.format(port)
            try:
                with open(cache) as f:
                    result[port] = f.read().strip() == '1'
            except OSError:
                result[port] = False
        return result

    def get_change_event(self, timeout=0):
        """
        Poll all QSFP ports for presence changes via daemon cache files.

        Reads /run/wedge100s/sfp_N_present for all 32 ports; no I2C access.
        xcvrd calls this with timeout in milliseconds.  We sleep 1 second
        between polls, which is appropriate since module insertion/removal
        is a human-scale event.

        Returns:
            (True, {'sfp': {port_idx: '1'|'0', ...}})
            where '1' = module inserted, '0' = module removed.
        """
        expiry = time.monotonic() + (timeout / 1000.0 if timeout else 0)

        while True:
            presence = self._bulk_read_presence()
            events = {}
            if presence is not None:
                for port, present in presence.items():
                    idx = port + 1              # convert to 1-based xcvrd index
                    prev = self._prev_presence.get(idx)
                    if prev != present:
                        events[str(idx)] = '1' if present else '0'
                        self._prev_presence[idx] = present

            if events:
                return True, {'sfp': events}

            if not timeout or time.monotonic() >= expiry:
                return True, {'sfp': {}}

            time.sleep(3.0)   # daemon polls every 3 s; no point polling faster

    def get_serial(self):
        """Return serial number from EEPROM TLV 0x23."""
        try:
            return self._eeprom.get_eeprom().get('0x23', 'NA')
        except Exception:
            return 'NA'

    def get_model(self):
        """Return part number from EEPROM TLV 0x22."""
        try:
            return self._eeprom.get_eeprom().get('0x22', 'NA')
        except Exception:
            return 'NA'

    def get_revision(self):
        """Return device version from EEPROM TLV 0x26."""
        try:
            return self._eeprom.get_eeprom().get('0x26', 'NA')
        except Exception:
            return 'NA'

    def get_base_mac(self):
        """Return base MAC address from EEPROM TLV 0x24."""
        try:
            info = self._eeprom.get_eeprom()
            return info.get('0x24') or info.get('Base MAC Address') or 'NA'
        except Exception:
            return 'NA'

    def get_reboot_cause(self):
        """Return (cause_constant, description) from reboot-cause file."""
        try:
            with open(self.REBOOT_CAUSE_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        return (self.REBOOT_CAUSE_NON_HARDWARE, line)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return (self.REBOOT_CAUSE_POWER_LOSS, "")

    def get_port_or_cage_type(self, index):
        """All 32 ports are QSFP28."""
        if 1 <= index <= NUM_SFPS:
            return SfpBase.SFP_PORT_TYPE_BIT_QSFP28
        return None

    def get_num_components(self):
        return len(self._component_list)

    def get_all_components(self):
        return self._component_list

    def get_component(self, index):
        if 0 <= index < len(self._component_list):
            return self._component_list[index]
        return None
