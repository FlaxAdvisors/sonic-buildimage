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

import time

try:
    from sonic_platform_base.chassis_base import ChassisBase
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
        # Pre-warm the SMBus pool for presence buses so first poll is fast.
        platform_smbus.read_byte(36, 0x22, 0)
        platform_smbus.read_byte(37, 0x23, 0)

    # ------------------------------------------------------------------
    # Status LED  (SYS1 — system-status indicator on CPLD reg 0x3e)
    # healthd calls set_status_led(GREEN|RED|AMBER|OFF) to reflect the
    # overall health state.  The CPLD driver exposes the attr led_sys1.
    # led_control.py owns SYS2 (port-activity) independently via ledd.
    # ------------------------------------------------------------------
    _CPLD_SYSFS    = '/sys/bus/i2c/devices/1-0032'
    _LED_ENCODE = {
        'green': 0x02,
        'red':   0x01,
        'amber': 0x01,   # hardware has no amber; map to red
        'off':   0x00,
    }
    _LED_DECODE = {v: k for k, v in _LED_ENCODE.items()}
    _LED_DECODE[0x01] = 'red'   # canonical decode for 0x01

    def set_status_led(self, color):
        val = self._LED_ENCODE.get(color)
        if val is None:
            return False
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
        Read all 32 port presence bits in 4 I2C reads via platform_smbus.

        PCA9535 INPUT registers are active-low: 0 = module present.
        The GPIO lines use XOR-1 interleave wiring; bit b in register r
        on chip group g maps to port = g*16 + (r*8 + b) ^ 1.

        Returns dict {port (0-based): bool present} or None on I2C error.
        Falls back to per-port GPIO sysfs if smbus2 is unavailable.
        """
        result = {}
        for reg_idx, (bus, addr, reg) in enumerate(_PRESENCE_REGS):
            byte = platform_smbus.read_byte(bus, addr, reg)
            if byte is None:
                # smbus2 unavailable — fall back to sysfs for all ports
                result = {}
                for idx in range(1, NUM_SFPS + 1):
                    result[idx - 1] = self._sfp_list[idx].get_presence()
                return result
            group = reg_idx // 2    # 0 = chip 36-0022, 1 = chip 37-0023
            reg_num = reg_idx % 2   # 0 = INPUT0, 1 = INPUT1
            for bit in range(8):
                line = reg_num * 8 + bit
                port = group * 16 + (line ^ 1)
                result[port] = not bool((byte >> bit) & 1)  # active-low
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

            time.sleep(1.0)
