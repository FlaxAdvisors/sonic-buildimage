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
from sonic_platform import platform_smbus

# PCA9535 presence registers (bus, i2c_addr, register).
# Register 0 = INPUT0 (GPIO lines 0-7), register 1 = INPUT1 (GPIO lines 8-15).
# Lines are wired with XOR-1 interleave: line (r*8+b) ^ 1 = port within chip group.
_PRESENCE_REGS = [
    (36, 0x22, 0),   # chip 36-0022 INPUT0 → ports {1,0,3,2,5,4,7,6}
    (36, 0x22, 1),   # chip 36-0022 INPUT1 → ports {9,8,11,10,13,12,15,14}
    (37, 0x23, 0),   # chip 37-0023 INPUT0 → ports {17,16,19,18,21,20,23,22}
    (37, 0x23, 1),   # chip 37-0023 INPUT1 → ports {25,24,27,26,29,28,31,30}
]


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
        # Previous presence state for get_change_event() polling
        self._prev_presence = {}

    # ------------------------------------------------------------------
    # Status LED  (SYS1 — system-status indicator on CPLD reg 0x3e)
    # healthd calls set_status_led(GREEN|RED|AMBER|OFF) to reflect the
    # overall health state.  The CPLD driver exposes the attr led_sys1.
    # led_control.py owns SYS2 (port-activity) independently via ledd.
    #
    # Write-through: every set writes to /run/wedge100s/led_sys1 (observable
    # state) AND to the CPLD sysfs attribute (immediate hardware effect).
    # The /run file lets the CLI utility and other tools read current state
    # without touching the i2c bus.
    # ------------------------------------------------------------------
    _CPLD_SYSFS = '/sys/bus/i2c/devices/1-0032'
    _RUN_DIR    = '/run/wedge100s'
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
        except Exception:
            pass
        try:
            with open('{}/led_sys1'.format(self._CPLD_SYSFS), 'w') as f:
                f.write(str(val))
            return True
        except Exception:
            return False

    def get_status_led(self):
        try:
            with open('{}/led_sys1'.format(self._CPLD_SYSFS)) as f:
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

        Primary path: /run/wedge100s/sfp_N_present written by the daemon every 3 s.
        Eliminates direct mux-tree I2C from the chassis polling loop, removing
        the residual mux race between chassis.py presence reads (mux 0x74 ch2/3)
        and daemon EEPROM reads (mux 0x70-0x73) on module insertion.

        Fallback (daemon not yet run — first ~5 s of boot): direct smbus2 reads
        of PCA9535 INPUT registers, identical to the old primary path.

        Returns dict {port (0-based): bool present}.
        """
        result = {}
        # Try daemon cache files (normal operation after first daemon tick)
        cache_miss = False
        for port in range(NUM_SFPS):
            cache = '/run/wedge100s/sfp_{}_present'.format(port)
            try:
                with open(cache) as f:
                    result[port] = f.read().strip() == '1'
            except OSError:
                cache_miss = True
                break

        if not cache_miss:
            return result

        # Fallback: direct smbus2 (daemon not yet run — first ~5 s of boot)
        result = {}
        for reg_idx, (bus, addr, reg) in enumerate(_PRESENCE_REGS):
            byte = platform_smbus.read_byte(bus, addr, reg)
            if byte is None:
                return {i: self._sfp_list[i + 1].get_presence()
                        for i in range(NUM_SFPS)}
            group   = reg_idx // 2
            reg_num = reg_idx % 2
            for bit in range(8):
                line = reg_num * 8 + bit
                port = group * 16 + (line ^ 1)
                result[port] = not bool((byte >> bit) & 1)
        return result

    def get_change_event(self, timeout=0):
        """
        Poll all QSFP ports for presence changes using bulk I2C reads.

        Replaces the per-port GPIO sysfs loop (330 reads/sec) with 4
        smbus2 reads of the PCA9535 INPUT registers — matching the ONL
        onlp_sfpi_presence_bitmap_get() pattern exactly.

        xcvrd calls this with timeout in milliseconds.  We sleep 1 second
        between polls (down from 0.1s), which is appropriate since module
        insertion/removal is a human-scale event.

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
