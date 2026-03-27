# QSFP / SFP Subsystem

## Hardware

- 32 × QSFP28 (100G) ports, labeled `Ethernet0`–`Ethernet124` (step 4) in SONiC.
- Port index is 1-based in `port_config.ini` (`index` column 1–32); 0-based internally
  in `Sfp.__init__()`.
- All ports run RS-FEC. Lane assignments are non-sequential and interleaved
  (Tomahawk BCM56960 pipe mapping — see `port_config.ini`).

**QSFP EEPROM buses** (from ONL `sfpi.c` `sfp_bus_index[]`, 0-based port → bus):

```
port  0→bus  3,  1→2,   2→5,   3→4,   4→7,   5→6,   6→9,   7→8
port  8→bus 11,  9→10, 10→13, 11→12, 12→15, 13→14, 14→17, 15→16
port 16→bus 19, 17→18, 18→21, 19→20, 20→23, 21→22, 22→25, 23→24
port 24→bus 27, 25→26, 26→29, 27→28, 28→31, 29→30, 30→33, 31→32
```

Physical path (daemon): host CP2112 (`/dev/hidraw0`) → one of four PCA9548 muxes
(`0x70`–`0x73`) on bus 1 → per-port channel → optoe1-compatible device at `0x50`.

**QSFP presence hardware:**
- Ports 0–15: PCA9535 at bus `36` / addr `0x22` (INPUT0 = ports 0–7, INPUT1 = ports 8–15)
- Ports 16–31: PCA9535 at bus `37` / addr `0x23` (INPUT0 = ports 16–23, INPUT1 = ports 24–31)
- Physical path: CP2112 (bus 1) → PCA9548 mux `0x74` → channel 2 (bus 36) or channel 3 (bus 37)
- Active-low: bit = 0 means module present
- XOR-1 interleave: within each 16-port group, port `p` maps to line `(p % 16) ^ 1`

## Driver / Daemon

**Phase 2 production (current):**

- `i2c_mux_pca954x`, `at24`, and `optoe` are **not loaded** as kernel modules.
  Bus numbers `i2c-2` through `i2c-41` do not exist.
- `wedge100s-i2c-daemon` owns all mux-tree I2C via `/dev/hidraw0` (CP2112 HID).
  - Polls PCA9535 presence registers every 3 s; writes
    `/run/wedge100s/sfp_N_present` ("0" or "1") for each port N (0-based).
  - On insertion event: reads 256 bytes of QSFP page 0 and writes
    `/run/wedge100s/sfp_N_eeprom`.
- `xcvrd` (inside `pmon` container) consumes presence and EEPROM data via Python API.

**Kernel module (direct sysfs fallback, not normally used):**
- `optoe1` driver at `/sys/bus/i2c/devices/i2c-{bus}/{bus}-0050/eeprom`
- Only reachable if mux tree is manually instantiated

## Python API

- **Class:** `Sfp` in `sonic_platform/sfp.py`
- **Inherits:** `SfpOptoeBase`
- **Instantiated by:** `Chassis.__init__()` as `Sfp(i)` for `i` in 0–31; stored at
  `_sfp_list[1]`–`_sfp_list[32]` (index 0 is a `None` sentinel to align with
  1-based `port_config.ini` index column).

| Method | Returns | Primary file(s) |
|---|---|---|
| `get_presence()` | `bool` | `/run/wedge100s/sfp_N_present` (mtime checked; stale > 8 s → smbus2 fallback) |
| `read_eeprom(offset, num_bytes)` | `bytearray` or `None` | `/run/wedge100s/sfp_N_eeprom` (fallback: sysfs under `_eeprom_bus_lock`) |
| `get_eeprom_path()` | `str` path | returns daemon cache path if it exists, else sysfs path |
| `get_name()` | `str` | e.g. `'QSFP28 1'` |
| `get_reset_status()` | `False` (always) | not wired to host CPU |
| `get_lpmode()` | `False` (always) | not wired to host CPU |
| `reset()` | `False` | not supported |
| `set_lpmode(lpmode)` | `False` | not supported |
| `get_error_description()` | `SFP_STATUS_OK` or `SFP_STATUS_UNPLUGGED` | calls `get_presence()` |

**Presence fallback logic** (smbus2, triggered when daemon cache is stale > 8 s):
```python
group = port // 16
line  = (port % 16) ^ 1      # XOR-1 interleave from ONL sfpi.c
reg   = line // 8
bit   = line % 8
byte  = platform_smbus.read_byte(_PCA9535_BUS[group], _PCA9535_ADDR[group], reg)
present = not bool((byte >> bit) & 1)   # active-low
```

**Bulk presence** is used by `Chassis.get_change_event()`:
- Primary: reads all 32 `/run/wedge100s/sfp_N_present` files
- Fallback: 4 × `platform_smbus.read_byte()` of PCA9535 INPUT registers
- Change event loop sleeps 3 s between polls (daemon cadence)

## Test Bench Module Inventory (verified 2026-03-27)

14 of 32 ports populated.  Passive copper DAC cables have no DOM (byte 220 = 0x00,
`temp_support=False`, `temp=N/A` is correct behaviour — not a bug).

| SONiC Port | 0-based | Vendor | Part Number | Type | DOM |
|---|---|---|---|---|---|
| Ethernet0   |  0 | Mellanox       | MCP7F00-A002R    | Passive DAC 2m  | None |
| Ethernet8   |  2 | Mellanox       | MCP1600-C01A     | Passive DAC     | None |
| Ethernet12  |  3 | FS             | Q28-PC03         | Passive DAC 3m  | None |
| Ethernet16  |  4 | FS             | Q28-PC02         | Passive DAC 2m  | None |
| Ethernet32  |  8 | FS             | Q28-PC02         | Passive DAC 2m  | None |
| Ethernet48  | 12 | FS             | Q28-PC02         | Passive DAC 2m  | None |
| Ethernet64  | 16 | Mellanox       | MCP7904-X002A    | Passive DAC     | None |
| Ethernet76  | 19 | AOI            | AQPLBCQ4EDMA1105 | Active optical  | byte220=0x0c ✓ |
| Ethernet80  | 20 | Amphenol       | NDAQGF-F305      | Passive DAC     | None |
| Ethernet84  | 21 | Arista Networks| QSFP28-SR4-100G  | Optical SR4     | byte220=0x0c ✓ |
| Ethernet100 | 25 | Arista Networks| QSFP28-SR4-100G  | Optical SR4     | byte220=0x0c ✓ |
| Ethernet104 | 26 | Arista Networks| QSFP28-LR4-100G  | Optical LR4     | byte220=0x0c ✓ |
| Ethernet108 | 27 | Arista Networks| QSFP28-SR4-100G  | Optical SR4     | byte220=0x0c ✓ |
| Ethernet112 | 28 | FS             | Q28-PC01         | Passive DAC 1m  | None |

Arista SR4-100G: byte 220 = 0x0c (bits 3+2 = bias/power monitoring; bits 5+4 = 0 →
voltage `N/A` is correct per module spec; temperature reported via SFF-8636 pre-Rev-2.8
path).

## Pass Criteria

- For each present port N (0-based): `/run/wedge100s/sfp_N_present` contains `"1"`
- For each present port N: `/run/wedge100s/sfp_N_eeprom` is exactly 256 bytes
- `Sfp(N).get_presence()` returns `True` for ports with modules inserted
- `Sfp(N).read_eeprom(0, 1)` returns `bytearray([0x11])` for QSFP28 (identifier byte)
- `show interfaces transceiver presence` shows `Present` for populated ports
- `xcvrd` logs no `read_eeprom` errors in `/var/log/syslog`

## Known Gaps

- `get_reset_status()` and `get_lpmode()` always return `False`; LP_MODE and RESET
  pins are on the mux board, not host-CPU-accessible.
- `reset()` and `set_lpmode()` always return `False`; no write path to QSFP control
  pins from host CPU.
- EEPROM cache (`sfp_N_eeprom`) is written by the daemon only on insertion events.
  If a module is inserted before the daemon first runs, the file is absent until
  the next insertion event; the fallback sysfs path will fail in Phase 2 (no optoe driver).
- Only page 0 (256 bytes) is cached by the daemon. Pages 1–3 (DOM data beyond byte 128)
  are not accessible without the optoe kernel driver.
- `get_model()` and `get_serial()` are not overridden; they are resolved from EEPROM
  data by the parent class (SfpOptoeBase) via `read_eeprom()`.
- No interrupt-driven presence notification; presence is polled every 3 s.
