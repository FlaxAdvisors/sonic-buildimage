# Wedge 100S-32X: I2C Bus Topology and QSFP Cages

This file is linked from [HARDWARE.md](HARDWARE.md) §3 and §6.
Full physical mux topology with address tables is also in [`notes/i2c_topology.json`](i2c_topology.json).

---

## §3 I2C Bus Topology

### Phase 2 Kernel-Visible Buses

In the current (Phase 2) software architecture, only two I2C buses are visible to the kernel:

| Bus | Driver | Description |
|-----|--------|-------------|
| `i2c-0` | `i2c_i801` | SMBus I801 adapter at f000 (LPC-attached) |
| `i2c-1` | `hid_cp2112` | CP2112 USB-HID bridge (USB VID:PID 10c4:ea90) |

**i2c-2 through i2c-41 do NOT exist in the running kernel.** `i2c_mux_pca954x` is intentionally not
loaded. All traffic beyond i2c-1 is handled by `wedge100s-i2c-daemon` via `/dev/hidraw0`.

### i2c-0 (SMBus I801) — Direct-Attach Devices

| Address | Device | Notes |
|---------|--------|-------|
| `0x08` | RTC / Clock | — |
| `0x44` | Voltage monitor | — |
| `0x48` | ADS1015 12-bit ADC | 4-channel, 3.3 V reference |

### i2c-1 (CP2112 USB-HID Bridge) — CPLD Only

The CP2112 is enumerated as a standard SMBus adapter by `hid_cp2112`. In Phase 2,
only the CPLD is registered as a kernel device on this bus:

| Address | Device | Driver |
|---------|--------|--------|
| `0x32` | CPLD | `wedge100s_cpld` (see [HARDWARE.md §4](HARDWARE.md#4-cpld)) |

The five PCA9548 8-channel mux ICs at addresses `0x70`–`0x74` are physically present
on this bus but are NOT instantiated as kernel devices. They are driven exclusively
by `wedge100s-i2c-daemon` through `/dev/hidraw0`.

### Physical Mux Tree (daemon-visible, not kernel-visible)

The daemon navigates this tree via raw HID reports. Bus numbers below are the logical
numbers that WOULD be assigned if `i2c_mux_pca954x` were loaded (they match the
`qsfp_port_to_bus` map in `i2c_topology.json`).

```
i2c-1 (CP2112)
├── 0x70  PCA9548 mux-A  → logical buses 2–9
│     ch0 → bus  2    (QSFP port 1 EEPROM, optoe @ 0x50)
│     ch1 → bus  3    (QSFP port 0 EEPROM, optoe @ 0x50)
│     ch2 → bus  4    (QSFP port 3 EEPROM)
│     ch3 → bus  5    (QSFP port 2 EEPROM)
│     ch4 → bus  6    (QSFP port 5 EEPROM)
│     ch5 → bus  7    (QSFP port 4 EEPROM)
│     ch6 → bus  8    (QSFP port 7 EEPROM)
│     ch7 → bus  9    (QSFP port 6 EEPROM)
├── 0x71  PCA9548 mux-B  → logical buses 10–17
│     ch0 → bus 10    (QSFP port 9 EEPROM)
│     ch1 → bus 11    (QSFP port 8 EEPROM)
│     ch2 → bus 12    (QSFP port 11 EEPROM)
│     ch3 → bus 13    (QSFP port 10 EEPROM)
│     ch4 → bus 14    (QSFP port 13 EEPROM)
│     ch5 → bus 15    (QSFP port 12 EEPROM)
│     ch6 → bus 16    (QSFP port 15 EEPROM)
│     ch7 → bus 17    (QSFP port 14 EEPROM)
├── 0x72  PCA9548 mux-C  → logical buses 18–25
│     ch0 → bus 18    (QSFP port 17 EEPROM)
│     ch1 → bus 19    (QSFP port 16 EEPROM)
│     ch2 → bus 20    (QSFP port 19 EEPROM)
│     ch3 → bus 21    (QSFP port 18 EEPROM)
│     ch4 → bus 22    (QSFP port 21 EEPROM)
│     ch5 → bus 23    (QSFP port 20 EEPROM)
│     ch6 → bus 24    (QSFP port 23 EEPROM)
│     ch7 → bus 25    (QSFP port 22 EEPROM)
├── 0x73  PCA9548 mux-D  → logical buses 26–33
│     ch0 → bus 26    (QSFP port 25 EEPROM)
│     ch1 → bus 27    (QSFP port 24 EEPROM)
│     ch2 → bus 28    (QSFP port 27 EEPROM)
│     ch3 → bus 29    (QSFP port 26 EEPROM)
│     ch4 → bus 30    (QSFP port 29 EEPROM)
│     ch5 → bus 31    (QSFP port 28 EEPROM)
│     ch6 → bus 32    (QSFP port 31 EEPROM)
│     ch7 → bus 33    (QSFP port 30 EEPROM)
└── 0x74  PCA9548 mux-E  → logical buses 34–41
      ch0 → bus 34    (QSFP port — unused)
      ch1 → bus 35    (QSFP port — unused)
      ch2 → bus 36    (PCA9535 @ 0x22, QSFP presence ports 0–15)
      ch3 → bus 37    (PCA9535 @ 0x23, QSFP presence ports 16–31)
      ch4 → bus 38    (unused)
      ch5 → bus 39    (unused)
      ch6 → bus 40    (24c64 @ 0x50, system EEPROM)
      ch7 → bus 41    (unused)
```

**Bus number assignment note:** When `i2c_mux_pca954x` is loaded (e.g., in a future
phase or for debugging), muxes must be registered in address order `0x70` → `0x71` →
`0x72` → `0x73` → `0x74` to preserve these bus numbers. Out-of-order instantiation
produces different bus-number assignments.

### Why the Mux Tree Is Not Kernel-Managed

Loading `i2c_mux_pca954x` caused QSFP EEPROM corruption during early bring-up:
the kernel's mux-select writes (i2c write to 0x70–0x74) interleaved with daemon
HID transactions, corrupting the daemon's channel selection and producing garbage
EEPROM reads. See `BEWARE_EEPROM.md §2–4` for the full incident record.

---

## §6 QSFP Cages

### Overview

32 QSFP28 cages (100G). All presence detection and EEPROM access in Phase 2 is via
`wedge100s-i2c-daemon` writing to `/run/wedge100s/`. The kernel does not register
optoe or PCA9535 GPIO devices.

RESET and LP_MODE pins are on the mux board and are not accessible from the host CPU.

### Presence Detection

Two PCA9535 16-bit I/O expanders provide insertion status:

| Logical bus | I2C address | Ports covered | Mux path |
|-------------|-------------|---------------|----------|
| 36 | `0x22` | 0–15 | i2c-1 → mux 0x74 ch2 |
| 37 | `0x23` | 16–31 | i2c-1 → mux 0x74 ch3 |

Each PCA9535 has two 8-bit input registers (offset 0x00 = ports 0–7, offset 0x01 = ports 8–15
within the group). Bits are active-low: `0` = module present, `1` = absent.

**XOR-1 interleave:** The bit position for port `N` within its group of 16 is
`(N % 16) ^ 1`. This even/odd swap is required to match physical cage order;
it is documented in ONL `sfpi.c` as `onlp_sfpi_reg_val_to_port_sequence()` and
verified on hardware. See `sfp.py` `get_presence()` for the Python implementation.

Example: port 0 → line `(0 % 16) ^ 1 = 1`, register 0x00 of 0x22, bit 1.

### EEPROM Access

Each QSFP cage's EEPROM (optoe1-compatible, address `0x50`) is reached by:
1. Selecting the appropriate PCA9548 channel on mux-A through mux-D (0x70–0x73)
2. Reading from address `0x50`

DOM data (page 1+) is at address `0x51` in optoe convention.

The daemon caches page 0 (256 bytes) on insertion to `/run/wedge100s/sfp_N_eeprom`.
`sfp.py` reads this cache; fallback to sysfs path (only valid if `i2c_mux_pca954x`
is loaded) is present for the first ~5 s of boot before the daemon's initial scan.

### Port-to-Bus Map (0-indexed)

Source: `sfp_bus_index[]` in `sonic_platform/sfp.py` (verified on hardware 2026-03-11).

| Port | Bus | Port | Bus | Port | Bus | Port | Bus |
|------|-----|------|-----|------|-----|------|-----|
| 0 | 3 | 8 | 11 | 16 | 19 | 24 | 27 |
| 1 | 2 | 9 | 10 | 17 | 18 | 25 | 26 |
| 2 | 5 | 10 | 13 | 18 | 21 | 26 | 29 |
| 3 | 4 | 11 | 12 | 19 | 20 | 27 | 28 |
| 4 | 7 | 12 | 15 | 20 | 23 | 28 | 31 |
| 5 | 6 | 13 | 14 | 21 | 22 | 29 | 30 |
| 6 | 9 | 14 | 17 | 22 | 25 | 30 | 33 |
| 7 | 8 | 15 | 16 | 23 | 24 | 31 | 32 |

Pattern: pairs of front-panel ports are interleaved (port N → bus N+3 if N even,
bus N+1 if N odd, within each group of 8). This matches the PCA9548 channel wiring
on the mux board.

### Address Caution

`0x50` appears at three distinct locations on this bus. They are not interchangeable:

| Path | Address | What it is |
|------|---------|------------|
| `i2c-1/0x50` | 0x50 | EC chip — ACKs I2C writes but silently discards them |
| `i2c-1/0x51` | 0x51 | COME module internal EEPROM — NOT the system EEPROM |
| `i2c-40/0x50` (via mux 0x74 ch6) | 0x50 | True system EEPROM (24c64, 8 KiB, ONIE TLV) |

Writing TlvInfo to `i2c-1/0x51` was a bug in early development. Full account
in `BEWARE_EEPROM.md`.
