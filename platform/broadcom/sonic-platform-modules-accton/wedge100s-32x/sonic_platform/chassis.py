#!/usr/bin/env python3
"""
sonic_platform/chassis.py — Chassis stub for Accton Wedge 100S-32X.

Subsystem population schedule:
  Phase 3: Thermal (8 sensors)
  Phase 4: Fan (5 fan trays)
  Phase 5 (this file): PSU (2 units)
  Phase 6: SFP/QSFP (32 ports)
  Phase 7: System EEPROM
"""

try:
    from sonic_platform_base.chassis_base import ChassisBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

from sonic_platform.thermal import Thermal, NUM_THERMALS
from sonic_platform.fan import FanDrawer, NUM_FANS
from sonic_platform.psu import Psu, NUM_PSUS


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

    def get_name(self):
        return "Wedge 100S-32X"
