# Daemon Restart Stale-File Fix & DOM Investigation (2026-03-27)

## Summary

Two issues investigated: (1) stale `sfp_N_lpmode` files causing modules to stay in
LP_MODE after daemon restart; (2) apparent DOM temperature failures on some ports that
turned out to be correct behavior (DAC cables, not optical modules).

---

## Issue 1 — Stale lpmode files on daemon restart

### Root cause

`poll_lpmode_hidraw()` skips the initial LP_MODE deassert for any port whose
`/run/wedge100s/sfp_N_lpmode` state file already exists (written by a prior daemon run):

```c
/* Skip ports already initialized (state file exists) */
struct stat st;
if (stat(state_path, &st) == 0) continue;
```

On daemon restart, stale `sfp_N_lpmode` files left from the previous run caused
all 32 ports to skip deassert. Modules remained in LP_MODE → DOM registers (bias,
Tx power) read as 0 → `show interfaces transceiver pm` showed 0 bias and -inf power.

Additionally `poll_presence_hidraw()` skips the EEPROM refresh if the cache file
exists with a valid identifier byte — so stale eeprom data (from the LP_MODE era)
was served until the 20-second DOM TTL fired.

### Fix (verified on hardware 2026-03-27)

`daemon_init()` now deletes all `sfp_N_lpmode` files before entering the main loop.
EEPROM cache files are intentionally **kept** (modules need up to 2 s after LP_MODE
exit before their MCU is ready for EEPROM reads; the cached data is valid in the
meantime, and the 20 s DOM TTL fires a fresh lower-page read once the module is ready).

LP_MODE is deasserted for all 32 ports in `daemon_init()` immediately after the
lpmode files are removed:

```c
/* Remove stale lpmode state files so that poll_lpmode_hidraw()
 * re-deasserts LP_MODE for all ports on the first tick.
 * EEPROM cache files are intentionally kept: modules need up to
 * 2 s after LP_MODE exit before their MCU is ready for reads;
 * poll_presence_hidraw() serves the cached data meanwhile, and
 * the 20 s DOM TTL timer triggers a fresh lower-page read once
 * the module is fully initialised. */
for (int p = 0; p < NUM_PORTS; p++) {
    snprintf(path, sizeof(path), RUN_DIR "/sfp_%d_lpmode", p);
    unlink(path);
}
/* Deassert LP_MODE for all ports ... */
for (int p = 0; p < NUM_PORTS; p++) {
    set_lpmode_hidraw(p, 0);
}
```

### ⚠ What NOT to do

Deleting `sfp_N_eeprom` files while the daemon is running triggers immediate
upper-page reads on the next tick. Modules only become fully ready 2–5 s after
LP_MODE deassert; reads fired too early return zeros. Byte 220 (DIAG_MON_TYPE,
vendor/PN strings) is in the upper page and is **not** refreshed by the DOM TTL
mechanism — zeros written here will persist until the module is physically
re-plugged or the daemon restarts with a complete initialization.

**Always stop the daemon before manipulating EEPROM cache files.** See BEWARE_EEPROM.md.

---

## Issue 2 — Temperature N/A for some ports

### Investigation

After a daemon restart, several ports showed `temp_support=False, temp=N/A`. These
ports all had byte 220 = 0x00 and bytes 22-23 = 0x0000. Initially suspected a
DIAG_MON_TYPE quirk on the Arista SR4-100G modules.

### Root cause — these are DAC cables, not optical modules

Port enumeration (14 ports populated in the test bench at time of investigation):

| SONiC Port | Vendor | Part Number | Type | DOM |
|---|---|---|---|---|
| Ethernet0  | Mellanox | MCP7F00-A002R  | Passive copper DAC 2m | None |
| Ethernet8  | Mellanox | MCP1600-C01A   | Passive copper DAC     | None |
| Ethernet12 | FS       | Q28-PC03       | Passive copper DAC 3m  | None |
| Ethernet16 | FS       | Q28-PC02       | Passive copper DAC 2m  | None |
| Ethernet32 | FS       | Q28-PC02       | Passive copper DAC 2m  | None |
| Ethernet48 | FS       | Q28-PC02       | Passive copper DAC 2m  | None |
| Ethernet64 | Mellanox | MCP7904-X002A  | Passive copper DAC     | None |
| Ethernet76 | AOI      | AQPLBCQ4EDMA1105 | Active optical       | byte 220=0x0c ✓ |
| Ethernet80 | Amphenol | NDAQGF-F305    | Passive copper DAC     | None |
| Ethernet84 | Arista Networks | QSFP28-SR4-100G | Optical SR4    | byte 220=0x0c ✓ |
| Ethernet100 | Arista Networks | QSFP28-SR4-100G | Optical SR4   | byte 220=0x0c ✓ |
| Ethernet104 | Arista Networks | QSFP28-LR4-100G | Optical LR4   | byte 220=0x0c ✓ |
| Ethernet108 | Arista Networks | QSFP28-SR4-100G | Optical SR4   | byte 220=0x0c ✓ |
| Ethernet112 | FS      | Q28-PC01       | Passive copper DAC 1m  | None |

**Passive DAC cables** have byte 220 = 0x00 (no DOM capability declared) and no
temperature data in bytes 22-23. `temp_support=False` and `temp=N/A` is correct.

### Optical module DOM status (verified 2026-03-27)

All 5 optical modules show DOM correctly after the daemon restart fix:

| Port | Temp | Voltage | Bias | Rx Power |
|---|---|---|---|---|
| Ethernet76  | ~36 °C | 3.2 V | 32–35 mA | -inf (no peer) |
| Ethernet84  | ~28 °C | N/A (byte 220 correct) | 6.5 mA | +1 dBm |
| Ethernet100 | ~28 °C | N/A | 6.5 mA | -inf |
| Ethernet104 | ~33 °C | 3.3 V | 43–47 mA | -inf (no peer) |
| Ethernet108 | ~30 °C | N/A | 6.5 mA | +0.2 dBm |

Arista SR4-100G modules have byte 220 = 0x0c (bits 3+2 set: bias+power monitoring,
voltage not declared). `Voltage: N/A` for these is correct per the module spec.

---

## sfp.py patch added

A `get_xcvr_api()` override was added to `sonic_platform/sfp.py` that monkey-patches
`get_temperature_support = lambda: True` on `Sff8636Api` instances where
`get_temperature_support()` returns False but bytes 22-23 are non-zero. This handles
Rev 2.8+ modules that have valid temperature data despite DIAG_MON_TYPE bit 5 = 0.

The patch is dormant on the current test bench (all "no temp" ports are DAC cables
with genuine byte 22-23 = 0x0000) but activates correctly if such a module is inserted.

---

## CLAUDE.md update

The I2C bus safety rule was elevated to a top-level `## Workflow Rules` block with
explicit mention of `sfp_*_eeprom` file manipulation risk and the recovery cost
(physical EEPROM reprogram). Previous wording was a paragraph buried in prose.
