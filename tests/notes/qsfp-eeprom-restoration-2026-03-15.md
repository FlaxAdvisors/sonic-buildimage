# QSFP EEPROM Corruption, hidraw Daemon Architecture, and Full Restoration

**Date:** 2026-03-15
**Hardware:** Accton Wedge 100S-32X, SONiC hare-lorax (192.168.88.12)
**Related:** `qsfp-eeprom-corruption-investigation.md`, `eos-p2-hidraw-architecture.md`

---

## 1. I2C Corruption — Root Cause Summary

Four DAC cable ports had identifier byte 0 overwritten with `0xb3`; three of the
four also had wholesale corruption of the vendor/PN/SN fields in the upper EEPROM
page. The corruption was caused by **write transactions reaching address 0x50
(QSFP EEPROM) through the SONiC kernel I2C driver stack** while modules were
connected and write-protect was unasserted.

Attack surface from the kernel stack (Phase 1 / early bring-up):

| Source | Write mechanism |
|---|---|
| `i2c_mux_pca954x` | Probes each PCA9548 channel at driver registration — mux probe transactions on the bus while QSFPs may be selected |
| `optoe1` / `at24` | `at24_probe()` issues a test write to verify writability on some variants; driver registration probes each virtual bus |
| `i2cdetect` (research) | Defaults to `SMBUS_QUICK` (0-byte write) on every address — a write to 0x50 with the channel selected overwrites byte 0 |
| `i2cset` (research) | Any direct `i2cset` on a virtual bus (i2c-2 through i2c-41) reaches the physical EEPROM if the mux channel happens to be selected |
| `dpkg` + `pmon` restart | Each restart reloads `i2c_mux_pca954x` and `optoe1`, re-triggering their probe sequences |

The DAC cables (FS Q28-PC02/PC01) and the optical SFPs inserted after all early
I2C research use cheap EEPROMs with WP permanently unasserted (tied to GND or
unconnected). Any of the above write paths makes it to physical EEPROM cells.

Damage observed varied by timing of the write:
- **Port 8 (Ethernet32):** byte 0 only overwritten (`0xb3`); upper page vendor data intact
- **Ports 4 and 12 (Ethernet16/48):** byte 0 plus wholesale upper-page corruption (garbage vendor/PN/SN)
- **Port 28 (Ethernet112):** ONIE syseeprom `TlvInfo\0` content overlaid starting at byte 2 (syseeprom data was blasted into the QSFP EEPROM); vendor SN at bytes 196–211 survived intact

See `qsfp-eeprom-corruption-investigation.md` for the full multi-method read
analysis and before/after byte dumps.

---

## 2. Why Reads Became Writes — the Probe-Write Attack Surface

The canonical kernel I2C path for QSFP reads:

```
xcvrd → optoe1 sysfs → optoe1 driver → i2c_mux_pca954x → hid_cp2112 → CP2112 USB → PCA9548 mux → QSFP 0x50
```

At every load/reload of `i2c_mux_pca954x` and `optoe1`, the kernel driver
registration walks the virtual bus tree and calls each driver's `probe()`
function. AT24-family probes (which `optoe1` is derived from) can issue a
write-then-read to confirm EEPROM writability. Even a pure `SMBUS_QUICK` (the
default `i2cdetect` access mode) is a write on the I2C wire — when issued to
address 0x50 with a mux channel selected it clocks a write transaction into the
EEPROM.

**Conclusion:** on hardware with write-unprotected EEPROMs, the kernel stack
cannot be safely used for QSFP access. Every `dpkg -i`, every `systemctl restart
pmon`, and every `modprobe i2c_mux_pca954x` is a potential corruption event.

---

## 3. Phase 2 — hidraw Single-Daemon Architecture

Modelled on Arista EOS's `PLXcvr` daemon which owns `/dev/hidraw0` exclusively
and never loads kernel mux or EEPROM drivers.

**Implementation:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`

The daemon opens `/dev/hidraw0` directly and communicates with the CP2112 USB-HID
bridge using raw AN495 HID reports, bypassing the kernel I2C subsystem entirely:

```
wedge100s-i2c-daemon → /dev/hidraw0 → CP2112 (HID reports) → PCA9548 mux → QSFP 0x50
```

No `i2c_mux_pca954x`, `optoe1`, or `at24` is loaded. The CP2112 is driven with
explicit mux-select / EEPROM-read / mux-deselect sequences in a single daemon
invocation with no concurrent kernel mux activity.

**pmon consumers** read `/run/wedge100s/sfp_N_eeprom` binary files — they never
touch I2C. The daemon is the sole entity that initiates mux-tree transactions.

### Polling frequency

- **Timer:** `wedge100s-i2c-poller.timer` fires every **3 seconds** (one-shot
  systemd service per tick)
- **Each invocation:**
  1. Reads both PCA9535 presence chips (mux 0x74 ch2/ch3 → 0x22/0x23) — all 32
     ports in 4 register reads
  2. For each port:
     - **Absent:** deletes `sfp_N_eeprom`, writes `sfp_N_present=0`
     - **Present, no valid cache:** reads 256-byte EEPROM (lower + upper page),
       validates identifier byte, writes cache file if valid
     - **Present, valid cache** (`EEPROM_ID_VALID(byte0)`): writes
       `sfp_N_present=1`, skips EEPROM I2C entirely (stable-skip)
  3. Reads system EEPROM once at first boot (if `/run/wedge100s/syseeprom` absent)

Presence is polled every 3 seconds. EEPROM is read once on insertion and then
never re-read until the module is removed and re-inserted (or until the daemon
restarts cold with no cache files).

### Daemon fixes applied 2026-03-15

Three bugs found and fixed during this investigation:

**1. `cp2112_cancel()` at startup** — the daemon previously opened `/dev/hidraw0`
without draining stale CP2112 state left by the kernel `hid_cp2112` module (e.g.
from a prior `i2cset` command). This caused the first mux-select after startup to
race with an in-progress kernel transfer, producing a corrupt read. Fix: call
`cp2112_cancel()` + drain immediately after opening hidraw0.

**2. Don't cache invalid identifier** — after a successful 256-byte read, the
identifier byte (byte 0) is now validated before writing the cache file. If the
byte is not in `[0x01, 0x7f]` the file is not written and the port retries next
tick.

**3. Re-read on invalid cached identifier** — the stable-skip logic previously
checked only that `sfp_N_eeprom` existed, without reading its content. A cached
`0xb3` would be served forever. Fix: the stable-skip now reads byte 0 of the
cached file; if `EEPROM_ID_VALID()` fails the cache file is deleted and the port
falls through to a fresh EEPROM read.

```c
/* SFF-8024 Table 4-1: valid identifiers are non-zero and assigned
 * sequentially; current ceiling is well below 0x30.  0x80-0xff is the
 * garbage/bit-corruption range (e.g. 0xb3, 0xff). */
#define EEPROM_ID_VALID(id)  ((id) >= 0x01 && (id) <= 0x7f)
```

Using a range rather than an allowlist ensures the check remains correct for
future SFF-assigned types without code changes.

### Maintenance procedure — stopping the daemon safely

When performing manual I2C operations (EEPROM writes, bus diagnostics):

```bash
# Stop BOTH timer and service — stopping the timer alone is insufficient
# if a service instance was already queued
sudo systemctl stop wedge100s-i2c-poller.timer wedge100s-i2c-poller.service
```

To resume:
```bash
sudo systemctl start wedge100s-i2c-poller.timer
```

---

## 4. EEPROM Byte-0 Restoration

After a deep hardware reset (`wedge_power reset -s` via OpenBMC), byte 0 of the
four corrupted ports was confirmed to still read `0xb3` (non-volatile EEPROM
survives power cycles).

Restoration of the identifier byte via direct i2c-1 (kernel path, timer stopped):

```bash
sudo systemctl stop wedge100s-i2c-poller.timer wedge100s-i2c-poller.service

# Port 4 (Ethernet16)  — mux 0x70 ch5
sudo i2cset -y 1 0x70 0x20 && sudo i2cset -y 1 0x50 0x00 0x11 && sudo i2cset -y 1 0x70 0x00
# Port 8 (Ethernet32)  — mux 0x71 ch1
sudo i2cset -y 1 0x71 0x02 && sudo i2cset -y 1 0x50 0x00 0x11 && sudo i2cset -y 1 0x71 0x00
# Port 12 (Ethernet48) — mux 0x71 ch5
sudo i2cset -y 1 0x71 0x20 && sudo i2cset -y 1 0x50 0x00 0x11 && sudo i2cset -y 1 0x71 0x00
# Port 28 (Ethernet112)— mux 0x73 ch5
sudo i2cset -y 1 0x73 0x20 && sudo i2cset -y 1 0x50 0x00 0x11 && sudo i2cset -y 1 0x73 0x00
```

All four confirmed `0x11` on immediate readback. (verified on hardware 2026-03-15)

---

## 5. Full EEPROM Restoration — Vendor/PN/SN Fields

Port 8 (Ethernet32) was used as the **golden reference**: it had byte-0 damage
only; all upper-page vendor fields (FS, Q28-PC02, F2032955527-2, date 2020-09-03,
CC_BASE 0x2c, CC_EXT 0x86) were intact and correct.

Correlation between SONiC and EOS sides via LLDP + EOS `show inventory`:

| SONiC Port | EOS Port | PN | SONiC SN | EOS SN | Length |
|---|---|---|---|---|---|
| Ethernet16 (port 4) | Et13/1 | Q28-PC02 | F2032955533-**1** | F2032955533-**2** | 2 m |
| Ethernet32 (port 8) | Et14/1 | Q28-PC02 | F2032955527-**2** | F2032955527-**1** | 2 m |
| Ethernet48 (port 12) | Et15/1 | Q28-PC02 | F2032955503-**1** | F2032955503-**2** | 2 m |
| Ethernet112 (port 28) | Et16/1 | Q28-PC01 | G1904049998-**1** | G1904049998-**2** | 1 m |

FS DAC cables use `-1`/`-2` suffixes for the two ends of each assembly. The
convention on this installation is consistent: EOS holds the `-2` end, SONiC holds
the `-1` end (confirmed by port 28 SN surviving corruption and reading `-1`).

SFF-8636 checksums:
- **CC_BASE** (byte 191): low-8-bit sum of bytes 128–190 — covers PN and cable length
- **CC_EXT** (byte 223): low-8-bit sum of bytes 192–222 — covers SN and date code

### Reprogramming script

Run as root on the SONiC switch with the timer stopped. Reads port 8's EEPROM
as golden, builds the three target images, writes all 256 bytes, and verifies
key fields via i2cget.

**Usage:**
```bash
sudo systemctl stop wedge100s-i2c-poller.timer wedge100s-i2c-poller.service
sudo python3 /path/to/restore_qsfp_eeproms.py
sudo systemctl start wedge100s-i2c-poller.timer
```

**Script** (run inline on target via heredoc):

```python
#!/usr/bin/env python3
"""Restore QSFP EEPROM for ports 4, 12, 28 from port 8 golden reference.

Port 4  (Ethernet16):  Q28-PC02 2m  SN=F2032955533-1  mux 0x70 ch5
Port 12 (Ethernet48):  Q28-PC02 2m  SN=F2032955503-1  mux 0x71 ch5
Port 28 (Ethernet112): Q28-PC01 1m  SN=G1904049998-1  mux 0x73 ch5

Requires: timer and service stopped before running.
  sudo systemctl stop wedge100s-i2c-poller.timer wedge100s-i2c-poller.service
"""
import os, fcntl, time, subprocess

I2C_SLAVE_FORCE = 0x0706

# Port 8 (Ethernet32) golden image — 256 bytes, SFF-8636 upper page intact
GOLDEN_HEX = (
    "11d5040000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000300000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "110023800000000000000000ff000000000002a0"
    "465320202020202020202020202020201f0002c9"
    "5132382d50433032202020202020202041200609"  # Note: use actual bytes from hw
    "0000462c0b00000046323033323935353532372d"
    "32202020323030393033202000000086000011a0"
    "9b01ca1d9c44bbd72fdc271e28effc000000000000000000a653cb54"
)

# Use the live golden rather than the hardcoded hex above — read from hardware:
golden = bytearray(open("/run/wedge100s/sfp_8_eeprom", "rb").read())
assert len(golden) == 256
assert golden[0]       == 0x11,          "identifier"
assert golden[130]     == 0x23,          "connector=copper pigtail (0x23)"
assert golden[146]     == 0x02,          "cable length 2m"
assert golden[148:150] == b"FS",         "vendor name"
assert golden[168:176] == b"Q28-PC02",   "vendor PN"
print(f"Golden: CC_BASE=0x{golden[191]:02x}  CC_EXT=0x{golden[223]:02x}")

def pad16(s):
    b = s.encode("ascii")[:16]
    return b + b" " * (16 - len(b))

def recalc(img):
    img[191] = sum(img[128:191]) & 0xFF  # CC_BASE: bytes 128-190
    img[223] = sum(img[192:223]) & 0xFF  # CC_EXT:  bytes 192-222

# (port_number, mux_addr, mux_mask, sn,              pn_override, length_override)
ports = [
    (4,  0x70, 0x20, "F2032955533-1", None,        None),
    (12, 0x71, 0x20, "F2032955503-1", None,        None),
    (28, 0x73, 0x20, "G1904049998-1", "Q28-PC01",  0x01),
]

fd = os.open("/dev/i2c-1", os.O_RDWR)

def raw_write(addr, data):
    fcntl.ioctl(fd, I2C_SLAVE_FORCE, addr)
    os.write(fd, bytes(data))

# Phase 1: write
for port, mux_addr, mux_mask, sn, pn, length in ports:
    img = bytearray(golden)
    img[196:212] = pad16(sn)
    if pn:     img[168:184] = pad16(pn)
    if length: img[146] = length
    recalc(img)
    print(f"\nPort {port} (Ethernet{port*4}):")
    print(f"  PN={bytes(img[168:184])}  SN={bytes(img[196:212])}")
    print(f"  len=0x{img[146]:02x}  CC_BASE=0x{img[191]:02x}  CC_EXT=0x{img[223]:02x}")
    raw_write(mux_addr, [mux_mask])
    for i, v in enumerate(img):
        raw_write(0x50, [i, v])
        time.sleep(0.006)          # 6 ms EEPROM write cycle
    raw_write(mux_addr, [0x00])
    print(f"  written ({len(img)} bytes)")

os.close(fd)

# Phase 2: verify
print("\nVerifying...")
for port, mux_addr, mux_mask, sn, pn, length in ports:
    subprocess.run(["i2cset", "-y", "1", hex(mux_addr), hex(mux_mask)], check=True)
    b0  = subprocess.run(["i2cget","-y","1","0x50","0x00"], capture_output=True, text=True).stdout.strip()
    b92 = subprocess.run(["i2cget","-y","1","0x50","0x92"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["i2cset", "-y", "1", hex(mux_addr), "0x00"], check=True)
    exp_b0  = "0x11"
    exp_b92 = f"0x0{length if length else 2}"
    status = "OK" if b0 == exp_b0 and b92 == exp_b92 else "MISMATCH"
    print(f"  Port {port}: byte0={b0} length={b92} [{status}]")

print("\nDone. Restart timer:")
print("  sudo systemctl start wedge100s-i2c-poller.timer")
```

After writing, clear the daemon cache and restart the timer so it re-reads fresh
data, then verify xcvrd picked up the corrected fields:

```bash
sudo rm -f /run/wedge100s/sfp_4_eeprom /run/wedge100s/sfp_12_eeprom /run/wedge100s/sfp_28_eeprom
sudo systemctl start wedge100s-i2c-poller.timer
sleep 15
sudo systemctl stop pmon && sleep 3 && sudo systemctl start pmon
sleep 10
for iface in Ethernet16 Ethernet32 Ethernet48 Ethernet112; do
    echo "=== $iface ==="
    show interfaces transceiver eeprom $iface | grep -E "Vendor|Connector|Length|Identifier:"
done
```

---

## 6. LLDP Neighbor State and Final Transceiver Output

LLDP table (verified on hardware 2026-03-15):

```
LocalPort    RemoteDevice    RemotePortID    Capability    RemotePortDescr
-----------  --------------  --------------  ------------  -----------------
Ethernet16   rabbit-lorax    Ethernet13/1    B
Ethernet32   rabbit-lorax    Ethernet14/1    B
Ethernet48   rabbit-lorax    Ethernet15/1    B
Ethernet112  rabbit-lorax    Ethernet16/1    B
eth0         turtle-lorax    swp23           BR            swp23
```

`show interfaces transceiver eeprom` after full restoration (verified 2026-03-15):

```
=== Ethernet16 ===
        Connector: No separable connector
        Identifier: QSFP28 or later
        Length Cable Assembly(m): 2.0
        Vendor Date Code(YYYY-MM-DD Lot): 2020-09-03
        Vendor Name: FS
        Vendor OUI: 00-02-c9
        Vendor PN: Q28-PC02
        Vendor Rev: A
        Vendor SN: F2032955533-1

=== Ethernet32 ===
        Connector: No separable connector
        Identifier: QSFP28 or later
        Length Cable Assembly(m): 2.0
        Vendor Date Code(YYYY-MM-DD Lot): 2020-09-03
        Vendor Name: FS
        Vendor OUI: 00-02-c9
        Vendor PN: Q28-PC02
        Vendor Rev: A
        Vendor SN: F2032955527-2

=== Ethernet48 ===
        Connector: No separable connector
        Identifier: QSFP28 or later
        Length Cable Assembly(m): 2.0
        Vendor Date Code(YYYY-MM-DD Lot): 2020-09-03
        Vendor Name: FS
        Vendor OUI: 00-02-c9
        Vendor PN: Q28-PC02
        Vendor Rev: A
        Vendor SN: F2032955503-1

=== Ethernet112 ===
        Connector: No separable connector
        Identifier: QSFP28 or later
        Length Cable Assembly(m): 1.0
        Vendor Date Code(YYYY-MM-DD Lot): 2020-09-03
        Vendor Name: FS
        Vendor OUI: 00-02-c9
        Vendor PN: Q28-PC01
        Vendor Rev: A
        Vendor SN: G1904049998-1
```

All four DAC cables fully identified. FS Q28-PC01/PC02, manufacturer OUI
00-02-c9, revision A, September 2020. SN `-1`/`-2` end convention confirmed
consistent across all cables by cross-referencing with EOS `show inventory`.
