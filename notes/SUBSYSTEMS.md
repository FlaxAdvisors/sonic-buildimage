# Platform Subsystems — Accton Wedge 100S-32X

Per-subsystem reference for developers implementing tests or debugging platform API
behaviour on the Accton Wedge 100S-32X SONiC port.

Each section follows the format: **Hardware → Driver/Daemon → Python API → Pass
Criteria → Known Gaps**. Every claim is derived from the source files listed below.

Because the combined content exceeds 500 lines, each subsystem has its own file.

## Table of Contents

| Subsystem | File | Key classes / paths |
|---|---|---|
| [System EEPROM](#system-eeprom) | [SUBSYSTEMS_EEPROM.md](SUBSYSTEMS_EEPROM.md) | `SysEeprom` · `/run/wedge100s/syseeprom` |
| [QSFP / SFP](#qsfp--sfp) | [SUBSYSTEMS_QSFP.md](SUBSYSTEMS_QSFP.md) | `Sfp` · `/run/wedge100s/sfp_N_{present,eeprom}` |
| [Fan](#fan) | [SUBSYSTEMS_FAN.md](SUBSYSTEMS_FAN.md) | `Fan`, `FanDrawer` · `/run/wedge100s/fan_*` |
| [PSU](#psu) | [SUBSYSTEMS_PSU.md](SUBSYSTEMS_PSU.md) | `Psu` · CPLD sysfs + `/run/wedge100s/psu_*` |
| [Thermal](#thermal) | [SUBSYSTEMS_THERMAL.md](SUBSYSTEMS_THERMAL.md) | `Thermal` · coretemp + `/run/wedge100s/thermal_N` |
| [LED](#led) | [SUBSYSTEMS_LED.md](SUBSYSTEMS_LED.md) | `LedControl`, `Chassis.set_status_led` · CPLD sysfs |
| [CPLD](#cpld) | [SUBSYSTEMS_CPLD.md](SUBSYSTEMS_CPLD.md) | `wedge100s_cpld` kmod · `/sys/bus/i2c/devices/1-0032/` |
| [Port Config](#port-config) | [SUBSYSTEMS_PORTCONFIG.md](SUBSYSTEMS_PORTCONFIG.md) | `port_config.ini` · `_SFP_BUS_MAP` |

## Source Files

| File | Role |
|---|---|
| `sonic_platform/eeprom.py` | SysEeprom class |
| `sonic_platform/sfp.py` | Sfp class, bus map, presence logic |
| `sonic_platform/fan.py` | Fan, FanDrawer classes |
| `sonic_platform/psu.py` | Psu class, LINEAR11 decoder |
| `sonic_platform/thermal.py` | Thermal class, sensor table |
| `sonic_platform/chassis.py` | Chassis assembly, SYS1 LED, bulk presence |
| `sonic_platform/platform_smbus.py` | Shared SMBus handle pool |
| `device/.../plugins/led_control.py` | LedControl plugin (ledd / SYS2) |
| `modules/wedge100s_cpld.c` | CPLD kernel driver |
| `notes/i2c_topology.json` | Hardware addresses and topology reference |
| `device/.../Accton-WEDGE100S-32X/port_config.ini` | Port lane / index / FEC config |

All `sonic_platform/` paths are relative to
`platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/`.

## Architecture Notes

### Daemon-Mediated I2C

In Phase 2 (current production), the kernel modules `i2c_mux_pca954x`, `at24`, and
`optoe` are **not loaded**. Bus numbers `i2c-2` through `i2c-41` do not exist.

All mux-tree I2C (QSFP EEPROM, system EEPROM) is owned by `wedge100s-i2c-daemon`,
which accesses the CP2112 USB HID bridge directly via `/dev/hidraw0`. Results are
written to `/run/wedge100s/` at regular intervals. Python API classes read these files
with fallbacks to direct smbus2 for the brief window before the daemon's first write.

All BMC-side data (thermal sensors, fan RPM, PSU PMBus telemetry) is owned by
`wedge100s-bmc-daemon`, which communicates with the OpenBMC via `/dev/ttyACM0` at
57600 baud.

### /run/wedge100s/ File Summary

| File pattern | Written by | Consumer |
|---|---|---|
| `syseeprom` | `wedge100s-i2c-daemon` | `eeprom.py` |
| `sfp_N_present` | `wedge100s-i2c-daemon` (3 s) | `sfp.py`, `chassis.py` |
| `sfp_N_eeprom` | `wedge100s-i2c-daemon` (on insert) | `sfp.py` |
| `fan_present` | `wedge100s-bmc-daemon` (10 s) | `fan.py` |
| `fan_N_front`, `fan_N_rear` | `wedge100s-bmc-daemon` (10 s) | `fan.py` |
| `thermal_1`–`thermal_7` | `wedge100s-bmc-daemon` (10 s) | `thermal.py` |
| `psu_N_vin`, `psu_N_iin`, `psu_N_iout`, `psu_N_pout` | `wedge100s-bmc-daemon` (10 s) | `psu.py` |

### CPLD at 1-0032

The CPLD at `i2c-1/0x32` is the only I2C device directly accessible from the host
without going through the mux tree. The `wedge100s_cpld` kernel driver registers it
at boot and exposes 7 sysfs attributes. It is the host-side source of truth for PSU
presence/pgood and the LED control path.

---

## System EEPROM

See [SUBSYSTEMS_EEPROM.md](SUBSYSTEMS_EEPROM.md).

Hardware: 24C64 at `0x50` on `i2c-40` (CP2112 → mux `0x74` ch6). Primary path:
`/run/wedge100s/syseeprom` written by `wedge100s-i2c-daemon`. Class `SysEeprom`
inherits `TlvInfoDecoder`; `get_eeprom()` returns a dict keyed by hex TLV type codes.

---

## QSFP / SFP

See [SUBSYSTEMS_QSFP.md](SUBSYSTEMS_QSFP.md).

Hardware: 32 × QSFP28. Presence via PCA9535 (buses 36/37), EEPROM via daemon cache
`/run/wedge100s/sfp_N_eeprom`. Class `Sfp` inherits `SfpOptoeBase`. RESET and LP_MODE
pins not host-accessible.

---

## Fan

See [SUBSYSTEMS_FAN.md](SUBSYSTEMS_FAN.md).

Hardware: 5 fan trays, 2 rotors each, BMC i2c-8/0x33. Daemon writes
`/run/wedge100s/fan_N_{front,rear}` and `fan_present`. Classes `Fan` and `FanDrawer`.
Speed set via BMC `set_fan_speed.sh` (all trays simultaneously).

---

## PSU

See [SUBSYSTEMS_PSU.md](SUBSYSTEMS_PSU.md).

Hardware: 2 × 650 W AC PSUs. Presence/pgood from CPLD sysfs; PMBus telemetry from
daemon. Class `Psu`; LINEAR11 decoder in module scope.

---

## Thermal

See [SUBSYSTEMS_THERMAL.md](SUBSYSTEMS_THERMAL.md).

Hardware: 1 × host coretemp (CPU), 7 × TMP75 (BMC-side). Daemon writes
`/run/wedge100s/thermal_N`. Class `Thermal`; thresholds hardcoded in `_SENSORS` table.

---

## LED

See [SUBSYSTEMS_LED.md](SUBSYSTEMS_LED.md).

Hardware: SYS1 (CPLD reg `0x3e`) and SYS2 (CPLD reg `0x3f`). SYS1 owned by
`healthd`/`chassis.py`; SYS2 owned by `ledd`/`led_control.py`. No per-port or
per-fan-tray LEDs are individually addressable.

---

## CPLD

See [SUBSYSTEMS_CPLD.md](SUBSYSTEMS_CPLD.md).

Hardware: single CPLD at `i2c-1/0x32`. Kernel module `wedge100s_cpld` (Phase R26).
7 sysfs attributes: `cpld_version`, `psu{1,2}_present`, `psu{1,2}_pgood`,
`led_sys{1,2}`.

---

## Port Config

See [SUBSYSTEMS_PORTCONFIG.md](SUBSYSTEMS_PORTCONFIG.md).

Static file `port_config.ini`: 32 × 100G RS-FEC ports, non-sequential Tomahawk lane
assignments. `_SFP_BUS_MAP` in `sfp.py` maps 0-based port index to I2C bus number.
`chassis.py` uses a `None` sentinel at `_sfp_list[0]` to align 1-based index.
