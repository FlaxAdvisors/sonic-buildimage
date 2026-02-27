#!/usr/bin/env python3
"""
sonic_platform/eeprom.py — System EEPROM for Accton Wedge 100S-32X.

Hardware: AT24C02 (256 B) at i2c-1/0x51, ONIE TlvInfo format.
The COME module is directly on i2c-1 (the CP2112 root bus), NOT behind
the PCA9548 mux at 0x74.  The mux channels are non-isolating for these
devices because the CP2112 cannot hold mux channel selection between HID
report transactions.

i2c-1/0x50: COME module EC chip — exposes ODM-format platform data via
            1-byte I2C register reads; NOT writable via standard AT24 protocol.
i2c-1/0x51: AT24C02 EEPROM — writable via standard I2C; holds ONIE TlvInfo.

The at24 device is registered by accton_wedge100s_util.py at boot via:
    echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device
Bus 40 is mux 0x74 ch6, which is transparent to i2c-1 for COME devices.
Sysfs node: /sys/bus/i2c/devices/40-0051/eeprom

The EEPROM cache written at platform-init time (before xcvrd/pmon start)
avoids any residual CP2112 mux-contention issues at runtime.
"""

try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# Persistent cache written by accton_wedge100s_util.py at platform-init time,
# before xcvrd/pmon start.  Prevents CP2112 I2C bus hangs from mux 0x74
# contention (EEPROM on ch6 vs. PCA9535 presence polls on ch2/ch3).
EEPROM_CACHE_PATH = '/var/run/platform_cache/syseeprom_cache'


class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):

    EEPROM_PATH = "/sys/bus/i2c/devices/40-0051/eeprom"

    def __init__(self):
        super(SysEeprom, self).__init__(self.EEPROM_PATH, 0, EEPROM_CACHE_PATH, True)
        self.cache_name = EEPROM_CACHE_PATH
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
