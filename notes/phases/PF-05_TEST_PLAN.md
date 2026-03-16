# PF-05 — I2C/QSFP Daemon: Test Plan

## What a Passing Test Looks Like

The I2C/QSFP daemon is working correctly when:
1. The timer is active and firing every 3 s.
2. `syseeprom` exists and contains valid ONIE TLV data.
3. All 32 `sfp_N_present` files exist.
4. Ports with physically installed modules have `sfp_N_eeprom` files with valid
   QSFP28 identifier bytes.
5. `i2c_mux_pca954x`, `optoe`, and `at24` are NOT loaded.

## Required Hardware State

- Platform init complete.
- At least 10 s elapsed since boot (first `OnBootSec=5s` + at least one `OnUnitActiveSec=3s`).
- At least one QSFP module physically installed (lab: ports 0, 4, 8, 12, 16, 20, 26-28).
- `/dev/hidraw0` accessible (hid_cp2112 loaded, CP2112 USB device present).

## Test Actions

### Step 1 — No mux/optoe/at24 drivers loaded

```bash
lsmod | grep -E 'i2c_mux_pca954x|optoe|at24'
```

Expected: no output.

### Step 2 — Timer active

```bash
systemctl is-active wedge100s-i2c-poller.timer
```

Expected: `active`

```bash
systemctl status wedge100s-i2c-poller.timer | grep Trigger
```

Expected: trigger within next 3 s.

### Step 3 — hidraw device accessible

```bash
ls -la /dev/hidraw0
```

Expected: file exists.

### Step 4 — syseeprom valid

```bash
python3 -c "
with open('/run/wedge100s/syseeprom', 'rb') as f:
    data = f.read()
assert len(data) >= 8, f'Too short: {len(data)}'
assert data[:8] == b'TlvInfo\x00', f'Bad magic: {data[:8]!r}'
print(f'syseeprom: {len(data)} bytes, TlvInfo magic OK')
"
```

Expected: `syseeprom: 8192 bytes, TlvInfo magic OK`

Verify specific TLV fields:

```bash
python3 -c "
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.eeprom import EepromS3IP
e = EepromS3IP()
print('Serial:', e.serial_number_str())
print('MAC:', e.base_mac_addr())
assert e.serial_number_str() == 'AI09019591', 'Wrong serial'
print('PASS')
"
```

Expected: `PASS` with correct serial number.

### Step 5 — All 32 presence files exist

```bash
ls /run/wedge100s/sfp_{0..31}_present | wc -l
```

Expected: `32`

### Step 6 — Presence files are `0` or `1`

```bash
for i in $(seq 0 31); do
  v=$(cat /run/wedge100s/sfp_${i}_present)
  if [ "$v" != "0" ] && [ "$v" != "1" ]; then
    echo "ERROR: sfp_${i}_present = '$v'"
    exit 1
  fi
done
echo "All 32 presence files valid"
```

Expected: `All 32 presence files valid`

### Step 7 — Occupied ports have EEPROM files with valid identifier

```bash
python3 -c "
import os, sys
present = []
for i in range(32):
    p = f'/run/wedge100s/sfp_{i}_present'
    if open(p).read().strip() == '1':
        present.append(i)
print(f'Present ports: {present}')
failures = []
for i in present:
    ep = f'/run/wedge100s/sfp_{i}_eeprom'
    if not os.path.exists(ep):
        failures.append(f'port {i}: no eeprom file')
        continue
    with open(ep, 'rb') as f:
        b0 = f.read(1)
    if not b0 or not (0x01 <= b0[0] <= 0x7f):
        failures.append(f'port {i}: invalid id byte {b0!r}')
if failures:
    print('FAIL:', failures)
    sys.exit(1)
print(f'{len(present)} present ports all have valid EEPROM identifiers')
print('PASS')
"
```

Expected: `PASS` with count of present ports.

### Step 8 — SONiC Python API uses daemon cache

```bash
python3 -c "
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.sfp import Sfp
# Find first present port
for i in range(32):
    s = Sfp(i)
    if s.get_presence():
        data = s.read_eeprom(0, 1)
        print(f'Port {i}: present=True, eeprom[0]={hex(data[0])}')
        assert 0x01 <= data[0] <= 0x7f, f'Invalid id {hex(data[0])}'
        print('PASS')
        break
else:
    print('No present ports found')
"
```

Expected: `PASS` with port number and identifier byte.

### Step 9 — EEPROM files are fresh (< 30 s old at time of check)

```bash
FIRST_PRESENT=$(for i in $(seq 0 31); do
  if [ "$(cat /run/wedge100s/sfp_${i}_present)" = "1" ]; then echo $i; break; fi
done)
find /run/wedge100s/sfp_${FIRST_PRESENT}_present -mmin -0.5
```

Expected: file found (updated within last 30 s).

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| i2c_mux_pca954x NOT loaded | not in lsmod | in lsmod |
| timer active | `active` | inactive |
| syseeprom TlvInfo magic | correct | wrong or file missing |
| syseeprom serial number | `AI09019591` | wrong or error |
| 32 sfp_N_present files | 32 files | < 32 |
| all presence values 0 or 1 | yes | any other value |
| present ports have eeprom | all present ports have file | missing |
| eeprom id bytes valid | 0x01–0x7f | 0x00 or 0x80–0xff |
| Python API returns correct id | 0x11 (QSFP28) for DAC port | wrong or error |
| presence files fresh | mtime < 30 s ago | older |

## Mapping to Test Stage

These checks map to `tests/stage_07_qsfp/` (existing, 11/11 passing 2026-03-14)
and `tests/stage_01_eeprom/` for the syseeprom check, and
`tests/stage_10_daemon/` (planned) for timer/file-age checks.

## State Changes and Restoration

All steps are read-only. The daemon timer fires in the background every 3 s and
overwrites the files — this is normal operation. No cleanup required.

Step 8 imports `sonic_platform` but does not write to Redis/CONFIG_DB.
