# LED Diagnostic Tooling & Daemon Fix Design
**Date:** 2026-04-02
**Branch:** wedge100s
**Status:** Approved for implementation planning
**Supersedes:** `docs/Opus-LEDs-Notes.md` (incorporated in full below)

---

## Problem Statement

Three issues block shipping the Wedge 100S front panel port LEDs:

1. **No diagnostic tool exists** to set LEDs to known states, verify register values, or empirically discover the color mapping. Every investigation so far has relied on ad-hoc bcmcmd calls and on-site human observers. We need a self-contained tool that can set state AND confirm the state took effect, without a person at the front panel.

2. **The bcmcmd/dsserve diag shell is broken** — the syncd container's diag socket never accepts connections. This means `ledinit` (which loads LED bytecode via bcmcmd) silently fails on every syncd start. Result: LEDUP CTRL=0x00, PROGRAM_RAM=all zeros, DATA_RAM=all zeros. No scan chain output reaches the front panel.

3. **The physical LED color mapping is unknown** — we don't know which LEDUP processor drives which color, whether wiring is active-high or active-low, or what the triangle LED geometry means. The only confirmed observation is: both LEDUP channels active simultaneously = magenta.

---

## Hardware Architecture

### Three Control Layers

```
┌──────────────────────────────────────────────────────────┐
│  BMC SYSCPLD (i2c-12, addr 0x31)                         │
│  Register 0x3c: gates BCM LEDUP output to front panel    │
│  th_led_en=1 → passthrough enabled                       │
│  th_led_en=0 → BCM output blocked                        │
│  test modes: walk_test, led_test_mode, blink              │
├──────────────────────────────────────────────────────────┤
│  BCM56960 Tomahawk LEDUP (3 processors: 0, 1, 2)        │
│  LEDUP0 + LEDUP1: each drives one color channel          │
│  DATA_RAM[0..31]: per-port status (HW auto-populated)    │
│  PROGRAM_RAM: LED bytecode (AS7712-identical)             │
│  led auto on/off: enables/disables HW status updates      │
├──────────────────────────────────────────────────────────┤
│  Physical LEDs: QSFP28 cage triangle pairs               │
│  LEDUP0 → color A (green per SUBSYSTEMS_LED.md)          │
│  LEDUP1 → color B (amber per SUBSYSTEMS_LED.md)          │
│  Both active → magenta (observed)                         │
└──────────────────────────────────────────────────────────┘
```

### Two Independent LED Subsystems

| Subsystem | LEDs | Controller | Colors |
|-----------|------|-----------|--------|
| **System LEDs** | SYS1, SYS2 | Host CPLD `i2c-1/0x32`, registers 0x3e/0x3f | off, red, green, blue + blinking variants |
| **Port LEDs** | 32 QSFP front panel | BCM LEDUP scan chain → CPLD passthrough | Unknown — at least 3+ (rainbow proves multi-color) |

System LED register encoding (from ONL `ledi.c`, verified in CPLD driver):
```
0x00 = off           0x08 = off (blinking)
0x01 = red           0x09 = red blinking
0x02 = green         0x0a = green blinking
0x04 = blue          0x0c = blue blinking
```

### BMC SYSCPLD Register 0x3c — Port LED Control

| Bit | Sysfs attr | Power-on | Current | Function |
|-----|-----------|----------|---------|----------|
| 7 | `led_test_mode_en` | 1 | 0 | CPLD test pattern overlay |
| 6 | `led_test_blink_en` | 1 | 0 | Blink during test |
| 5:4 | `th_led_steam` | 2 | 0 | Test stream select |
| 3 | `walk_test_en` | 0 | 0 | Walking LED test |
| 2 | (unknown) | 0 | 0 | — |
| 1 | `th_led_en` | 0 | 1 | BCM LEDUP passthrough |
| 0 | `th_led_clr` | 0 | 0 | Clear scan chain |

- Power-on default: `0xe0` (rainbow test mode, LEDUP gated off)
- Required SONiC value: `0x02` (passthrough enabled, all tests off)
- Register 0x3d = test color selector (only active when th_led_steam ≠ 0)

### BCM LEDUP DATA_RAM — Per-Port Status Byte

| Bit | Field | Description |
|-----|-------|-------------|
| 7 | **Link Up** | 1 = link detected |
| 6 | Flow Control | 1 = flow control active |
| 5 | Duplex | 1 = full duplex |
| 4:3 | Speed | 00=10M, 01=100M, 10=1G, 11=10G+ |
| 2 | Collision | 1 = collision detected |
| 1 | TX activity | 1 = transmitting |
| 0 | RX activity | 1 = receiving |

### BCM LEDUP Register Map (PCIe BAR2 offsets)

BCM56960 at PCI `0000:06:00.0`, BAR2 = auto-discovered, size 8MB.

| Register | Offset | Width | Purpose |
|----------|--------|-------|---------|
| `CMIC_LEDUP0_CTRL` | 0x34000 | 32-bit | Enable, scan timing |
| `CMIC_LEDUP0_STATUS` | 0x34004 | 32-bit | Running, PC |
| `CMIC_LEDUP0_PROGRAM_RAM(n)` | 0x34100 + 4*n | 32-bit | LED bytecode (256 words) |
| `CMIC_LEDUP0_DATA_RAM(n)` | 0x34800 + 4*n | 32-bit | Per-port status (256 entries) |
| `CMIC_LEDUP1_CTRL` | 0x34400 | 32-bit | Same layout, processor 1 |
| `CMIC_LEDUP1_PROGRAM_RAM(n)` | 0x34500 + 4*n | 32-bit | Processor 1 bytecode |
| `CMIC_LEDUP1_DATA_RAM(n)` | 0x34C00 + 4*n | 32-bit | Processor 1 per-port status |

**Note:** PROGRAM_RAM offset (0x34100) needs confirmation — during this session all reads returned zero because ledinit never loaded bytecode. The `set passthrough` command will write bytecode and confirm by read-back.

### Port Order Remap

The BCM LED port index does not match the front-panel port order. The `led_proc_init.soc` file contains the full remap table. Key mapping (front panel position → LED port index):

```
FP1/Ethernet0→29   FP2/Ethernet4→28   FP3/Ethernet8→31   FP4/Ethernet12→30
FP5/Ethernet16→1   FP6/Ethernet20→0   FP7/Ethernet24→3   FP8/Ethernet28→2
...
FP31/Ethernet120→27  FP32/Ethernet124→26
```

---

## Root Cause Analysis (from 2026-04-02 investigation)

### Original Magenta LEDs

**Chain of causation:**
1. `config_db.json` had no PORT table (dropped by `gen-l3-config.py` on 2026-03-30)
2. SONiC configured zero ports → BCM SDK had all ports `!ena` (admin-down)
3. BCM SERDES still populated LEDUP DATA_RAM with link=1 for `!ena` ports
4. LED bytecode saw link=1 → output both LEDUP0 and LEDUP1 active
5. Both color channels lit simultaneously → **magenta**

**Resolution:** PORT table restored. `tools/deploy.py` applied breakouts, port-channels, FEC. 12 ports now have operational link.

### Current State (post PORT table fix)

After restoring ports and restarting syncd:
- LEDUP0 CTRL = 0x00000000 (disabled)
- LEDUP1 CTRL = 0x00000000 (disabled)
- All PROGRAM_RAM = zeros (no bytecode loaded)
- All DATA_RAM = zeros
- CPLD 0x3c = 0x02 (passthrough mode, but nothing to pass through)
- **ledinit failed silently** because bcmcmd cannot communicate with syncd

### DATA_RAM Values Observed (before PORT table fix)

| Port type | Entries | Value | Meaning |
|-----------|---------|-------|---------|
| ce (100G) | 0,8,16,24 | `0xb8` | Link + FD + Speed=11 |
| xe (even) | 2,4,6,10... | `0xf8` | Link + FC + FD + Speed=11 |
| xe (odd) | 1,3,5,7... | `0x80` | Link only |

All 32 entries had bit 7 (Link Status) = 1. Values could not be cleared via `setreg` — hardware auto-populated them and they snapped back immediately.

---

## Design: LED Diagnostic Tool

### Overview

**`utils/wedge100s-led-diag.py`** — a single Python script that runs on the SONiC target as root. All ASIC register access via PCIe BAR2 `/dev/mem` (no bcmcmd dependency). All CPLD access via BMC SSH.

### CLI Interface

```
wedge100s-led-diag.py status                # dump CPLD 0x3c + LEDUP CTRL + DATA_RAM summary
wedge100s-led-diag.py set rainbow           # CPLD test mode on (0xe0)
wedge100s-led-diag.py set passthrough       # CPLD 0x02, reload LED bytecode, led auto on
wedge100s-led-diag.py set all-off           # led auto off, zero DATA_RAM, LEDUP disabled
wedge100s-led-diag.py set color <color>     # led auto off, software-drive all ports to <color>
wedge100s-led-diag.py set port <n> <color>  # software-drive single port
wedge100s-led-diag.py probe                 # cycle through color combos, record results
```

Every `set` command does a read-back verify and prints PASS/FAIL per register written.

### Register Access Layer

**PCIe BAR2 access** (proven working this session):
- Auto-discovered at runtime by scanning `/sys/bus/pci/devices/` for `14e4:b960`
- Single `mmap()` of BAR2, held open for lifetime of command
- Read and write via `struct.pack/unpack` at known offsets
- Only touches LED registers (LEDUP area 0x34000–0x35FFF) — no risk to forwarding state

**CPLD access** — two paths depending on daemon state:
1. **Daemons running:** Write `/run/wedge100s/cpld_led_ctrl.set` with desired values; daemon dispatches to BMC SSH. Read via `ssh root@<bmc> cat sysfs`.
2. **Daemons stopped:** Direct `ssh root@<bmc>` for both read and write.

Tool auto-detects by checking `systemctl is-active wedge100s-bmc-daemon`.

### LED Color Model

**Port LED color sources — two modes:**

1. **CPLD test mode** (reg 0x3c bit 7 = 1): CPLD ignores BCM scan chain, drives its own rainbow pattern. Register 0x3d selects color/pattern. `th_led_steam` selects stream. This produces the full rainbow — CPLD-generated, not BCM-generated.

2. **BCM passthrough** (reg 0x3c = 0x02): CPLD passes through BCM LEDUP scan chain. Two processors (LEDUP0, LEDUP1) each shift out 1 bit per port per scan cycle. 4 possible combinations per port.

**The 4 BCM-controllable states per port LED:**

| LEDUP0 | LEDUP1 | Physical color |
|--------|--------|---------------|
| 0 | 0 | TBD (probe will discover) |
| 1 | 0 | TBD |
| 0 | 1 | TBD |
| 1 | 1 | magenta (confirmed) |

If wiring is active-low, the mapping inverts. The `probe` command resolves this empirically.

### Probe Sequence

**Phase 1 — CPLD test mode colors** (no BCM involvement):
1. Enable `led_test_mode_en=1`
2. Cycle `th_led_steam` and register 0x3d values
3. Maps the CPLD's built-in color palette
4. Answers: what colors can the hardware physically produce?

**Phase 2 — BCM scan chain combinations** (passthrough mode):
1. Set `0x3c = 0x02` (passthrough), disable LEDUP auto and processors
2. For each of the 4 combinations (LEDUP0=0/1, LEDUP1=0/1):
   - Write DATA_RAM[0..31] on the appropriate processor(s)
   - Enable LEDUP processors so scan chain drives physical LEDs
   - Record: "LEDUP0=X LEDUP1=Y → [observed color]"
3. Maps the 4 BCM-controllable states to physical colors

**Phase 3 — Per-port walk**:
1. Light up one port at a time, walk across all 32 positions
2. Confirms PORT_ORDER_REMAP table is correct for this PCB

Results saved to `/run/wedge100s/led_probe_results.json`.

---

## Design: dsserve/ledinit Investigation and Fix

### Problem Chain

1. `syncd` starts inside its container with `dsserve` as wrapper
2. `dsserve` creates `/var/run/sswsyncd/sswsyncd.socket`, forks syncd, enters `do_wait`
3. syncd inherits the socket fd but **never accepts connections on it**
4. `bcmcmd` connects → EAGAIN or timeout — every time
5. `ledinit` runs `led_proc_init.soc` via bcmcmd — **silently fails** (exits 0 after ~5s)
6. LEDUP CTRL=0x00, PROGRAM_RAM=all zeros, DATA_RAM=all zeros
7. No scan chain output → port LEDs driven solely by CPLD default behavior

### Evidence Collected

- `ss -lxp`: socket owned by dsserve (pid 27), NOT syncd (pid 49)
- syncd thread list: 43 threads, none in `accept()` or polling diag socket
- dsserve stuck in `do_wait` — only waits for syncd to exit
- `bcmcmd -t 10 "ps"` → timeout after clean restart with 60s warm-up
- ledinit exits code 0 after 5 seconds — false success
- `show interfaces status` works fine — syncd is functional for forwarding, just not diag shell

### Investigation Plan

1. **Check ledinit script** inside syncd container — what does it run? If it uses bcmcmd with a short timeout, that explains the silent failure.

2. **Check syncd build flags** — the `--diag` flag should enable the BCM diag shell thread. Either: (a) not compiled into this SAI build, (b) diag thread crashes on start, or (c) configuration issue in `/etc/sai.d/sai.profile`.

3. **Search for known SONiC issues** — dsserve socket hang may be a regression in the trixie/hare-lorax build.

### Fix Strategy

**If bcmcmd is fixable:** Fix the root cause (build flag, config, or SONiC patch). ledinit works again, standard LED pipeline restored.

**If not easily fixable:** Modify ledinit (or create a replacement) that loads LED bytecode via `/dev/mem` BAR2 writes as fallback. Parse `led_proc_init.soc` for bytecode and register values, write PROGRAM_RAM + PORT_ORDER_REMAP + CTRL directly. Shares the register access library with the diagnostic tool.

Either way, `wedge100s-led-diag.py status` provides independent verification.

---

## File Layout

### New Files

| File | Location | Purpose |
|------|----------|---------|
| `wedge100s-led-diag.py` | `utils/` → `/usr/bin/` on target | Main diagnostic/control tool |
| `wedge100s_ledup.py` | `utils/` | Shared library: BAR2 mmap, register R/W, bytecode parser |
| `led_probe_results.json` | `/run/wedge100s/` (runtime) | Persisted color map from probe |

### Modified Files

| File | Change |
|------|--------|
| `wedge100s-bmc-daemon.c` | Add `cpld_led_read.set` dispatch entry for CPLD register reads |

### Removed Files

| File | Reason |
|------|--------|
| `docs/Opus-LEDs-Notes.md` | Fully incorporated into this spec |

### Unchanged

- `led_proc_init.soc` — bytecode is correct, just never got loaded
- `led_control.py` / `chassis.py` — system LED path works fine
- `wedge100s-i2c-daemon.c` — no port LED involvement

### Dependencies

Zero external dependencies. Python 3 stdlib only (`mmap`, `struct`, `os`, `argparse`, `json`, `subprocess`). BMC access via SSH (auto-detects daemon vs direct path).

---

## Implemented During 2026-04-02 Session (historical record)

### BMC Daemon Write-Request Dispatch

Implemented the `.set` file dispatch mechanism in `wedge100s-bmc-daemon.c`:
- Host script writes `/run/wedge100s/<name>.set`
- Daemon detects via inotify, maps to BMC command, executes via SSH, removes file
- First entry: `clear_led_diag.set` → `/usr/local/bin/clear_led_diag.sh`
- Deployed and verified (journalctl confirmed dispatch)

### Updated `clear_led_diag.sh` (BMC-side)

Added `echo 0 > ${SYSCPLD_SYSFS_DIR}/th_led_steam` — was missing before, caused `th_led_steam=2` ("all LED check" mode) to persist.

### PORT Table Restoration

- `config_db.json` PORT table restored (was dropped by `gen-l3-config.py`)
- `tools/deploy.py` applied 3 breakouts (Ethernet0→4x25G, Ethernet64→4x10G, Ethernet80→4x25G)
- PortChannel1 (Ethernet16+32) created, VLANs 10/999 configured
- 12 ports operational with link up
- DPB was blocked by BGP_GLOBALS/BGP_NEIGHBOR/MGMT_INTERFACE yang validation failures — fixed by removing L3 tables from config before breakout

### `/dev/mem` BAR2 Register Access (proven)

Created `utils/read_ledup_mmap.py` — confirmed PCIe BAR2 at 0xfb000000 reads LEDUP registers correctly. All DATA_RAM = 0x00 after syncd restart (because ledinit failed). LEDUP CTRL = 0x00 (processors disabled).

---

## Open Questions

These will be answered by the `probe` command:

1. Which color does LEDUP0 drive vs LEDUP1? (green/amber per docs, unverified)
2. Active-high vs active-low LED wiring?
3. Triangle LED geometry: which triangle = which port, which = link vs traffic?
4. Whether the AS7712 bytecode is correct for Wedge 100S PCB wiring
5. What colors does the CPLD test mode cycle through?

## Reference: Inventec Software LED Control Pattern

`platform/broadcom/sonic-platform-modules-inventec/common/utils/led_proc.py` demonstrates:
- `led auto off` to stop hardware DATA_RAM updates
- Software writes to `CMIC_LEDUP_DATA_RAM[]` via bcmshell
- Per-platform port-to-LED mapping
- This is the escape hatch when hardware auto-population gives wrong results
