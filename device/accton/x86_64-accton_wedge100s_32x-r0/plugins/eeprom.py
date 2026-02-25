#!/usr/bin/env python
import subprocess

try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError:
    from sonic_eeprom import eeprom_tlvinfo

class board(eeprom_tlvinfo.TlvInfoDecoder):
    def __init__(self, name, path, cpld_root, ro):
        self.eeprom_path = "/sys/bus/i2c/devices/40-0050/eeprom"
        super(board, self).__init__(self.eeprom_path, 0, '', True)