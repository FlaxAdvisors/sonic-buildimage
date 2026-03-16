# PF-01 — I2C Topology: Implementation

## What Was Built

### Primary artifact

`notes/i2c_topology.json` — machine-readable topology reference (generated 2026-02-25,
updated 2026-03-11).

`notes/HARDWARE.md` — human-readable hardware reference derived from the JSON.

### _NOTICE header (Phase 2 architecture caveat)

The JSON carries a `_NOTICE` block that documents the intentional divergence between
the physical topology it describes and what the running kernel sees:

```
"This file is reference documentation only. It is not loaded at runtime."
"The CURRENT (Phase 2) kernel module list is: i2c_dev, i2c_i801, hid_cp2112, wedge100s_cpld."
"i2c_mux_pca954x, at24, and optoe are intentionally NOT loaded."
"Bus numbers i2c-2 through i2c-41 do NOT exist in the running system."
"All QSFP EEPROM and system EEPROM access is via /dev/hidraw0 (wedge100s-i2c-daemon)."
```

This is critical context: the file describes physical topology that the
`wedge100s-i2c-daemon` navigates via HID reports — not kernel-visible bus numbers.

### Key topology facts captured

**Root buses**
- `i2c-0`: `i2c_i801` SMBus (devices: RTC 0x08, voltage monitor 0x44, ADC 0x48)
- `i2c-1`: `hid_cp2112` CP2112 USB-HID bridge (USB VID:PID 10c4:ea90)

**CPLD** — i2c-1 / 0x32
- Register 0x00: version major (hardware value: `0x02`)
- Register 0x01: version minor (hardware value: `0x06`)
- Register 0x02: board ID (`0x65`)
- Register 0x10: PSU presence/pgood (hardware value verified live: `0xe0`)
- Register 0x3e: SYS LED 1 (0=off, 1=red, 2=green, 4=blue, +8=blink)
- Register 0x3f: SYS LED 2 (same encoding)

**PSU register bit polarity** (active-low present, active-high pgood):
- bit 0: PSU1 present (0=present, 1=absent)
- bit 1: PSU1 pgood
- bit 4: PSU2 present (0=present, 1=absent)
- bit 5: PSU2 pgood

**Mux tree** — all five PCA9548 are behind i2c-1 (CP2112):

| Mux addr | Logical bus range | Use |
|----------|------------------|-----|
| 0x70 | 2–9 | QSFP ports 0–7 EEPROMs |
| 0x71 | 10–17 | QSFP ports 8–15 EEPROMs |
| 0x72 | 18–25 | QSFP ports 16–23 EEPROMs |
| 0x73 | 26–33 | QSFP ports 24–31 EEPROMs |
| 0x74 | 34–41 | ch2→PCA9535 0x22 (ports 0-15); ch3→PCA9535 0x23 (ports 16-31); ch6→24c64 0x50 |

**QSFP port-to-bus map** (from ONL `sfpi.c sfp_bus_index[]`, 0-indexed):
```
port  0→bus  3,  1→bus  2,  2→bus  5,  3→bus  4
port  4→bus  7,  5→bus  6,  6→bus  9,  7→bus  8
port  8→bus 11,  9→bus 10, 10→bus 13, 11→bus 12
...
port 28→bus 31, 29→bus 30, 30→bus 33, 31→bus 32
```
XOR-1 interleave (even/odd pairs swap). Verified: port 0 → bus 3 → identifier 0x11 (QSFP28).

**System EEPROM** — 24c64 at 0x50, mux 0x74 channel 6 (logical bus 40).
- Format: ONIE TLV (`TlvInfo\x00` magic header)
- Verified contents:
  - Part number: `20-001688`
  - Serial number: `AI09019591`
  - Base MAC: `00:90:fb:61:da:a1`
  - Vendor: Accton / Manufacturer: Joytech

**BMC** — OpenBMC on ASPEED AST2400, accessible via `/dev/ttyACM0` at 57600 baud.
BMC-side I2C topology (not host-visible):
- Bus 3: TMP75 sensors at 0x48–0x4c (5 sensors)
- Bus 8: TMP75 sensors at 0x48–0x49 (2 sensors), fan board at 0x33
- Bus 7: PSU PMBus mux PCA9546 at 0x70 (PSU1 ch2/0x59, PSU2 ch1/0x5a)

### Stale section

The `required_kernel_modules` array in the JSON lists Phase 1 modules
(`i2c_mux_pca954x`, `at24`, `optoe`). These are no longer loaded in Phase 2.
The `planned_kernel_modules` section was accurate for Phase R26 and is now
implemented. Neither section should be treated as authoritative for current state.

## Hardware-Verified Facts

- CPLD version 2.6 at i2c-1/0x32 (verified on hardware 2026-02-25)
- PSU register 0x10 live value `0xe0`: PSU1 present+no AC, PSU2 present+pgood
- System EEPROM TlvInfo magic confirmed: `54 6c 76 49 6e 66 6f 00` (verified on hardware 2026-03-14)
- QSFP port 0 (bus 3): identifier byte `0x11` (QSFP28) — confirmed SONiC kernel 6.1.0
- ONL comparison: `no-platform-modules.yml` — ONL loads zero custom kernel modules.
  Only custom module added for SONiC port is `wedge100s_cpld`.

## Remaining Known Gaps

- `required_kernel_modules` section in i2c_topology.json is stale (Phase 1 list).
  A future cleanup could update it to the Phase 2 module set.
- The `_grub_kernel_args.note` field still says "not yet applied" — that was fixed
  in Phase R30; the note is outdated.
- `notes/HARDWARE.md` references `HARDWARE_I2C.md` (§3 and §6 cross-references)
  which does not yet exist as a separate file.
