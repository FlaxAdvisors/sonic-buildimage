#!/usr/bin/env python
import os
import subprocess

try:
    from sonic_sfp.sfputilbase import SfpUtilBase
except ImportError:
    from sonic_platform_base.sonic_sfp.sfputilbase import SfpUtilBase

# Port-to-I2C-bus mapping (confirmed from ONL sfpi.c sfp_bus_index[],
# verified: port 0 -> bus 3 has QSFP28 identifier 0x11 on hare-lorax)
_SFP_BUS_MAP = [
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    19, 18, 21, 20, 23, 22, 25, 24,
    27, 26, 29, 28, 31, 30, 33, 32,
]

# PCA9535 GPIO expanders for QSFP presence (confirmed on hardware)
# i2c-36/0x22: ports 0-15, i2c-37/0x23: ports 16-31
_PRESENCE_BUS  = [36, 37]   # index 0 = ports 0-15, index 1 = ports 16-31
_PRESENCE_ADDR = [0x22, 0x23]


def _bit_swap(value):
    """Swap even/odd bit pairs per ONL sfpi.c onlp_sfpi_reg_val_to_port_sequence().
    PCA9535 wiring interleaves even/odd ports; this corrects the mapping."""
    result = 0
    for i in range(8):
        if i % 2 == 1:
            result |= (value & (1 << i)) >> 1
        else:
            result |= (value & (1 << i)) << 1
    return result


def _read_presence_byte(bus, addr, offset):
    cmd = "i2cget -f -y {} 0x{:02x} 0x{:02x}".format(bus, addr, offset)
    out = subprocess.check_output(cmd, shell=True).decode().strip()
    return int(out, 0)


class SfpUtil(SfpUtilBase):
    port_to_eeprom_mapping = {}

    def __init__(self):
        SfpUtilBase.__init__(self)
        self.port_start = 0
        self.port_end = 31
        self.qsfp_ports = range(0, 32)

        for port in range(self.port_start, self.port_end + 1):
            bus = _SFP_BUS_MAP[port]
            # Path exists only after optoe1/at24 device is instantiated by platform init
            self.port_to_eeprom_mapping[port] = \
                "/sys/class/i2c-adapter/i2c-{0}/{0}-0050/eeprom".format(bus)

    @property
    def port_start(self):
        return self._port_start

    @port_start.setter
    def port_start(self, val):
        self._port_start = val

    @property
    def port_end(self):
        return self._port_end

    @port_end.setter
    def port_end(self, val):
        self._port_end = val

    @property
    def qsfp_ports(self):
        return self._qsfp_ports

    @qsfp_ports.setter
    def qsfp_ports(self, val):
        self._qsfp_ports = val

    def get_presence(self, port_num):
        try:
            group  = 0 if port_num < 16 else 1
            bus    = _PRESENCE_BUS[group]
            addr   = _PRESENCE_ADDR[group]
            # Within each group, ports 0-7 are offset 0, ports 8-15 are offset 1
            local  = port_num % 16
            offset = 0 if (local < 8 or (port_num >= 16 and local < 8)) else 1
            # sfpi.c: ports <8 or ports 16-23 use offset 0; others use offset 1
            if port_num < 8 or (16 <= port_num <= 23):
                offset = 0
            else:
                offset = 1
            raw = _read_presence_byte(bus, addr, offset)
            swapped = _bit_swap(raw)
            return not bool(swapped & (1 << (port_num % 8)))
        except Exception:
            return False

    def get_low_power_mode(self, port_num):
        return False

    def set_low_power_mode(self, port_num, lpmode):
        return False

    def reset(self, port_num):
        return False
