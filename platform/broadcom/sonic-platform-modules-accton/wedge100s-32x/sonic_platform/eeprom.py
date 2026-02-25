try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError:
    # If the base class isn't installed, we use a dummy
    class eeprom_tlvinfo:
        class TlvInfoDecoder:
            def __init__(self, a, b, c, d): pass

class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):
    def __init__(self):
        self.eeprom_path = "/sys/class/i2c-adapter/i2c-40/40-0050/eeprom"
        # Standard SONiC TlvInfo args: (path, start_offset, link, use_cache)
        super(SysEeprom, self).__init__(self.eeprom_path, 0, '', True)

    def decode_eeprom(self):
        # The official tool specifically looks for this method name
        return self.decode()
