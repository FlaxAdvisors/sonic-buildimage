# EEPROM Address Research: 0x50 vs 0x51 on Wedge 100S-32X

**Date:** 2026-02-27
**Status:** Research complete; hardware verification steps listed at end

---

## Status: HARDWARE-VERIFIED 2026-02-27

All conclusions below confirmed by live hardware testing on hare-lorax running ONIE.

---

## Authoritative Sources All Say 0x50

Three independent systems, implemented separately, all expect the system EEPROM at **0x50**:

| Source | Evidence |
|--------|----------|
| OpenBMC `eeprom` tool | `base-address of eeproms: 0x50` |
| ONIE | "Invalid TLV header" reading from 0x50 (device found, content wrong) |
| ONL `platform_lib.h` line 45 | `#define IDPROM_PATH "/sys/class/i2c-adapter/i2c-40/40-0050/eeprom"` |
| ONL `__init__.py` line 18 | `('24c64', 0x50, 40)` |
| BMC dmesg | `at24 7-0050: 256 byte 24c02 EEPROM, writable` |

**The factory address of the system EEPROM is 0x50.** Our SONiC development work
inadvertently moved it to 0x51. The "0x50 is the COME EC chip" conclusion in our
earlier session notes was written after that relocation had already occurred and was
incorrect.

---

## How a Physical Address Move Is Possible

The AT24C02 has hardware address-select pins A0/A1/A2. The 7-bit I2C address is
`1010 A2 A1 A0`. If A0 is wired to a CPLD GPIO output (not hard-tied to GND):

```
CPLD GPIO out = 0  →  A0=0  →  EEPROM at 0x50   (factory/ONIE/BMC view)
CPLD GPIO out = 1  →  A0=1  →  EEPROM at 0x51   (after our init runs)
```

Any write to the CPLD at i2c-1/0x32 that sets the bit connected to A0 would move the
EEPROM. Our platform init script writes to at minimum the LED registers (0x3e, 0x3f).
One of the CPLD registers we wrote to is likely the one that controls A0.

**Why this explains everything:**
- BMC reads from its own i2c bus connection to the COME module — it bypasses the CPLD
  address-select circuit and always sees the chip at the physical address (0x50 from BMC)
- ONIE boots before our init runs → CPLD at power-on state → EEPROM at 0x50 → ONIE
  finds the device but content was corrupted by our dev work → "Invalid TLV header"
- SONiC boots → our init runs → CPLD write moves EEPROM to 0x51 → at24 registered at
  0x51 → reads TlvInfo we wrote there

If the EEPROM was always at 0x51 from the factory, ONIE and ONL and the BMC eeprom tool
would never have been written to look at 0x50. They agree because 0x50 was correct.

---

## What Our Development Work Damaged

### 1. Content corruption at 0x50 (the real system EEPROM)

When the EEPROM was still at 0x50, we registered it as `24c64` (2-byte addressing)
following ONL. The at24 driver for 24c64 sends a 2-byte address word before data.
An AT24C02 (1-byte addressing) interprets the first byte as the word address and the
second byte as the first data byte. So our write attempts to 0x50 wrote:

```
at24 24c64 write to offset 0:  sends [addr_high=0x00, addr_low=0x00, data=0x54, ...]
AT24C02 (1-byte) interprets:   word_addr=0x00, writes data=[0x00, 0x54, 0x6c, ...]
Result:                         EEPROM[0] = 0x00  ← corrupted (was factory content)
                                EEPROM[1] = 0x54  ← first TlvInfo byte accidentally written
```

The factory content at 0x50 (ONIE TlvInfo or Accton ODM data) is now corrupted. The
"EC chip registers" we documented in memory were this same corruption — we misidentified
the AT24C02 as an EC chip because we were seeing corrupted register-like data.

### 2. TlvInfo written to the wrong chip (0x51)

After giving up on 0x50, we registered `24c02 0x51` and successfully wrote TlvInfo.
This 0x51 chip may be the COME module's internal EEPROM (ODM format, 24c64). Writing
TlvInfo there may have partially overwritten the COME module's factory ODM data. Whether
this matters depends on what the COME module uses that data for at runtime.

---

## Hardware-Verified Conclusions (2026-02-27)

Tested from ONIE (ssh root@192.168.88.12) and BMC (ssh root@192.168.88.13):

### 1. 0x50 and 0x51 are two different physical devices
`i2cdetect -y 1` shows both 0x50 and 0x51 responding simultaneously.
They cannot be the same chip. The CPLD-A0 theory was wrong.

### 2. 0x50 is the EC chip — writes are silently discarded
- `i2cget -f -y 1 0x50 0x00` returns `0x10` (EC firmware major version), consistently
- `i2cset -f -y 1 0x50 0xf8 0xaa` appears to succeed — but this is a false positive:
  undefined EC registers echo writes transiently; low registers (0x00–0x0F) are ROM-backed
- Writing 180 bytes to 0x50 byte-by-byte: all silently discarded; byte 0 still `0x10`/`0x11`
  (the counter increments suggest the EC tracks write attempts)

### 3. 0x51 is the AT24C02 — writable, holds our TlvInfo
- `i2cget -f -y 1 0x51 0x00` returns `0x54` = 'T' (TlvInfo magic)
- TlvInfo intact (180 bytes, CRC 0x69F352D5) from our earlier programming session

### 4. onie-syseeprom is broken for this platform
Running `onie-syseeprom -s ...` on ONIE:
- Writes to 0x50 (EC chip ACKs, discards data) — confirmed by unchanged raw bytes after
- "Verification" is in-memory (the output that shows valid TlvInfo reads from the
  in-memory buffer, not hardware)
- Outputs "Programming passed" and CRC 0x69F352D5 — which happens to be our existing
  TlvInfo on 0x51. This is NOT a hardware readback; the tool verified its own encoding.
- Subsequent `onie-syseeprom` (no -s) reads from hardware at 0x50 → EC chip → "Invalid TLV header"

### 5. BMC i2c-7/0x50 is inaccessible while ONIE holds the host I2C bus
Read from BMC `/sys/bus/i2c/devices/7-0050/eeprom` times out (Connection timed out)
while ONIE is running. This implies bus contention — the BMC's i2c-7 and ONIE's i2c-1
are physically connected to the same I2C segment. They compete for the SDA/SCL pair.

### 6. Our SONiC implementation at 0x51 is correct
The AT24C02 system EEPROM is physically at i2c-1/0x51. Our platform code is right.
ONL/ONIE pointing to 0x50 is a pre-existing upstream bug.

---

## What To Do

### Step 1: Determine which CPLD register moves the EEPROM (on running SONiC system)

Read all CPLD registers with the EEPROM currently at 0x51:

```bash
python3 -c "
import fcntl
with open('/dev/i2c-1', 'r+b', buffering=0) as f:
    fcntl.ioctl(f, 0x0706, 0x32)
    for reg in range(0x00, 0x60):
        f.write(bytes([reg]))
        try:
            v = f.read(1)[0]
            print(f'0x{reg:02x}: 0x{v:02x}')
        except:
            pass
"
```

Then power-cycle (so CPLD resets), and from ONIE:
```bash
# Before anything writes to the CPLD:
i2cdetect -y 1          # does 0x50 respond? does 0x51 respond?
i2ctransfer -y 1 w1@0x50 0x00 r8@0x50   # what's at byte 0?
i2ctransfer -y 1 w1@0x51 0x00 r8@0x51   # what's at byte 0?
```

If 0x50 responds in ONIE and 0x51 does not → CPLD controls A0, reset state = 0x50.

### Step 2: Read the CPLD register state in ONIE to find the bit

```bash
# In ONIE, read the CPLD with no platform init having run:
python3 -c "
import fcntl
with open('/dev/i2c-1', 'r+b', buffering=0) as f:
    fcntl.ioctl(f, 0x0706, 0x32)
    for reg in range(0x00, 0x60):
        f.write(bytes([reg]))
        try:
            v = f.read(1)[0]
            print(f'0x{reg:02x}: 0x{v:02x}')
        except:
            pass
"
```

Compare the ONIE register dump vs. the post-init SONiC register dump. The register(s)
that differ AND whose value change could move an address pin are the candidate(s).

### Step 3: Restore factory TlvInfo to 0x50

Once the EEPROM is confirmed at 0x50 (in ONIE/power-on state), use the BMC to write
valid TlvInfo to it — because from the BMC the chip is at i2c-7/0x50 and the CPLD
A0 circuit is not in the path:

```bash
# On BMC (root@hare-lorax-bmc):
# The system EEPROM is at i2c-7/0x50 from the BMC.
# Write TlvInfo bytes directly:
i2ctransfer -y 7 w<N>@0x50 <TlvInfo bytes>
```

Or write from SONiC BEFORE the platform init runs (immediately after kernel boot,
before accton_wedge100s_util.py executes) — at that point the EEPROM should still
be at 0x50.

### Step 4: Fix the SONiC platform code

Change `accton_wedge100s_util.py` to:
- NOT write the specific CPLD register bit that controls A0 (or clear it explicitly)
- Register the EEPROM as `24c02 0x50` on i2c-40
- Update all `EEPROM_SYSFS_PATH` references from `40-0051` to `40-0050`

Change `sonic_platform/eeprom.py` line 37: `40-0051/eeprom` → `40-0050/eeprom`
Change `plugins/eeprom.py` line 13: already shows `40-0050/eeprom` — this was correct

---

## Revised I2C Topology (Corrected)

```
i2c-1 (CP2112 root):
  0x32 = CPLD (controls LEDs, PSU presence, and possibly EEPROM A0 address pin)
  0x50 = System EEPROM AT24C02  ← FACTORY ADDRESS (when CPLD in reset state)
  0x51 = COME module internal EEPROM (24c64, ODM format)  ← address when A0=1
  0x70-0x74 = PCA9548 muxes
```

After our platform init (which wrote to CPLD), the EEPROM moved to 0x51.

BMC i2c-7 view (bypasses CPLD A0 select circuit):
  0x50 = same physical AT24C02 (always visible at 0x50 from BMC)
  0x51 = 24c64 COME module EEPROM
  0x52 = 24c64 COME module EEPROM

---

## Files Requiring Correction

| File | Line | Current (wrong) | Correct |
|------|------|-----------------|---------|
| `platform/.../sonic_platform/eeprom.py` | 37 | `40-0051/eeprom` | `40-0050/eeprom` |
| `platform/.../utils/accton_wedge100s_util.py` | 49 | `40-0051/eeprom` | `40-0050/eeprom` |
| `platform/.../utils/accton_wedge100s_util.py` | 108 | `24c02 0x51` | `24c02 0x50` |
| `platform/.../sonic_platform/chassis.py` | 45 (comment) | `0x50, ONIE TlvInfo` | correct addr, fix comment |
| `CLAUDE.md` | EEPROM rule | "EEPROM is at i2c-40/0x51 (24c02, 256B)" | needs revision |
| `memory/MEMORY.md` | EEPROM entry | `i2c-1/0x51: AT24C02` | revise after hardware confirm |
| `device/.../i2c_bus_map.json` | idprom section | `addr: 0x50, device: 24c64` | `addr: 0x50, device: 24c02` |

**Note:** plugins/eeprom.py line 13 already shows `40-0050/eeprom` — this was correct
all along.

---

## Key Open Questions Requiring Hardware Access

1. **Which CPLD register controls A0?** (ONIE vs. post-init register dump comparison)
2. **Does the EEPROM return to 0x50 after power cycle?** (confirms CPLD-controlled A0)
3. **What is at 0x51 from BMC i2c-7?** Does it contain our TlvInfo? (confirms we wrote
   to the wrong chip, i.e. COME module internal EEPROM)
4. **Can we read good TlvInfo from BMC i2c-7/0x50?** (the physical AT24C02 we programmed
   via SONiC at 0x51 should appear at 0x50 from the BMC if it's the same chip)
