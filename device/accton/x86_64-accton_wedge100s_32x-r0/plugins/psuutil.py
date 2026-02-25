#!/usr/bin/env python
import subprocess

try:
    from sonic_psu.psu_base import PsuBase
except ImportError:
    from sonic_platform_base.psu_base import PsuBase

# CPLD at i2c-1/0x32, PSU status register 0x10 (confirmed on hardware)
# Bit layout per ONL psui.c:
#   PSU1 present:   bit 0  (0 = present)
#   PSU1 pwr good:  bit 1  (1 = good)
#   PSU2 present:   bit 4  (0 = present)
#   PSU2 pwr good:  bit 5  (1 = good)
# NOTE: live register reads 0xe0 with both PSUs installed and running.
# PSU2 pgood (bit 5 = 1) confirmed good. PSU1 pgood (bit 1 = 0) may indicate
# polarity difference or reserved bit — verify against known-good hardware state.
CPLD_BUS  = 1
CPLD_ADDR = "0x32"
PSU_REG   = "0x10"

_PRESENT_BIT = [0, 4]   # index 0 = PSU1, index 1 = PSU2
_PGOOD_BIT   = [1, 5]


def _read_psu_reg():
    cmd = "i2cget -f -y {} {} {}".format(CPLD_BUS, CPLD_ADDR, PSU_REG)
    result = subprocess.check_output(cmd, shell=True).decode().strip()
    return int(result, 0)


class PsuUtil(PsuBase):
    def __init__(self):
        PsuBase.__init__(self)

    def get_num_psus(self):
        return 2

    def get_psu_presence(self, index):
        # index is 1-based
        try:
            val = _read_psu_reg()
            bit = _PRESENT_BIT[index - 1]
            return not bool(val & (1 << bit))  # 0 = present
        except Exception:
            return False

    def get_psu_status(self, index):
        # Returns True if PSU is present and power-good
        try:
            val = _read_psu_reg()
            present_bit = _PRESENT_BIT[index - 1]
            pgood_bit   = _PGOOD_BIT[index - 1]
            present = not bool(val & (1 << present_bit))
            pgood   = bool(val & (1 << pgood_bit))
            return present and pgood
        except Exception:
            return False
