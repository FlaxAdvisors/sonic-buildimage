# PS-04 TEST PLAN — QSFP/SFP Subsystem

## Test Stage Mapping

`tests/stage_07_qsfp/` and `tests/stage_11_transceiver/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- `wedge100s-i2c-poller.timer` active and having fired at least once
  (verify: `/run/wedge100s/sfp_0_present` exists)
- At least 1 QSFP28 module physically installed (for EEPROM tests)
- `i2c_mux_pca954x` NOT loaded (verify: `lsmod | grep pca954` returns nothing)
- pmon container running with `/run/wedge100s` bind-mounted

## Step-by-Step Test Actions

### 1. Import and verify count

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.sfp import Sfp, NUM_SFPS

assert NUM_SFPS == 32
```

### 2. Verify chassis sentinel structure

```python
from sonic_platform.chassis import Chassis
chassis = Chassis()
sfp_list = chassis._sfp_list

assert len(sfp_list) == 33,   f"Expected 33 entries (1 sentinel + 32), got {len(sfp_list)}"
assert sfp_list[0] is None,   "sfp_list[0] should be None sentinel"
assert sfp_list[1] is not None
assert sfp_list[32] is not None
```

### 3. Verify port indexing alignment

```python
for port in range(NUM_SFPS):
    sfp = sfp_list[port + 1]   # 1-based access as xcvrd uses
    assert sfp._port == port,  f"Port {port}: _port mismatch"
    assert sfp.get_position_in_parent() == port + 1
    assert sfp.get_name() == f'QSFP28 {port + 1}'
```

### 4. Verify bus map

```python
from sonic_platform.sfp import _SFP_BUS_MAP
assert len(_SFP_BUS_MAP) == 32
# Spot-check ONL values
assert _SFP_BUS_MAP[0]  ==  3   # port 0 → bus 3
assert _SFP_BUS_MAP[1]  ==  2   # port 1 → bus 2
assert _SFP_BUS_MAP[31] == 32   # port 31 → bus 32
```

### 5. Verify presence for all ports

```python
# Cross-check daemon cache against direct PCA9535 read
import os

daemon_presence = {}
for port in range(NUM_SFPS):
    p = f'/run/wedge100s/sfp_{port}_present'
    try:
        daemon_presence[port] = open(p).read().strip() == '1'
    except OSError:
        daemon_presence[port] = None

# Now verify Sfp.get_presence() agrees
for port in range(NUM_SFPS):
    sfp = sfp_list[port + 1]
    api_present = sfp.get_presence()
    if daemon_presence[port] is not None:
        assert api_present == daemon_presence[port], \
            f"Port {port}: API={api_present} but daemon={daemon_presence[port]}"
```

### 6. Verify EEPROM read for populated ports

```python
populated = [p for p in range(NUM_SFPS) if daemon_presence.get(p)]
assert len(populated) > 0, "No modules installed — cannot test EEPROM"

for port in populated[:3]:   # test first 3 populated ports
    sfp = sfp_list[port + 1]
    data = sfp.read_eeprom(0, 256)
    assert data is not None, f"Port {port}: read_eeprom returned None"
    assert len(data) == 256, f"Port {port}: expected 256 bytes, got {len(data)}"
    # Byte 0 = identifier; 0x11 = QSFP28
    assert data[0] == 0x11, f"Port {port}: identifier byte {data[0]:#x} != 0x11 (QSFP28)"
```

### 7. Verify EEPROM returns None for absent ports

```python
absent = [p for p in range(NUM_SFPS) if daemon_presence.get(p) is False]
for port in absent[:2]:
    sfp = sfp_list[port + 1]
    data = sfp.read_eeprom(0, 256)
    assert data is None, f"Port {port}: expected None for absent port, got {data}"
```

### 8. Verify unsupported operations

```python
sfp = sfp_list[1]  # any port
assert sfp.reset()         is False
assert sfp.set_lpmode(True) is False
assert sfp.get_lpmode()    is False
assert sfp.get_reset_status() is False
```

### 9. Verify error description

```python
for port in range(NUM_SFPS):
    sfp = sfp_list[port + 1]
    if not sfp.get_presence():
        assert sfp.get_error_description() == sfp.SFP_STATUS_UNPLUGGED
```

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| `NUM_SFPS` | 32 | Any other value |
| `_sfp_list` length | 33 | != 33 |
| `_sfp_list[0]` | None | Not None |
| All port._port | 0-based match | Mismatch |
| Presence vs daemon | Agreement | Mismatch |
| Populated EEPROM byte 0 | 0x11 | != 0x11 |
| Absent port `read_eeprom` | None | Any non-None |
| `reset()` | False | True |
| `set_lpmode()` | False | True |

## State Changes and Restoration

This test is read-only. No I2C writes, no sysfs writes.
`i2c_mux_pca954x` must remain unloaded throughout — the test should verify
this precondition and abort if it detects the module loaded:

```python
import subprocess
result = subprocess.run(['lsmod'], capture_output=True, text=True)
assert 'pca954' not in result.stdout, \
    "ABORT: i2c_mux_pca954x is loaded — EEPROM corruption risk"
```

---

## Pending Investigation — Vendor String Empty on DAC Cables

**Test:** `stage_07_qsfp/test_qsfp.py::test_qsfp_eeprom_vendor_info`

**Symptom:** Vendor name bytes at EEPROM offset 148–163 return empty or
non-printable data on SONiC. The same physical modules read correct vendor
strings via `show interfaces transceiver detail` on the Arista EOS peer
(rabbit-lorax), confirming the modules are not defective. Identifier byte 0
reads correctly; the failure is specific to the upper vendor-info region.

**Hypotheses (rule out in order):**

1. **Page select not implemented** — QSFP28 vendor name is at page 0 offset 148.
   Some platforms require writing page select byte (offset 127) to 0x00 before
   reading the upper half of the lower page. Check whether `sfp.py` or
   `SfpOptoeBase.read_eeprom()` handles page select, or whether the daemon
   EEPROM cache is built correctly (it should be 256 contiguous bytes from a
   single I2C read, no page select needed for page 0).

2. **EEPROM sysfs path returns only lower page (128 bytes)** — The sysfs
   `eeprom` file may be truncated to 128 bytes. Verify on hardware:
   ```bash
   sudo wc -c /run/wedge100s/sfp_0_eeprom
   sudo hexdump -C /run/wedge100s/sfp_0_eeprom | tail -20
   ```
   The daemon cache file should be exactly 256 bytes; bytes 128–255 should
   contain module identifier, extended ID, connector, and vendor data.

3. **I2C mux not held during multi-byte read** — The PCA9548 mux channel may
   be released between sequential I2C reads inside the daemon. This would
   cause reads past the first 16-byte chunk to hit a different mux channel.
   Check `wedge100s-i2c-daemon.c`: the read loop must hold the mux channel
   select byte throughout the full 256-byte read.

4. **Module is non-standard or QSFP-DD** — Confirm identifier byte value
   (0x11 = QSFP28, 0x18 = QSFP-DD) and verify the correct EEPROM memory map
   is being applied. A QSFP-DD module has a different page layout.

**Comparison command (EOS peer):**
```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et13/1 transceiver detail'
```

**Reference:** SFF-8636 rev 2.10 Table 6-14 (vendor name: bytes 148–163, page 0).
