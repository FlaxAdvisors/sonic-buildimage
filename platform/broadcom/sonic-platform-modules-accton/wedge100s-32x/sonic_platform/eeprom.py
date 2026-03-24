#!/usr/bin/env python3
"""
sonic_platform/eeprom.py — System EEPROM for Accton Wedge 100S-32X.

Hardware: 24c64 (8 KiB) at 0x50 on mux 0x74 ch6 (i2c-40), ONIE TlvInfo format.
ONIE dmesg: "at24 7-0050: 8192 byte 24c64 EEPROM, writable, 1 bytes/write"

The at24 device is registered by accton_wedge100s_util.py at boot via:
    echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device
Sysfs node: /sys/bus/i2c/devices/40-0050/eeprom

Source: /run/wedge100s/syseeprom written by wedge100s-i2c-daemon at first
boot.  This is the only read path; there is no sysfs fallback.  The daemon
serialises all CP2112 access, eliminating the mux-contention issue (shared
PCA9548 0x74 between EEPROM ch6 and PCA9535 presence chips ch2/3) that
caused address corruption and zeroed-data before the daemon architecture.
"""

try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

_SYSEEPROM_DAEMON_CACHE = '/run/wedge100s/syseeprom'
_ONIE_MAGIC             = b'TlvInfo\x00'


class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):

    def __init__(self):
        # TlvInfoDecoder base path is not used (read_eeprom is fully overridden).
        super(SysEeprom, self).__init__('', 0, '', False)
        self._eeprom_cache = None

    def read_eeprom(self):
        """Return raw EEPROM bytes from the daemon cache, or None if absent."""
        try:
            with open(_SYSEEPROM_DAEMON_CACHE, 'rb') as f:
                data = f.read(8192)
            if len(data) >= 8 and data[:8] == _ONIE_MAGIC:
                return bytearray(data)
        except OSError:
            pass
        return None

    def get_eeprom(self):
        """
        Returns a dictionary of TLV entries decoded from the system EEPROM.

        Keys are hex type-code strings (e.g. "0x21" for Product Name).
        Values are their decoded string representations.

        Returns {} without caching when the daemon file is absent (normal for
        the first few seconds after boot) so the next call retries.  Once a
        valid parse succeeds the result is cached permanently (EEPROM is static).
        """
        if self._eeprom_cache is not None:
            return self._eeprom_cache

        try:
            raw = self.read_eeprom()
        except Exception:
            return {}

        # Daemon file absent or invalid — do not cache; caller will retry.
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

        if result:
            self._eeprom_cache = result  # cache only on successful parse
        return result

    def system_eeprom_info(self):
        return self.get_eeprom()
