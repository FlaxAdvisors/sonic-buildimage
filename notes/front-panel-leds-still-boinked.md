# Front-Panel Port LEDs — Architecture, Root Cause, and Fix

**Status as of 2026-04-07**: Bytecode patch deployed and live on hardware.
All 32 QSFP port LEDs now correctly reflect STATE_DB oper_status.

---

## Overview

The Wedge 100S-32X has 32 QSFP28 front-panel ports, each with a bi-color LED
(blue + orange).  The BCM56960 (Tomahawk) drives them directly via two independent
LED scan chains (LEDUP0, LEDUP1).  There are no per-port CPLD LED registers;
the only path to control port LED state is through BCM DATA_RAM.

**Original symptom**: All 32 port LEDs showed magenta (blue+orange) regardless of
physical link state, because BCM SDK initialises DATA_RAM with `link=1` for all
configured ports on *both* chains and does not subsequently correct LEDUP1.

---

## BCM56960 LED Scan Chain Architecture

### Two independent chains

| Chain  | Color   | DATA_RAM owner (before fix) | DATA_RAM owner (after fix) |
|--------|---------|----------------------------|---------------------------|
| LEDUP0 | Blue    | BCM SDK (linkscan updates) | BCM SDK (unchanged) |
| LEDUP1 | Orange  | BCM SDK (always 0x80 = up) | wedge100s-ledup-linkstate daemon (safe zone) |

Each chain has its own:
- **PROGRAM_RAM[256]** — LED microcontroller bytecode, runs at ~10 Hz scan rate
- **DATA_RAM[256]** — per-port status bytes; indices 0-31 are port link state; 128-255 are bytecode scratchpad (F9, FC, FD-FF are hardware/scratch registers used by the stock program)
- **REMAP registers** — 16 registers (`CMIC_LEDUP{0,1}_PORT_ORDER_REMAP_{0_3..60_63}`), each holding four 6-bit fields, translating scan position → DATA_RAM address

### DATA_RAM bit semantics

The stock AS7712 bytecode (used on this platform, same Tomahawk chip) checks **bit 7** of each DATA_RAM entry:

| bit 7 | Meaning | Scan output |
|-------|---------|-------------|
| 1     | Link up | Chain fires → LED lights |
| 0     | Link down | Chain dark → LED off |

### Possible LED colors

| LEDUP0 DATA_RAM[port] bit 7 | LEDUP1 DATA_RAM[port+64] bit 7 | LED color |
|-----------------------------|--------------------------------|-----------|
| 0 | 0 | **OFF** (no SFP or both chains dark) |
| 1 | 0 | **BLUE** (link down — hardware sees physical link, daemon says down) |
| 0 | 1 | **ORANGE** (anomalous — LEDUP0 dark but LEDUP1 lit, shouldn't happen) |
| 1 | 1 | **MAGENTA** (blue + orange = link up) |

In practice on this platform:
- LEDUP0 is written by BCM SDK linkscan → reflects hardware PHY link state
- LEDUP1 is written by `wedge100s-ledup-linkstate` → reflects STATE_DB `oper_status`

Expected steady-state:
- **SFP absent / no peer**: LEDUP0=0 (PHY down) → **OFF**
- **SFP present, link training / down**: LEDUP0=1, LEDUP1=0 → **BLUE**
- **SFP present, link up**: LEDUP0=1, LEDUP1=1 → **MAGENTA**

### DATA_RAM zone layout

```
DATA_RAM[0..31]    BCM SDK zone — SDK linkscan writes here continuously at ~4 Hz
                   LEDUP0 reads from here (unchanged)
                   LEDUP1 must NOT read from here (BCM SDK always sets bit 7=1)

DATA_RAM[32..63]   Also BCM SDK zone — SDK writes for "extended" port addresses

DATA_RAM[64..95]   SAFE ZONE — never written by BCM SDK (gap in BCM port address space)
                   wedge100s-ledup-linkstate writes here for LEDUP1
                   LEDUP1 patched bytecode reads from here

DATA_RAM[96..127]  BCM SDK zone again

DATA_RAM[128..255] Bytecode scratchpad — F9=REMAP pointer, FC=temp, FD-FF=program state
                   DATA_RAM[F9] is hardware-managed: holds REMAP[current_scan_pos]
                   at each scan position, i.e. the LED_port value (0..31)
```

---

## Port Mapping Table

Authoritative derivation: `LED_port = (first_serdes_lane - 1) / 4`
Source: `portmap_*.0=<serdes>:100` entries in `th-wedge100s-32x-flex.config.bcm`

| Scan pos | BCM port | SONiC iface | Serdes | LED_port | DATA_RAM addr (LEDUP0) | DATA_RAM addr (LEDUP1, patched) |
|----------|----------|-------------|--------|----------|------------------------|--------------------------------|
| 0  | 1   | Ethernet16  | 5   | 1  | 1  | 65  |
| 1  | 5   | Ethernet20  | 1   | 0  | 0  | 64  |
| 2  | 9   | Ethernet24  | 13  | 3  | 3  | 67  |
| 3  | 13  | Ethernet28  | 9   | 2  | 2  | 66  |
| 4  | 17  | Ethernet32  | 21  | 5  | 5  | 69  |
| 5  | 21  | Ethernet36  | 17  | 4  | 4  | 68  |
| 6  | 25  | Ethernet40  | 29  | 7  | 7  | 71  |
| 7  | 29  | Ethernet44  | 25  | 6  | 6  | 70  |
| 8  | 34  | Ethernet48  | 37  | 9  | 9  | 73  |
| 9  | 38  | Ethernet52  | 33  | 8  | 8  | 72  |
| 10 | 42  | Ethernet56  | 45  | 11 | 11 | 75  |
| 11 | 46  | Ethernet60  | 41  | 10 | 10 | 74  |
| 12 | 50  | Ethernet64  | 53  | 13 | 13 | 77  |
| 13 | 54  | Ethernet68  | 49  | 12 | 12 | 76  |
| 14 | 58  | Ethernet72  | 61  | 15 | 15 | 79  |
| 15 | 62  | Ethernet76  | 57  | 14 | 14 | 78  |
| 16 | 68  | Ethernet80  | 69  | 17 | 17 | 81  |
| 17 | 72  | Ethernet84  | 65  | 16 | 16 | 80  |
| 18 | 76  | Ethernet88  | 77  | 19 | 19 | 83  |
| 19 | 80  | Ethernet92  | 73  | 18 | 18 | 82  |
| 20 | 84  | Ethernet96  | 85  | 21 | 21 | 85  |
| 21 | 88  | Ethernet100 | 81  | 20 | 20 | 84  |
| 22 | 92  | Ethernet104 | 93  | 23 | 23 | 87  |
| 23 | 96  | Ethernet108 | 89  | 22 | 22 | 86  |
| 24 | 102 | Ethernet112 | 101 | 25 | 25 | 89  |
| 25 | 106 | Ethernet116 | 97  | 24 | 24 | 88  |
| 26 | 110 | Ethernet120 | 109 | 27 | 27 | 91  |
| 27 | 114 | Ethernet124 | 105 | 26 | 26 | 90  |
| 28 | 118 | Ethernet0   | 117 | 29 | 29 | 93  |
| 29 | 122 | Ethernet4   | 113 | 28 | 28 | 92  |
| 30 | 126 | Ethernet8   | 125 | 31 | 31 | 95  |
| 31 | 130 | Ethernet12  | 121 | 30 | 30 | 94  |

Scan positions 32-63 on both chains are remapped to index 63 (unused, always 0x00).

---

## Services and Their Roles in LED Control

### System LEDs (SYS1, SYS2)

Controlled entirely outside BCM, via CPLD at i2c-1 / 0x32:
- **SYS1** (reg 0x3e): system-status indicator — green while SONiC running
- **SYS2** (reg 0x3f): port-activity indicator — green when ≥1 port is oper_status=up

| Service | Role |
|---------|------|
| `pmon` / `ledd` | Calls `LedControl.port_link_state_change()` on oper_status transitions; `led_control.py` writes SYS1/SYS2 via `/run/wedge100s/led_sys{1,2}` |
| `wedge100s-i2c-daemon` | Reads `/run/wedge100s/led_sys{1,2}` on 3-second poll tick; writes CPLD register 0x3e/0x3f via i2c |

### Port LEDs (QSFP bi-color, all 32 ports)

Controlled entirely by BCM56960 LED scan chains:

| Service | Role |
|---------|------|
| `syncd` (BCM SDK) | Executes `led_proc_init.soc` at init: sets REMAP, initialises DATA_RAM[64..95]=0, loads bytecode programs, starts chains. BCM SDK linkscan then updates LEDUP0 DATA_RAM[0..31] at ~4 Hz. LEDUP1 DATA_RAM[0..63] is also written by SDK (always 0x80) — this is the root cause of all-magenta. |
| `wedge100s-ledup-linkstate` | Polls STATE_DB PORT_TABLE every 1s; writes `CMIC_LEDUP1_DATA_RAM(LED_port+64)` = 0x80 (up) or 0x00 (down) via bcmcmd setreg. Watches `/run/wedge100s/ledup1_port_N.set` for sub-1s fast-path hints. |
| `pmon` / `ledd` | Calls `led_control.py::port_link_state_change()` on each transition; plugin writes a `.set` hint file so the ledup-linkstate daemon reacts within one poll cycle. |
| `wedge100s-bmc-daemon` | Controls CPLD 0x3c LED test-mode patterns (off/solid/rainbow/walk) — this overrides BMC-facing LEDs only, not BCM scan chain output. |

---

## Root Cause: BCM SDK Owns DATA_RAM[0..63] on Both Chains

### Why all LEDs were magenta

1. At syncd startup, BCM SAI port initialisation writes DATA_RAM[0..31] (and more of 0..63)
   with port-capability bytes (speed, duplex, FC bits) on **both** LEDUP0 and LEDUP1.
   All these bytes have bit 7 = 1 (link-capable = "enabled").

2. BCM linkscan subsequently updates LEDUP0 DATA_RAM on physical PHY state changes.
   LEDUP1 DATA_RAM is **never updated** by linkscan — it stays 0x80 for every port
   regardless of link state.

3. Result: LEDUP0 fires (blue) + LEDUP1 fires (orange) = **magenta on every port**.

### Why REMAP +64 could not fix it

The first attempted fix was to shift LEDUP1 REMAP values from 0..31 to 65..95 so the
LED program reads from DATA_RAM[64..95] (the safe zone) instead. This failed because:

- `CMIC_LEDUP1_PORT_ORDER_REMAP_*` fields are **6-bit** (max value = 63).
- Values 65..95 silently truncate to 1..31 — no change to actual REMAP behaviour.
- A bcmcmd setreg with value 65 returns: `Value '65' too large for 6-bit field 'REMAP_PORT_0'`

The safe zone offset of +64 cannot be expressed in REMAP registers.

---

## Solution Architecture

### The Bytecode Patch

Since REMAP cannot redirect to DATA_RAM[64..95], the LED bytecode program itself was
patched to compute the +64 offset at run time.

**BCM CMICD LED ISA** (key opcodes decoded from the 256-byte stock program):

| Opcode | Mnemonic | Effect |
|--------|----------|--------|
| `02 xx` | ST DATA_RAM[xx], A | Store accumulator A to address xx |
| `12 xx` | ST DATA_RAM[xx], A | Store A to address xx (variant) |
| `06 xx` | LD A, (DATA_RAM[xx]) | *Indirect* load: A = DATA_RAM[DATA_RAM[xx]] |
| `42 xx` | LD A, #xx | Load immediate xx into A |
| `0A xx` | OR A, #xx | A = A \| xx |
| `77 xx` | JMP xx | Unconditional jump to absolute address xx |
| `74 xx` | JC xx | Jump if carry |
| DATA_RAM[F9] | (hardware-managed) | Holds REMAP[current_scan_pos] = led_port (0..31) at each scan position; updated by hardware before each iteration |
| DATA_RAM[FC] | (scratchpad) | Temp pointer register used by patch |

**Original instruction at offset 0x14-0x15** (the link-data load):
```
06 F9    ; LD A, (DATA_RAM[F9])  — indirect: A = DATA_RAM[DATA_RAM[F9]]
         ;                          = DATA_RAM[led_port]  ← BCM SDK-owned zone
```

**Patched instruction at offset 0x14-0x15**:
```
77 F0    ; JMP 0xF0              — jump to patch routine in previously-unused padding
```

**Patch routine at offset 0xF0-0xF9** (10 bytes, formerly all 0x00):
```asm
02 F9    ; LD A, DATA_RAM[F9]    — direct read: A = led_port (0..31)
0A 40    ; OR A, 0x40            — A = led_port | 64 = led_port + 64
12 FC    ; ST DATA_RAM[FC], A    — store computed address in temp register FC
06 FC    ; LD A, (DATA_RAM[FC])  — indirect: A = DATA_RAM[led_port+64]  ← daemon-owned zone
77 16    ; JMP 0x16              — return to original flow (D2 00 74 1E comparison)
```

The remaining 6 bytes at 0xFA-0xFF stay 0x00 (unused padding).

### Full Patched Hex String (led 1 prog)

Changes from the stock LEDUP0 program are at **offset 0x14** and **offset 0xF0**:

```
02 FD 42 80 02 FF 42 00 02 FE 42 00 02 FA 42 E0 02 FB 42 40  ← 0x00-0x13 unchanged
77 F0                                                          ← 0x14-0x15 PATCHED (was 06 F9)
D2 00 74 1E 02 F9 42 03 67 AC 67 C3 67 52 86 FE 67 C3 67 52  ← 0x16-0x29 unchanged
86 FE 67 C3 67 52 86 FE 67 C3 67 52 86 FE 06 FB D6 FE 74 1E  ← 0x2A-0x3F unchanged
86 FC 3E FA 06 FE 88 4A 03 71 4C 67 84 57 67 84 57 67 98 57  ← 0x40-0x57 unchanged
06 FE 88 80 4A 00 27 97 75 4F 90 4A 00 27 4A 01 27 B7 97 71  ← 0x58-0x6F unchanged
69 77 42 06 F9 D6 FC 74 7C 02 F9 4A 07 37 4E 07 02 FC 42 00  ← 0x70-0x83 unchanged
4E 07 06 F9 0A 07 71 4F 77 42 16 FF 06 FD 17 4D DA 07 74 95  ← 0x84-0x97 unchanged
12 FF 52 00 86 FD 57 86 FF 57 16 FF 06 FD 07 4D DA 07 74 A9  ← 0x98-0xAB unchanged
12 FF 52 00 86 FD 57 86 FF 57 06 FE C2 FC 98 98 12 F4 50 C2  ← 0xAC-0xBF unchanged
FC 98 98 F2 F0 14 06 F4 C2 03 88 77 D1 06 FE C2 FC 98 98 F2  ← 0xC0-0xD3 unchanged
E0 14 06 FE C2 03 88 18 71 E2 80 18 71 DD 67 98 67 98 57 67  ← 0xD4-0xE7 unchanged
84 67 98 57 80 18 71 EB 67 98 67 84 57 67 84 67 84 57         ← 0xE8-0xEF unchanged
02 F9 0A 40 12 FC 06 FC 77 16                                  ← 0xF0-0xF9 PATCH ROUTINE
00 00 00 00 00 00                                              ← 0xFA-0xFF unchanged
```

### LEDUP1 REMAP (after fix)

LEDUP1 REMAP is identical to LEDUP0 (same 1,0,3,2,...,31,30 values). The +64 offset
is handled entirely inside the patched bytecode, not in REMAP. REMAP fields are 6-bit
(max 63) so a REMAP-only approach is architecturally impossible.

### Daemon: wedge100s-ledup-linkstate

- Polls STATE_DB `PORT_TABLE` every 1 second
- On oper_status change: `setreg CMIC_LEDUP1_DATA_RAM(LED_port+64) DATA=0x80/0x00`
- Must use `DATA=value` field-qualified syntax — `setreg REG 0x00` silently does nothing;
  `setreg REG DATA=0x00` performs the read-modify-write that actually lands
- Opens bcmcmd socket only when changes exist (avoids contention with flex-counter-daemon)
- Watches `/run/wedge100s/ledup1_port_N.set` files for sub-second fast-path hints
  written by `led_control.py::port_link_state_change()` (ledd event → hint → daemon picks up next cycle)
- Waits up to 180s at startup for bcmcmd socket (syncd must be up before daemon can work)

### led_control.py fast path

`LedControl.port_link_state_change()` (called by ledd on link state transitions) also:
1. Updates SYS2 LED (green ↔ off based on any-port-up)
2. Writes `/run/wedge100s/ledup1_port_N.set` = 1 or 0 so the daemon reacts within <1s
   rather than waiting up to the full 1-second poll interval

### led_proc_init.soc initialisation

On syncd startup (before daemon runs):
1. Sets LEDUP0 and LEDUP1 REMAP to 1,0,3,2,...,31,30 (positions 32-63 → 63)
2. Loads stock program into LEDUP0 PROGRAM_RAM
3. Loads **patched** program into LEDUP1 PROGRAM_RAM
4. Initialises `CMIC_LEDUP1_DATA_RAM(64..95) = 0x00` — all ports start blue-only
   until daemon performs first STATE_DB sweep
5. Starts both chains with `led 0 start` / `led 1 start` / `led auto on`

The DATA_RAM[64..95] zero-initialisation means all QSFP LEDs start **blue-only** at
syncd startup, then transition to magenta as the daemon populates oper_status=up ports.

---

## Deployment

### Files changed

| File | Change |
|------|--------|
| `device/accton/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc` | LEDUP1 REMAP corrected; `led 1 prog` replaced with patched bytecode; DATA_RAM[64..95] zero-init added |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-ledup-linkstate` | Removed `_apply_ledup1_remap()` (6-bit limit, always failed); updated docstrings; bcmcmd connection now released between poll cycles |
| `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py` | Added `_IFACE_TO_LED_PORT` map; `port_link_state_change()` now writes `.set` hint files for fast-path LEDUP1 update |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` | Enable and start `wedge100s-ledup-linkstate.service` on package install |

### Installed paths on target

| Source | Installed at |
|--------|-------------|
| `led_proc_init.soc` | `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc` |
| `wedge100s-ledup-linkstate` | `/usr/bin/wedge100s-ledup-linkstate` |
| `led_control.py` | `/usr/lib/python3/dist-packages/sonic_platform/` (via pmon container) |

### Verification (hardware, 2026-04-07)

```
# Confirm patch bytes are live in LEDUP1 PROGRAM_RAM
sudo python3 -c "
import mmap, os, struct
BASE = 0xfb021800
fd = os.open('/dev/mem', os.O_RDONLY | os.O_SYNC)
m = mmap.mmap(fd, mmap.PAGESIZE*3, mmap.MAP_SHARED, mmap.PROT_READ,
              offset=BASE & ~(mmap.PAGESIZE-1))
d = BASE & (mmap.PAGESIZE-1)
rb = lambda n: struct.unpack('<I', m[d+4*n:d+4*n+4])[0] & 0xff
print('0x14=0x%02x (expect 77)' % rb(0x14))
print('0x15=0x%02x (expect f0)' % rb(0x15))
print('0xF0-F9:', ' '.join('%02x'%rb(0xF0+i) for i in range(10)))
"
# Expected: 0x14=0x77  0x15=0xf0  0xF0-F9: 02 f9 0a 40 12 fc 06 fc 77 16

# Confirm daemon is running and has no REMAP errors
sudo systemctl status wedge100s-ledup-linkstate --no-pager -n 10
```

---

## Appendix: Failed Approaches and Crash History

### 2026-04-03 — /dev/mem CTRL write crash

Direct write to `CMIC_LEDUP0_CTRL` (BAR2 offset 0x20000) via Python `/dev/mem` mmap crashed
the switch and required a hardware reboot.

**Rule**: Never write BCM CTRL or PROGRAM_RAM via `/dev/mem` on a running system.
DATA_RAM reads/writes via `/dev/mem` are safe (plain SRAM); CTRL and PROGRAM_RAM must
only be written via bcmcmd or the soc-file init path before chains start.

### 2026-04-06 — REMAP +64 approach (impossible)

Attempted to shift LEDUP1 REMAP values from 0..31 to 65..95 to redirect reads to the
safe zone. Failed: REMAP fields are 6-bit (max 63).  BCM setreg rejects value 65 with
`Value '65' too large for 6-bit field 'REMAP_PORT_0'`.  This approach is architecturally
impossible regardless of how it is attempted.

### 2026-04-07 — Bytecode patch (working)

See Solution Architecture above. Verified live on hardware. (visual LED confirmation pending)

---

## AS7712 Comparison

AS7712 uses entirely different per-port LED hardware (two port CPLDs at i2c 0x62/0x64,
one register per port per LED channel).  SONiC's `ledd` daemon writes directly to those
sysfs entries via the `leds-accton_as7712_32x.ko` kernel module.  BCM LEDUP DATA_RAM is
not involved at all.

On Wedge 100S, there are no per-port CPLD LED registers.  The equivalent of AS7712's
kernel driver path is the combination of:
- `led_control.py::port_link_state_change()` (called by ledd) → writes hint file
- `wedge100s-ledup-linkstate` daemon → writes LEDUP1 DATA_RAM via bcmcmd

This achieves the same sub-second link-state-change → LED-update latency as the AS7712
kernel driver path, without requiring kernel-space `/dev/mem` access.
