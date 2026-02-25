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
        # Previous presence state for get_change_event() polling
        self._prev_presence = {}

    def get_name(self):
        return "Wedge 100S-32X"

    def get_change_event(self, timeout=0):
        """
        Poll all QSFP ports for presence changes.

        This platform has no hardware interrupt for SFP insertion/removal;
        the PCA9535 GPIO expanders must be polled via i2cget.

        xcvrd calls this with timeout in milliseconds.  We poll until
        either a change is detected or the timeout expires, then return.

        Returns:
            (True, {'sfp': {port_idx: '1'|'0', ...}})
            where '1' = module inserted, '0' = module removed.
        """
        expiry = time.monotonic() + (timeout / 1000.0 if timeout else 0)

        while True:
            events = {}
            for idx in range(1, NUM_SFPS + 1):   # 1-based, matching _sfp_list
                sfp = self._sfp_list[idx]
                present = sfp.get_presence()
                prev = self._prev_presence.get(idx)
                if prev != present:
                    events[str(idx)] = '1' if present else '0'
                    self._prev_presence[idx] = present

            if events:
                return True, {'sfp': events}

            if not timeout or time.monotonic() >= expiry:
                return True, {'sfp': {}}

            time.sleep(0.1)
