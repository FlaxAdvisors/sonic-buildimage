# QSFP EEPROM Data Investigation — Probable SONiC Driver-Stack Corruption

**Date:** 2026-03-14
**Context:** Follow-up to EOS-like I2C daemon implementation (see `EOS-LIKE-PLAN.md`, `phase-r29-python-api-daemon-files.md`).

## Problem Statement

`show interfaces transceiver eeprom` showed garbled/corrupt EEPROM data for all
present QSFP ports (Ethernet0, 16, 32, 48, 64, 80, 104, 108, 112).

LLDP confirms SONiC Ethernet16/32/48/112 ↔ EOS rabbit-lorax Ethernet13/1–16/1
via FS Q28-PC02/PC01 DAC cables. Both ends of the same physical cable were
compared using EOS `show interfaces transceiver detail`.

## Read Methods Tested (all return byte-for-byte identical data)

1. optoe1 sysfs — `fopen /sys/bus/i2c/devices/7-0050/eeprom`
2. Direct `i2c-dev` I2C_RDWR on `/dev/i2c-7` — two transactions (lower + upper)
3. SMBus I2C_BLOCK_DATA (32 bytes/chunk) on `/dev/i2c-7`
4. Two-transaction with explicit page-select write (reg 0x7F = 0) before upper read
5. **CP2112 bus 1 direct** — manually selected PCA9548 0x70 channel 5, held mux
   channel through page-select write and upper-page read, no kernel mux driver
   in the path, pmon and wedge100s-i2c-daemon stopped

All five methods produced identical bytes. Race conditions and concurrent I2C
traffic were fully eliminated.

## Key Technical Notes

- `flat_mem` is **bit 0** of byte 2 (SFF-8636), NOT bit 2. Byte 2 = 0x04 means:
  - bit 2 = IntL = 1 (interrupt not asserted — normal)
  - bit 0 = flat_mem = 0 → **paged memory** (page select register at 0x7F applies)
- The CP2112 does NOT support 3-message I2C_RDWR (write + write + read) — returns
  EOPNOTSUPP. Max is 2-message write-then-read.
- `idle_state = -1` on all PCA9548 muxes = MUX_IDLE_DISCONNECT (equivalent to
  `force_deselect_on_exit=1`). Not the cause of bad reads.
- PCA9548 0x70 channel mapping (bus 1 = CP2112):
  - channel 0 → bus 2 (port 1)
  - channel 1 → bus 3 (port 0)
  - channel 5 → bus 7 (port 4, Ethernet16) ← DAC cable to EOS Ethernet13/1

## Findings: SONiC-End vs EOS-End EEPROM Content

Port 4 (Ethernet16, bus 7) — same physical DAC cable, opposite connectors:

| Field | SONiC end (port 4) | EOS end (Ethernet13/1) |
|---|---|---|
| Connector (byte 130) | `0x21` (RJ45 — incorrect) | `0x00` (unspecified/pigtail) |
| Compliance (byte 131) | `0x08` | `0x80` (100GBASE-CR4) |
| Ext compliance (byte 192) | `0x0b` (100G Passive Copper) | `0x11` (100GBASE-CR4) |
| Vendor (bytes 148–163) | `b'Ad\x60\x60\x60,\x02@...'` | `b'FS              '` |
| Date code (bytes 212–219) | `000101  ` (Jan 1, 2000) | `200903  ` (Sept 2020) |
| CC_BASE (byte 191) | computed=`0x71` ≠ stored=`0x20` | `0x2C`/`0x2C` ✓ valid |
| CC_EXT (byte 223) | computed=`0xbe` ≠ stored=`0x00` | `0x83`/`0x83` ✓ valid |

All 9 present ports have invalid CC_BASE and CC_EXT checksums with similar
garbage vendor names.

## Full Raw Dump — Port 4 (Ethernet16, bus 7), pmon+daemon stopped

```
000: 11 05 04 00 00 00 00 00 00 00 00 00 00 00 00 00
010: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
...
070: 00 40 00 00 00 00 00 00 00 00 00 00 00 00 00 00
080: 11 00 21 08 00 00 00 00 00 00 00 01 2a 00 00 00   ← upper page 00h start
090: 00 00 00 20 41 64 60 60 60 2c 02 40 00 20 00 00   ← vendor bytes 148+
0a0: 20 20 20 20 00 28 07 14 02 04 00 10 03 02 2c 04
0b0: 21 30 10 00 20 20 20 20 40 20 04 02 00 00 44 20
0c0: 0b 00 00 00 40 10 00 30 30 30 30 31 30 30 30 20   ← ext_compliance=0x0b, serial "00001000"
0d0: 00 20 20 20 30 30 30 31 30 31 20 20 00 00 00 00   ← date "000101" (factory default)
0e0: 00 00 11 a0 9b 01 ca 1d 9c 44 bb d7 05 10 03 1e
0f0: 28 ef fc 00 00 00 00 00 00 00 00 00 a6 53 cb 54
```

Byte 192 = `0x0b` = "100G Passive Copper Alloy Cable" (SFF-8024 Table 4-4) — this
field is at least plausible for a DAC cable, but the checksum bytes (191, 223)
are wrong relative to the stored data, meaning the EEPROM was programmed with
inconsistent content (data bytes do not match their own checksum).

## Optical SFP Ports — Follow-up Read (2026-03-14)

Ethernet104 and Ethernet108 carry optical transceivers inserted **after** all early
I2C research work was complete. They were read with pmon and wedge100s-i2c-daemon
stopped to eliminate contention (same methodology as DAC cable tests above).

| Field | Ethernet104 (bus 29) | Ethernet108 (bus 28) |
|---|---|---|
| Identifier | 0x11 (QSFP28) ✓ | 0x11 (QSFP28) ✓ |
| Connector | 0x01 (SC) | 0x01 (SC) |
| Compliance | 0x00 | 0x00 |
| Ext compliance | 0x02 | 0x02 |
| Vendor | `@@@@@` (garbage) | `Ad\x60\x60\x60` (garbage) |
| Date code | `000000` (factory zero) | `000100` (factory default) |
| CC_BASE | stored=0x20 computed=0xb7 **MISMATCH** | stored=0x20 computed=0x6b **MISMATCH** |
| CC_EXT | stored=0x00 computed=0xe2 **MISMATCH** | stored=0x00 computed=0x04 **MISMATCH** |

The `Ad\x60\x60\x60` vendor prefix in Ethernet108 is identical to the pattern seen
in the DAC cables. Both optical ports show the same failure mode: valid QSFP28
identifier byte, garbage vendor string, factory-default date code, and checksum
bytes that do not match the stored fields.

**The "factory defect" conclusion is not credible.** The user correctly identified
that it is statistically implausible for four independently inserted DAC cables to
have all landed with their corrupt connector on the SONiC side, and for two
separately sourced optical transceivers to show the identical corruption pattern.
The prior conclusion was revised after investigation of the Arista EOS xcvr stack.

## Revised Conclusion — SONiC Kernel Driver Stack as Probable Cause

Examination of Arista EOS on the peer Wedge100S revealed a fundamentally
different I2C architecture:

**EOS I2C stack (non-destructive):**
- `PLXcvr` daemon owns `/dev/hidraw0` exclusively and talks to the CP2112 using
  raw HID reports (CP2112 AN495 protocol) — no kernel I2C subsystem involved
- No `hid_cp2112`, no `i2c_mux_pca954x`, no `optoe1` in the kernel at all
- The mux is selected, page is set, and EEPROM is read in a controlled atomic
  sequence with no intervening deselect
- EOS has been running 7+ weeks on this hardware with no EEPROM corruption

**SONiC I2C stack (write-capable at multiple points):**
- `hid_cp2112` takes ownership of CP2112 and exposes `/dev/i2c-1`
- `i2c_mux_pca954x` probes each PCA9548 channel at driver registration —
  channel probe transactions are sent on the bus while QSFPs may be selected
- `optoe1` (AT24-family driver) probes each virtual bus at registration; some
  AT24 variants issue a test write during `at24_probe` to verify writability
- `i2cdetect` run during early research defaults to `SMBUS_QUICK` (0-byte write)
  on each address — at address 0x50 this constitutes a write transaction
- Any `i2cset` command issued during research on a virtual bus (i2c-2 through
  i2c-41) reaches the physical EEPROM at 0x50 if the mux channel is selected

The QSFP EEPROMs on these budget transceivers appear to have their WP (write
protect) pin unasserted or permanently tied to write-enable. Any of the above
write paths reaching address 0x50 while a QSFP is connected would overwrite
physical EEPROM cells. The corruption pattern (garbage in vendor info area,
factory-default date codes, mismatched checksums) is consistent with partial
overwrite of the upper page, not with factory mis-programming.

The optical SFPs inserted "after the research phase" were still exposed to
`i2c_mux_pca954x` and `optoe1` probe writes at driver registration, which
happens every time the platform module is loaded or pmon starts — explaining
why they show the same damage despite later insertion.

Physical link operation is unaffected. The corruption is metadata-only.

## Next Step

See `tests/notes/qsfp-eeprom-fresh-approach.md` for the plan to collect the
EOS tooling, decompile it, and reproduce the safe hidraw-direct I2C approach
on SONiC — eliminating the kernel driver probe attack surface entirely.

## Verified on Hardware

- 2026-03-14: all 5 read methods tested on DAC cables; CP2112 direct access with
  manual mux hold confirmed, pmon and daemon stopped during testing
- 2026-03-14: optical SFPs at Ethernet104 (bus 29) and Ethernet108 (bus 28) read
  with pmon and daemon stopped; same corruption pattern confirmed across both
  transceiver types
- 2026-03-14: EOS peer (192.168.88.14, EOS 4.27.0F, uptime 7+ weeks) confirmed
  to use PLXcvr → /dev/hidraw0 exclusively; no kernel mux driver; no EEPROM
  corruption observed on EOS side despite identical hardware
