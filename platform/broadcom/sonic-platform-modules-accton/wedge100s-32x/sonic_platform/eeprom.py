#!/usr/bin/env python3
"""
sonic_platform/eeprom.py — System EEPROM for Accton Wedge 100S-32X.

Hardware: 24c64 (8 KB AT24C64) at i2c-40/0x50, ONIE TlvInfo format.
The at24 device is registered by accton_wedge100s_util.py at boot:
    echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device
Sysfs node: /sys/bus/i2c/devices/40-0050/eeprom
"""

try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):

    EEPROM_PATH = "/sys/bus/i2c/devices/40-0050/eeprom"

    def __init__(self):
        super(SysEeprom, self).__init__(self.EEPROM_PATH, 0, '', True)
        self._eeprom_cache = None

    def get_eeprom(self):
        """
        Returns a dictionary of TLV entries decoded from the system EEPROM.

        Keys are hex type-code strings (e.g. "0x21" for Product Name).
        Values are their decoded string representations.
        Returns {} on any read or parse error.
        """
        if self._eeprom_cache is not None:
            return self._eeprom_cache

        try:
            raw = self.read_eeprom()
        except Exception:
            return {}

        if raw is None or len(raw) < self._TLV_INFO_HDR_LEN + 2:
            return {}

        total_length = (raw[9] << 8) | raw[10]
        idx = self._TLV_INFO_HDR_LEN
        end = idx + total_length
        result = {}

        while (idx + 2) <= len(raw) and idx < end:
            if not self.is_valid_tlv(raw[idx:]):
                break
            tlv_len = raw[idx + 1]
            tlv = raw[idx:idx + 2 + tlv_len]
            code = "0x{:02X}".format(raw[idx])
            _, value = self.decoder(None, tlv)
            result[code] = value
            if raw[idx] == self._TLV_CODE_CRC_32:
                break
            idx += 2 + tlv_len

        self._eeprom_cache = result
        return result

    def system_eeprom_info(self):
        return self.get_eeprom()
