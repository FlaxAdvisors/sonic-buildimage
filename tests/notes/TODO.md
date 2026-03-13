# TODO — Outstanding Investigation Items

## QSFP EEPROM Data Appears Corrupt on SONiC

**Stage:** `stage_07_qsfp` — `test_qsfp_eeprom_vendor_info` (currently XFAIL)

**Symptom:**
- Vendor name bytes at EEPROM offset 148–163 return empty or non-printable data on SONiC
- The same physical modules read correctly via `show interfaces transceiver detail` on the
  Arista EOS peer (rabbit-lorax), confirming the modules themselves are not defective
- `test_qsfp_eeprom_identifier_byte` (byte 0) passes — identifier byte is readable
- The failure is in the upper page / vendor info region, not in the lower page

**Likely causes to investigate:**
1. **Page select not implemented** — QSFP28 vendor name is at page 0 offset 148. Some
   platforms require writing the page select byte (offset 127) before reading the upper
   half. Check whether `sfp.py` / the EEPROM sysfs driver handles page select, or whether
   `dd skip=148` is reading past a 128-byte page boundary incorrectly.
2. **EEPROM sysfs path returns only lower page** — The sysfs eeprom file may be limited
   to 128 bytes (lower page only). Verify with:
   ```
   sudo wc -c <eeprom_path>
   sudo hexdump -C <eeprom_path> | head -20
   ```
3. **i2c mux not held open during multi-byte read** — The PCA9548 mux channel may be
   released between page select write and data read. Check if the xcvrd / EEPROM sysfs
   driver holds the mux during the full transaction.
4. **Module is QSFP-DD or non-standard** — Confirm identifier byte value (0x11=QSFP28,
   0x18=QSFP-DD) and verify the correct EEPROM map is being used.

**Investigation steps:**
```bash
# On SONiC (hare-lorax):
# 1. Check identifier and eeprom file size
sudo hexdump -C /sys/bus/i2c/devices/<N>/eeprom | head -40
sudo wc -c /sys/bus/i2c/devices/<N>/eeprom

# 2. Compare against EOS (rabbit-lorax) for same physical port
sshpass -p '0penSesame' ssh -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et13/1 transceiver detail'

# 3. Check xcvrd is not competing for the i2c bus during the read
docker exec pmon supervisorctl status xcvrd
```

**Reference:**
- Test file: `stage_07_qsfp/test_qsfp.py::test_qsfp_eeprom_vendor_info` (line 184)
- QSFP28 EEPROM map: SFF-8636 rev 2.10, Table 6-14 (vendor name bytes 148–163, page 0)
