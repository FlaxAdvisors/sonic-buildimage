# ARCHSPEC: SONiC Platform Support for Accton Wedge 100S-32X

*Implementation record — all 10 phases complete and hardware-verified 2026-02-25.*

---

## 1. Hardware Architecture

### 1.1 CPU and BMC Split

The Wedge 100S has **two independent processing domains**.  Everything below is
confirmed on hardware (SONiC kernel 6.1.0-29-2-amd64, host: hare-lorax).

```
Host CPU (Intel Broadwell-DE D1508)
  ├─ i2c-0: Intel SMBus I801 (driver: i2c_i801)
  │    ├─ 0x08: RTC/Clock
  │    ├─ 0x44: Voltage monitor
  │    └─ 0x48: ADS1015 12-bit ADC
  │
  └─ USB → CP2112 HID I2C Bridge (10c4:ea90, /dev/hidraw0)
       └─ i2c-1: CP2112 SMBus Bridge  [driver: hid_cp2112]
            ├─ 0x32: System CPLD  [v2.6, board ID 0x65]
            │    PSU presence/status at reg 0x10
            │    LED control at regs 0x3e (SYS1), 0x3f (SYS2)
            └─ PCA9548 mux tree (0x70–0x74)  [driver: i2c_mux_pca954x]
                 ├─ i2c-2 to i2c-33:  32× QSFP28 EEPROMs at 0x50 (optoe1)
                 ├─ i2c-36:  PCA9535 @ 0x22 (QSFP presence ports 0–15)
                 ├─ i2c-37:  PCA9535 @ 0x23 (QSFP presence ports 16–31)
                 └─ i2c-40:  24c64 EEPROM @ 0x50  [ONIE TlvInfo, S/N: AI09019591]

IMPORTANT: i2c_ismt is NOT present on this board.
           The CP2112 USB-HID bridge is the sole I2C master on the host side.

OpenBMC (ARM processor, separate chassis management module)
  ├─ /dev/ttyACM0 on host (57600 8N1) ← sole host-to-BMC interface
  │    Login: root / 0penBmc
  │    Prompt: "root@HOSTNAME:~# " — match on b':~# ' not b'@bmc:'
  │    I/O mode: BLOCKING + VMIN=1  (ttyACM does not signal select() in O_NONBLOCK)
  ├─ BMC i2c-3: TMP75 thermal sensors @ 0x48–0x4c (5 sensors, mainboard)
  ├─ BMC i2c-7: PSU PMBus via PCA9546 mux @ 0x70 (PSU1@0x59, PSU2@0x5a)
  └─ BMC i2c-8: Fan board controller @ 0x33, TMP75 @ 0x48/0x49
       ├─ fantray_present sysfs attribute (hex; 0x0 = all present)
       ├─ fan1_input..fan10_input (5 trays × front/rear rotors)
       └─ set_fan_speed.sh <pct>  (controls all trays simultaneously)
```

### 1.2 I2C Mux Tree (Host CPU)

5× PCA9548 8-channel muxes on i2c-1, registered in address order to preserve
stable bus numbering on SONiC kernel 6.1.0 (matches ONL numbering exactly):

| Mux Address | Buses Created | Primary Use |
|-------------|---------------|-------------|
| 0x70 | i2c-2 to i2c-9   | QSFP ports 0–7 |
| 0x71 | i2c-10 to i2c-17 | QSFP ports 8–15 |
| 0x72 | i2c-18 to i2c-25 | QSFP ports 16–23 |
| 0x73 | i2c-26 to i2c-33 | QSFP ports 24–31 |
| 0x74 | i2c-34 to i2c-41 | SFP GPIO expanders, IDPROM |

QSFP port-to-bus mapping (from `sfpi.c`, 0-based port index):
```
Port  0→bus3,   1→bus2,   2→bus5,   3→bus4,   4→bus7,   5→bus6,   6→bus9,   7→bus8,
Port  8→bus11,  9→bus10, 10→bus13, 11→bus12, 12→bus15, 13→bus14, 14→bus17, 15→bus16,
Port 16→bus19, 17→bus18, 18→bus21, 19→bus20, 20→bus23, 21→bus22, 22→bus25, 23→bus24,
Port 24→bus27, 25→bus26, 26→bus29, 27→bus28, 28→bus31, 29→bus30, 30→bus33, 31→bus32
```

### 1.3 Confirmed Device Map

| Subsystem | Bus | Address | Notes |
|-----------|-----|---------|-------|
| System CPLD | i2c-1 | 0x32 | PSU presence/status reg 0x10; LED regs 0x3e/0x3f |
| PSU1 present | i2c-1/0x32 | — | reg 0x10, bit 0 (0=present) |
| PSU1 power good | i2c-1/0x32 | — | reg 0x10, bit 1 |
| PSU2 present | i2c-1/0x32 | — | reg 0x10, bit 4 (0=present) |
| PSU2 power good | i2c-1/0x32 | — | reg 0x10, bit 5 |
| LED SYS1 | i2c-1/0x32 | — | reg 0x3e; 0=off, 1=red, 2=green, 4=blue |
| LED SYS2 | i2c-1/0x32 | — | reg 0x3f; same encoding |
| System EEPROM | i2c-40 | 0x50 | 24c64, ONIE TlvInfo |
| QSFP EEPROM | i2c-2..33 | 0x50 | optoe1 driver, lazy instantiation via new_device |
| QSFP DOM | i2c-2..33 | 0x51 | upper page via optoe1 |
| SFP presence 0–15 | i2c-36 | 0x22 | PCA9535, offset 0 (ports 0–7), offset 1 (ports 8–15) |
| SFP presence 16–31 | i2c-37 | 0x23 | PCA9535, offset 0 (ports 16–23), offset 1 (ports 24–31) |
| Thermal 1–5 | BMC i2c-3 | 0x48–0x4c | TMP75; sysfs via `devices/<dev>/hwmon/*/temp1_input` |
| Thermal 6–7 | BMC i2c-8 | 0x48–0x49 | TMP75; same sysfs pattern |
| Fan board | BMC i2c-8 | 0x33 | fantray_present, fan1..10_input |
| Fan control | BMC | — | `set_fan_speed.sh <pct>` via TTY |
| PSU1 PMBus | BMC i2c-7 via mux@0x70 | 0x59 | mux channel 0x02 |
| PSU2 PMBus | BMC i2c-7 via mux@0x70 | 0x5a | mux channel 0x01 |

### 1.4 PCA9535 Bit-Swap Caveat

GPIO lines on both PCA9535 expanders are wired in interleaved (even/odd swapped)
order relative to the front-panel QSFP port sequence.  All presence reads must
apply `onlp_sfpi_reg_val_to_port_sequence()` from `sfpi.c` — which swaps adjacent
bit pairs — before testing any individual bit.  This is implemented in
`sfputil.py:_bit_swap()` and `sfp.py:_bit_swap()`.

---

## 2. ONL vs SONiC Platform Tooling

### 2.1 ONL (OpenNetworkLinux) Approach

ONL uses the **ONLP** C API consumed by `onlpd`.  All hardware access is in a
C shared library; the BMC is reached by blocking TTY I/O in `platform_lib.c`.
ONL source in `/home/dbahi/git/OpenNetworkLinux/` was used as the authoritative
reference for all hardware addresses, register layouts, and BMC command patterns.

### 2.2 SONiC Approach

```
SONiC CLI / APP layer
       │
    Redis DB
       │
   pmon daemons (xcvrd, psud, thermalctld, ledd, syseepromd)
       │
   Platform API (Python)
   ├─ Legacy:  device/*/plugins/{sfputil,psuutil,eeprom,led_control}.py
   └─ Modern:  platform/*/sonic_platform/{chassis,fan,thermal,sfp,psu,eeprom,bmc}.py
       │
   Kernel drivers / sysfs / i2cget+i2cset subprocesses / BMC TTY
```

### 2.3 ONL-to-SONiC Translation Map

| ONL Component | SONiC Equivalent | Status |
|---------------|-----------------|--------|
| `platform_lib.c` BMC TTY | `sonic_platform/bmc.py` | ✓ |
| `thermali.c` | `sonic_platform/thermal.py` | ✓ |
| `fani.c` | `sonic_platform/fan.py` + `FanDrawer` | ✓ |
| `psui.c` (presence) | `plugins/psuutil.py` | ✓ |
| `psui.c` (telemetry) | `sonic_platform/psu.py` | ✓ |
| `sfpi.c` (presence + eeprom) | `plugins/sfputil.py` + `sonic_platform/sfp.py` | ✓ |
| `sysi.c` EEPROM | `plugins/eeprom.py` + `sonic_platform/eeprom.py` | ✓ |
| `ledi.c` | `plugins/led_control.py` (ledd plugin) | ✓ |
| ONLP mux init | `utils/accton_wedge100s_util.py` | ✓ |

---

## 3. File Inventory

### 3.1 Device directory: `device/accton/x86_64-accton_wedge100s_32x-r0/`

| File | State | Notes |
|------|-------|-------|
| `Accton-WEDGE100S-32X/port_config.ini` | ✓ DONE | 32×100G; 1-based index column (1–32) |
| `Accton-WEDGE100S-32X/sai.profile` | ✓ DONE | References `th-wedge100s-32x100G.config.bcm` |
| `Accton-WEDGE100S-32X/th-wedge100s-32x100G.config.bcm` | ✓ VERIFIED | BCM56960 Tomahawk; lane assignments match port_config.ini |
| `default_sku` | ✓ DONE | `Accton-WEDGE100S-32X t1` |
| `platform_asic` | ✓ DONE | `broadcom` |
| `installer.conf` | ✓ DONE | `CONSOLE_PORT=0x3f8`, `CONSOLE_DEV=ttyS0`, `CONSOLE_SPEED=57600` |
| `pmon_daemon_control.json` | ✓ DONE | All daemons enabled: xcvrd, psud, thermalctld, ledd |
| `i2c_bus_map.json` | ✓ DONE | Authoritative bus map from hardware discovery |
| `plugins/eeprom.py` | ✓ DONE | `TlvInfoDecoder`; path `/sys/bus/i2c/devices/40-0050/eeprom` |
| `plugins/psuutil.py` | ✓ DONE | CPLD i2c-1/0x32, reg 0x10, correct bit polarity |
| `plugins/sfputil.py` | ✓ DONE | PCA9535 presence + mux-tree EEPROM paths; bit-swap applied |
| `plugins/led_control.py` | ✓ DONE | `LedControlBase`; SYS1=0x3e green on init; SYS2=0x3f tracks link-up |

### 3.2 Platform directory: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/`

| File | State | Notes |
|------|-------|-------|
| `sonic_platform/bmc.py` | ✓ DONE | Open/close per command; BLOCKING+VMIN=1; prompt `b':~# '` |
| `sonic_platform/thermal.py` | ✓ DONE | 8 sensors: 1 CPU coretemp (host) + 7 TMP75 (BMC TTY) |
| `sonic_platform/fan.py` | ✓ DONE | `Fan` + `FanDrawer`; presence+RPM via BMC sysfs; set via `set_fan_speed.sh` |
| `sonic_platform/psu.py` | ✓ DONE | Presence/pgood from CPLD; PMBus telemetry (VIN/IIN/IOUT/POUT) via BMC |
| `sonic_platform/sfp.py` | ✓ DONE | `SfpOptoeBase`; lazy `optoe1` device registration; bit-swap presence |
| `sonic_platform/eeprom.py` | ✓ DONE | `TlvInfoDecoder` at `40-0050/eeprom` |
| `sonic_platform/chassis.py` | ✓ DONE | Populates thermals, fan drawers, PSUs, SFPs (0-sentinel at index 0), eeprom |
| `sonic_platform/platform.py` | ✓ DONE | `Platform → Chassis` |
| `sonic_platform/__init__.py` | ✓ DONE | `from .platform import Platform` |
| `sonic_platform_setup.py` | ✓ DONE | Builds `sonic_platform-1.0-py3-none-any.whl` |
| `utils/accton_wedge100s_util.py` | ✓ DONE | Loads kos (incl. `optoe`); registers 5×PCA9548 + PCA9535×2 + 24c64 |
| `service/wedge100s-platform-init.service` | ✓ DONE | `Before=pmon.service`, oneshot |
| `modules/` | ✓ REMOVED | No custom kernel drivers needed; all access via upstream drivers |
| `onl/` | ✓ REMOVED | ONL ONLP layer — reference only, not used in SONiC build |

### 3.3 Debian packaging: `platform/broadcom/sonic-platform-modules-accton/debian/`

| File | State | Notes |
|------|-------|-------|
| `control` | ✓ DONE | `sonic-platform-accton-wedge100s-32x` package stanza added |
| `rules` | ✓ DONE | `wedge100s-32x` in `MODULE_DIRS`; conditional udev cp; elif for `sonic_platform_setup.py` |
| `sonic-platform-accton-wedge100s-32x.install` | ✓ DONE | Installs wheel to `usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/` |
| `sonic-platform-accton-wedge100s-32x.postinst` | ✓ DONE | `#!/bin/sh`; `depmod -a`; enable+start init service |

---

## 4. Implementation Notes by Subsystem

### 4.1 BMC TTY (`bmc.py`)

Three deviations from the C `platform_lib.c` pattern required by this hardware:

| Issue | C code assumption | Hardware reality | Fix |
|---|---|---|---|
| `O_NONBLOCK` + `select()` | works | `ttyACM` (USB CDC) does not signal `select()` readiness under `O_NONBLOCK` | Blocking mode + `VMIN=1` |
| Login prompt | `b'@bmc:'` | `root@hare-lorax-bmc:~# ` — hostname varies | `_TTY_PROMPT = b':~# '` |
| Login detection | `b'bmc login:'` | `b'hare-lorax-bmc.sesame.lab login:'` | Check `b' login:'` |

Threading lock serialises access within one process.  When multiple pmon daemons
poll simultaneously, cross-process serialisation can be added via `fcntl.flock`
on a lock file if contention is observed in production.

### 4.2 Thermal (`thermal.py`)

- CPU Core: glob `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input`
  — reports max across all cores.  The intermediate `hwmon/hwmon*` path is
  required; the older `drivers/coretemp/...` path does not exist on this kernel.
- TMP75 sensors: BMC sysfs path must use `devices/<bus>/hwmon/*/temp1_input`
  (wildcard required — hwmonN is not stable on OpenBMC).  The `drivers/lm75/`
  path used in `thermali.c` is wrong on OpenBMC and returns nothing.
- 8 sensors: thresholds 95/102 °C for CPU core, 70/80 °C for all TMP75.

### 4.3 Fan (`fan.py`)

- `fantray_present` sysfs attribute returns a hex value (e.g. `0x00`);
  must be read with `base=16` in `bmc.file_read_int()`.
- `set_fan_speed.sh` controls all 5 trays simultaneously (no per-tray control).
- `get_target_speed()` raises `NotImplementedError` before first `set_speed()`
  call — this tells thermalctld to skip under/over-speed checks until a speed
  has been commanded, avoiding false "Not OK" alarms on startup.
- Max RPM: 15400.  Direction: F2B (INTAKE), fixed per `fani.c`.

### 4.4 PSU (`psu.py`)

- Presence and power-good come from CPLD register 0x10 via host i2cget
  (no BMC needed; faster and independent of BMC TTY availability).
- PMBus telemetry uses LINEAR11 format (5-bit exponent + 11-bit mantissa).
- DC output voltage is computed as POUT/IOUT rather than reading VOUT_MODE
  (avoids LINEAR16 complexity; mirrors `psui.c` approach).
- MFR_MODEL (0x9a) is not read — SMBus block-read is not implemented in bmc.py.
- Telemetry is cached for 30 s to reduce BMC TTY load.

### 4.5 QSFP/SFP (`sfp.py`, `sfputil.py`)

- All 32 ports are QSFP28.  EEPROM access uses `optoe1` driver registered
  lazily on first access via `new_device` sysfs interface.
- PCA9535 presence bytes are cached for 1 s; all 32 Sfp instances share the
  cache so one thermalctld/xcvrd poll round hits the bus at most 4 times
  (2 PCA9535s × 2 offsets each).
- LP_MODE and RESET pins are on the mux board and not accessible from the
  host CPU.  `get_lpmode()`, `reset()`, and `set_lpmode()` return False/False.
- `chassis.py` prepends a `None` sentinel at `_sfp_list[0]` because
  `port_config.ini` uses a 1-based `index` column (1–32), so `get_sfp(1)`
  must yield port 0, not port 1.

### 4.6 System EEPROM (`eeprom.py`, `plugins/eeprom.py`)

- 24c64 AT24C64 at i2c-40/0x50, registered by the platform init service.
- ONIE TlvInfo format; S/N AI09019591 confirmed on hardware.
- `SysEeprom` caches the decoded dict after first read.

### 4.7 LED Control (`plugins/led_control.py`)

- ledd calls `port_link_state_change(port, state)` for every port event.
- SYS1 (0x3e) set green on `LedControl.__init__()` — stays green while ledd runs.
- SYS2 (0x3f) set green when any port is up, off when all ports are down.
- ledd reads `STATE_DB PORT_TABLE` field `netdev_oper_status` (not APPL_DB,
  not `oper_status`).
- ledd must start AFTER `sonic-cfggen` has written ports to `CONFIG_DB`.
  If ledd starts with an empty CONFIG_DB it will idle with no port events;
  restart via `supervisorctl restart ledd` inside the pmon container.

---

## 5. Deployment Requirements

### 5.1 pmon Container Device Access (CRITICAL — manual post-install)

The SONiC pmon Docker container does not pass through `/dev/ttyACM0` or the
I2C device nodes by default.  The following two additions to `/usr/bin/pmon.sh`
(inside the `docker create` command) are required for full pmon functionality.
They must be applied after each image deployment (they do not survive a Docker
image rebuild unless the pmon Dockerfile source is patched):

**ttyACM0** (add after the `ipmi0` conditional block):
```bash
$(if [ -e "/dev/ttyACM0" ]; then echo "--device=/dev/ttyACM0:/dev/ttyACM0"; fi) \
```

**I2C buses 1–41** (CPLD, QSFP EEPROMs, PCA9535, system EEPROM):
```bash
$(for n in $(seq 1 41); do dev="/dev/i2c-$n"; [ -e "$dev" ] && echo "--device=$dev:$dev"; done) \
```

After editing pmon.sh, the container must be **recreated** (not just restarted):
`docker rm pmon` then let SONiC restart it.
**WARNING:** never `docker rm -f pmon` while xcvrd is running — this hangs the
I2C bus and requires a full power cycle to recover.

Without ttyACM0: all 7 BMC thermal sensors time out (~140 s per poll cycle).
Without i2c nodes: CPLD reads fail (PSU presence, LED control) and QSFP
EEPROM reads fail.

### 5.2 Platform Init Service

`wedge100s-platform-init.service` runs at boot before `pmon.service` and:
1. Loads: `i2c_dev`, `i2c_i801`, `hid_cp2112`, `i2c_mux_pca954x`, `at24`, `optoe`
2. Registers: 5× pca9548 (i2c-1, 0x70–0x74), pca9535 (i2c-36/0x22, i2c-37/0x23), 24c64 (i2c-40/0x50)
3. Bus numbering is stable on SONiC kernel 6.1.0 if muxes are registered in address order.

### 5.3 Console

`CONSOLE_PORT=0x3f8`, `CONSOLE_DEV=ttyS0`, `CONSOLE_SPEED=57600` (confirmed from
GRUB on hardware).  The OpenBMC console is NOT on ttyS0 — it is only accessible
via ttyACM0 at 57600 baud from the host side.

---

## 6. Known Limitations and Open Items

| Item | Severity | Notes |
|------|----------|-------|
| pmon.sh device passthrough | **Deployment blocker** | Manual post-install step; see Section 5.1 |
| PSU model/serial | Low | SMBus block-read not in bmc.py; `get_model()` returns `'N/A'` |
| Fan per-tray speed control | N/A | Not supported in hardware; `set_fan_speed.sh` is global |
| QSFP LP_MODE / RESET | N/A | Pins on mux board, not host-accessible |
| BMC cross-process lock | Low | Single process (pmon) accesses TTY; contention not yet observed |
| Bus number stability | Note | Verified on kernel 6.1.0-29-2-amd64 only; re-verify on kernel upgrade |

---

## 7. Reference Sources

1. **ONL sfpi.c** (SFP bus map, presence logic, bit-swap):
   `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/onlp/builds/x86_64_accton_wedge100s_32x/module/src/sfpi.c`
2. **ONL platform_lib.c** (BMC TTY pattern):
   same path, `platform_lib.c`
3. **ONL fani.c / thermali.c / psui.c / ledi.c**:
   same path
4. **Facebook Wedge 100 SONiC config** (BCM reference):
   `device/facebook/x86_64-facebook_wedge100-r0/`
5. **Accton AS7712** (similar Tomahawk, fuller SONiC implementation):
   `device/accton/x86_64-accton_as7712_32x-r0/`
6. **Hardware discovery log**:
   `device/accton/x86_64-accton_wedge100s_32x-r0/i2c_bus_map.json`
