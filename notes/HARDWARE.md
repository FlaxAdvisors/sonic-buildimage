# Wedge 100S-32X Hardware Reference

**Platform:** Accton Wedge 100S-32X (Facebook Wedge 100S, OEM by Joytech)
**SONiC branch:** `wedge100s`
**Verified on:** SONiC kernel 6.1.0-29-2-amd64, 2026-03-15

This document is the first stop for understanding the physical platform:
what chips exist, where they live, and how to reach them.

---

## 1. Platform Overview

| Component | Part | Notes |
|-----------|------|-------|
| CPU | Intel Atom C2538 (x86-64, 4-core, 2.4 GHz) | COME module |
| RAM | 8 GiB DDR3 (COME module) | — |
| Storage | 120 GiB eMMC (COME module) | SONiC 32GB root filesystem |
| ASIC | Broadcom BCM56960 (Tomahawk, 3.2 Tbps) | 32× QSFP28 ports |
| BMC | ASPEED AST2400 running OpenBMC | Manages fans, PSUs, thermal |
| CPLD | Custom (wedge100s_cpld) | LED, PSU presence/pgood |

### ONL Baseline

ONL (OpenNetworkLinux) is the reference platform for this port. ONL uses zero custom
kernel modules for the Wedge 100S-32X — the platform relied entirely on the CP2112
USB-HID bridge for I2C. The SONiC port follows the same philosophy: the only custom
kernel module added is `wedge100s_cpld` for CPLD sysfs.

---

## 2. Console and Management Access

### Host Console

- Device: `ttyS0` (on-board UART, addresses `0x3f8`)
- Speed: 57600 baud, 8N1
- GRUB kernel args: `console=ttyS0,57600n8 nopat intel_iommu=off noapic`

### BMC Serial Console (host → BMC)

The BMC exposes a USB-CDC ACM device to the host:

- Device: `/dev/ttyACM0`
- Speed: 57600 baud
- Login: `root` / `0penBmc`
- Prompt: `@bmc:` (hostname `hare-lorax-bmc`)

This is also used by `wedge100s-bmc-daemon` to poll thermal sensors, fans, and PSU
telemetry. Only one process should hold the tty at a time.

### SSH

- SONiC: `admin@192.168.88.12` (management interface)
- OpenBMC: `root@192.168.88.13`

---

## 3. I2C Bus Topology

> **Full detail:** See [HARDWARE_I2C.md §3](HARDWARE_I2C.md) for the complete mux
> tree with per-channel assignments and device addresses.

The full physical mux topology with address tables is in
[`notes/i2c_topology.json`](i2c_topology.json). The kernel-visible I2C surface is
intentionally reduced to `i2c-0` and `i2c-1` only — this is a deliberate design
choice to eliminate the write-attack surface that caused QSFP EEPROM corruption
during early bring-up. See `BEWARE_EEPROM.md §2–4`.

### Kernel-Visible Buses (Phase 2)

| Bus | Driver | Devices |
|-----|--------|---------|
| `i2c-0` | `i2c_i801` | RTC `0x08`, voltage monitor `0x44`, ADC `0x48` |
| `i2c-1` | `hid_cp2112` | CPLD `0x32` only |

**`i2c-2` through `i2c-41` do not exist in the running kernel.**
`i2c_mux_pca954x` is intentionally not loaded.

All traffic through the five PCA9548 muxes (`0x70`–`0x74`) is managed exclusively
by `wedge100s-i2c-daemon` via `/dev/hidraw0`.

### Physical Mux Summary

```
i2c-1 → PCA9548 @ 0x70  (QSFP EEPROMs, ports 0–7,  logical buses 2–9)
      → PCA9548 @ 0x71  (QSFP EEPROMs, ports 8–15, logical buses 10–17)
      → PCA9548 @ 0x72  (QSFP EEPROMs, ports 16–23, logical buses 18–25)
      → PCA9548 @ 0x73  (QSFP EEPROMs, ports 24–31, logical buses 26–33)
      → PCA9548 @ 0x74  (presence PCA9535 × 2, system EEPROM, logical buses 34–41)
```

---

## 4. CPLD

- **Bus/Address:** i2c-1 / `0x32`
- **Kernel driver:** `wedge100s_cpld`
- **sysfs root:** `/sys/bus/i2c/devices/1-0032/`
- **Source:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c`

### Registers

| Register | Description | Live value |
|----------|-------------|-----------|
| `0x00` | CPLD version major | `0x02` |
| `0x01` | CPLD version minor | `0x06` |
| `0x02` | Board ID | `0x65` |
| `0x10` | PSU presence/pgood | `0xe0` (verified 2026-02-25) |
| `0x3e` | SYS LED 1 | — |
| `0x3f` | SYS LED 2 | — |

### PSU Register (0x10) Bit Map

Polarity: `0` = present/good, `1` = absent/failed (active-low).

| Bits | Signal |
|------|--------|
| 0 | PSU1 present |
| 1 | PSU1 power good |
| 4 | PSU2 present |
| 5 | PSU2 power good |

### LED Encoding (registers 0x3e, 0x3f)

| Value | Color |
|-------|-------|
| `0x00` | Off |
| `0x01` | Red |
| `0x02` | Green |
| `0x04` | Blue |
| `+0x08` | Blink modifier (add to any color value) |

---

## 5. System EEPROM

- **Physical location:** 24c64, 8 KiB, behind PCA9548 mux `0x74` channel 6
- **Logical bus/address:** i2c-40 / `0x50` (bus exists only if `i2c_mux_pca954x` is loaded)
- **Format:** ONIE TLV (`TlvInfo\x00` magic header)
- **sysfs path:** `/sys/bus/i2c/devices/40-0050/eeprom` (Phase 1 / debugging only)

### Phase 2 Access

`wedge100s-i2c-daemon` reads the full 8 KiB via hidraw0 on first boot and writes
to `/run/wedge100s/syseeprom`. `eeprom.py` reads from this cache file.

### Verified Contents

| Field | Value |
|-------|-------|
| Part number | 20-001688 |
| Serial number | AI09019591 |
| Base MAC | 00:90:fb:61:da:a1 |
| Vendor | Accton |
| Manufacturer | Joytech |

---

## 6. QSFP Cages

> **Full detail:** See [HARDWARE_I2C.md §6](HARDWARE_I2C.md) for the full port-to-bus
> map table, XOR-1 presence interleave description, and mux channel assignments.

32 QSFP28 cages (100G each). RESET and LP_MODE pins are on the mux board and are
not accessible from the host CPU.

### Presence Detection

| Logical bus | Address | Ports | Mux path |
|-------------|---------|-------|----------|
| 36 | `0x22` | 0–15 | i2c-1 → 0x74 ch2 (PCA9535) |
| 37 | `0x23` | 16–31 | i2c-1 → 0x74 ch3 (PCA9535) |

Bits are active-low. Port N presence bit: `(N % 16) ^ 1` within its register byte
(XOR-1 interleave, from ONL `sfpi.c`, verified on hardware 2026-03-11).

### EEPROM

Address `0x50` per port, reached via PCA9548 muxes `0x70`–`0x73`.
Port-to-bus map pattern: `[3,2,5,4,7,6,9,8, 11,10,13,12,15,14,17,16, ...]` (0-indexed, from `sfp.py`).

### Phase 2 Access

All QSFP access is via `/run/wedge100s/sfp_N_present` and `/run/wedge100s/sfp_N_eeprom`
written by `wedge100s-i2c-daemon`. `i2c_mux_pca954x` is not loaded.

---

## 7. Thermal Sensors

### Host-Side

CPU core temperatures are available via the kernel coretemp driver:

```
/sys/class/hwmon/hwmon*/temp*_input   (millidegrees Celsius)
```

Use a glob — the hwmon index is not stable across reboots.

### BMC-Side (7× TMP75)

All seven ambient/inlet/exhaust temperature sensors are on BMC I2C buses and are
not visible to the host kernel. Access via `wedge100s-bmc-daemon` or directly over
`/dev/ttyACM0`.

| BMC bus | Addresses | Sensor count |
|---------|-----------|--------------|
| 3 | `0x48`, `0x49`, `0x4a`, `0x4b`, `0x4c` | 5 |
| 8 | `0x48`, `0x49` | 2 |

**BMC sysfs path** (on BMC filesystem, not host):
```
/sys/bus/i2c/devices/<bus>-<addr>/hwmon/*/temp1_input
```
Note: the path is `hwmon/*/temp1_input`, not the `lm75` driver path.

### Daemon Cache

`wedge100s-bmc-daemon` writes `/run/wedge100s/thermal_N` (1–7) in millidegrees every
10 s. `thermal.py` reads from these files.

---

## 8. Fan

5 fan trays, each with a front rotor and a rear rotor. Managed by OpenBMC.

### Fan Board

| BMC bus | Address | Driver |
|---------|---------|--------|
| 8 | `0x33` | Custom BMC fan driver |

**sysfs root (on BMC):** `/sys/bus/i2c/devices/8-0033/`

### sysfs Files

| File | Description |
|------|-------------|
| `fan<2n-1>_input` | Front rotor RPM for tray n (n=1–5, e.g., fan1, fan3, fan5, fan7, fan9) |
| `fan<2n>_input` | Rear rotor RPM for tray n (e.g., fan2, fan4, fan6, fan8, fan10) |
| `fantray_present` | Presence bitmap (bit n=0 means tray n+1 present) |

**Maximum RPM:** 15400
**Airflow direction:** Front-to-back (intake at front, exhaust at rear). Fixed, no reversal.

### Speed Control

```bash
set_fan_speed.sh <percent>   # global, applies to all trays equally
```

No per-tray control is available.

### Daemon Cache

`wedge100s-bmc-daemon` writes `/run/wedge100s/fan_N_front_rpm`, `fan_N_rear_rpm`,
and `fan_N_present` every 10 s. `fan.py` reads from these files.

---

## 9. PSU

2 PSU slots. Presence and pgood are visible to the host via CPLD register `0x10`
(see §4). Electrical telemetry is available via PMBus on the BMC.

### PMBus Access (BMC-side)

| BMC bus | Mux address | PSU | Mux channel value | PMBus address |
|---------|-------------|-----|-------------------|---------------|
| 7 | `0x70` | PSU1 | `0x02` (ch2) | `0x59` |
| 7 | `0x70` | PSU2 | `0x01` (ch1) | `0x5a` |

### Key PMBus Registers

| Register | Quantity | Format |
|----------|----------|--------|
| `0x88` | VIN (input voltage) | PMBus linear |
| `0x89` | IIN (input current) | PMBus linear |
| `0x8c` | IOUT (output current) | PMBus linear |
| `0x96` | POUT (output power) | PMBus linear |
| `0x9a` | Model string | raw ASCII (i2craw) |

### Daemon Cache

`wedge100s-bmc-daemon` writes `/run/wedge100s/psu_1_vin`, `psu_1_iin`, `psu_1_iout`,
`psu_1_pout` (and `psu_2_*`) every 10 s. `psu.py` reads from these files.

---

## 10. BCM Port Map

BCM56960 (Tomahawk), 32 front-panel ports. All ports are QSFP28 (100G).
Each port uses 4 BCM serdes lanes.

### Port-to-Lane Assignment

SONiC interface names use `EthernetN` where N = (panel_port_index - 1) × 4.
The `index` column in `port_config.ini` equals the front-panel port number (1-based).

| Panel port | SONiC iface | BCM lanes | EOS iface |
|------------|-------------|-----------|-----------|
| 1 | Ethernet0 | 117,118,119,120 | Et1/1–4 |
| 2 | Ethernet4 | 113,114,115,116 | Et2/1–4 |
| 3 | Ethernet8 | 125,126,127,128 | Et3/1–4 |
| 4 | Ethernet12 | 121,122,123,124 | Et4/1–4 |
| 5 | Ethernet16 | 5,6,7,8 | Et5/1–4 |
| 6 | Ethernet20 | 1,2,3,4 | Et6/1–4 |
| 7 | Ethernet24 | 13,14,15,16 | Et7/1–4 |
| 8 | Ethernet28 | 9,10,11,12 | Et8/1–4 |
| 9 | Ethernet32 | 21,22,23,24 | Et9/1–4 |
| 10 | Ethernet36 | 17,18,19,20 | Et10/1–4 |
| 11 | Ethernet40 | 29,30,31,32 | Et11/1–4 |
| 12 | Ethernet44 | 25,26,27,28 | Et12/1–4 |
| 13 | Ethernet48 | 37,38,39,40 | Et13/1–4 |
| 14 | Ethernet52 | 33,34,35,36 | Et14/1–4 |
| 15 | Ethernet56 | 45,46,47,48 | Et15/1–4 |
| 16 | Ethernet60 | 41,42,43,44 | Et16/1–4 |
| 17 | Ethernet64 | 53,54,55,56 | Et17/1–4 |
| 18 | Ethernet68 | 49,50,51,52 | Et18/1–4 |
| 19 | Ethernet72 | 61,62,63,64 | Et19/1–4 |
| 20 | Ethernet76 | 57,58,59,60 | Et20/1–4 |
| 21 | Ethernet80 | 69,70,71,72 | Et21/1–4 |
| 22 | Ethernet84 | 65,66,67,68 | Et22/1–4 |
| 23 | Ethernet88 | 77,78,79,80 | Et23/1–4 |
| 24 | Ethernet92 | 73,74,75,76 | Et24/1–4 |
| 25 | Ethernet96 | 85,86,87,88 | Et25/1–4 |
| 26 | Ethernet100 | 81,82,83,84 | Et26/1–4 |
| 27 | Ethernet104 | 93,94,95,96 | Et27/1–4 |
| 28 | Ethernet108 | 89,90,91,92 | Et28/1–4 |
| 29 | Ethernet112 | 101,102,103,104 | Et29/1–4 |
| 30 | Ethernet116 | 97,98,99,100 | Et30/1–4 |
| 31 | Ethernet120 | 109,110,111,112 | Et31/1–4 |
| 32 | Ethernet124 | 105,106,107,108 | Et32/1–4 |

### Tomahawk Internal Note

- Lane pairs are interleaved within each group of 8 (odd/even pairs swap). This is
  the ASIC's internal pipe arrangement, not a wiring anomaly.

### BCM Config Files

| File | Purpose |
|------|---------|
| `device/facebook/x86_64-facebook_wedge100-r0/Facebook-W100-C32/th-wedge100-32x100G.config.bcm` | original 32× 100G config |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` | DPB-capable flex config |
