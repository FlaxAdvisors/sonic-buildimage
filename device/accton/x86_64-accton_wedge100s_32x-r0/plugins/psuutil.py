#!/usr/bin/env python
import subprocess

try:
    from sonic_psu.psu_base import PsuBase
except ImportError:
    from sonic_platform_base.psu_base import PsuBase

# CPLD sysfs attributes from wedge100s_cpld driver (Phase R26).
# Driver binds to i2c-1/0x32; attributes:
#   psu{N}_present — 1 = present (driver inverts active-low bit)
#   psu{N}_pgood   — 1 = power good
_CPLD_SYSFS = '/sys/bus/i2c/devices/1-0032'


def _cpld_read(attr):
    try:
        with open('{}/{}'.format(_CPLD_SYSFS, attr)) as f:
            return int(f.read().strip(), 0)
    except Exception:
        return None


class PsuUtil(PsuBase):
    def __init__(self):
        PsuBase.__init__(self)

    def get_num_psus(self):
        return 2

    def get_psu_presence(self, index):
        # index is 1-based
        val = _cpld_read('psu{}_present'.format(index))
        return bool(val) if val is not None else False

    def get_psu_status(self, index):
        # Returns True if PSU is present and power-good
        present = _cpld_read('psu{}_present'.format(index))
        pgood   = _cpld_read('psu{}_pgood'.format(index))
        if present is None or pgood is None:
            return False
        return bool(present) and bool(pgood)
