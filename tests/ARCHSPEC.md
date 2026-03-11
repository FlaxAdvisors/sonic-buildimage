# ARCHSPEC: SONiC Platform Support for Accton Wedge 100S-32X

*Implementation record — phases 0–10 complete and hardware-verified 2026-02-25; pmon device passthrough automated 2026-02-26.*
*Refactoring plan (phases R26–R31) added 2026-03-11 following ONL architecture review.*

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
C shared library (`libonlp-platform.so`).

**Key findings from source audit (2026-03-11):**

- `platform_lib.c` opens `/dev/ttyACM0` at 57600 baud — **identical** to our
  `bmc.py`.  ONL also uses the OpenBMC serial TTY for all BMC-side sensor access.
  There is no alternative BMC path (no IPMI KCS, no LPC bus, no USB network).
- **ONL has zero custom kernel modules** for this platform.
  `modules/PKG.yml` explicitly includes `no-platform-modules.yml`.
  All access uses upstream Linux drivers: `hid_cp2112`, `i2c_mux_pca954x`,
  `at24`, `optoe`, `gpio-pca953x`.
- `ledi.c` and `psui.c` use `onlp_i2c_readb()` / `onlp_i2c_writeb()` from
  `onlplib/i2c.h` — this is the Linux `i2c-dev` ioctl API, identical in
  mechanism to our `smbus2` calls (no subprocess fork).
- `thermali.c` uses `bmc_file_read_int()` over TTY to read
  `/sys/bus/i2c/drivers/lm75/<bus>/temp1_input` on the BMC.
  **Note:** this path (`drivers/lm75/`) is wrong on our OpenBMC — the correct
  path is `devices/<bus>/hwmon/*/temp1_input` (hwmonN is not stable).
- `platform-config/__init__.py` registers: 5×pca9548 (0x70–0x74) + 24c64
  at i2c-40/0x50.  It does **not** register PCA9535 or optoe1 devices.

**ONL vs our SONiC Python layer (current state):**

| Access path | ONL C code | Our Python |
|---|---|---|
| CPLD (LED/PSU presence) | `onlp_i2c_readb()` ioctl | `smbus2.read_byte_data()` ioctl |
| PCA9535 presence | `onlp_i2c_readb()` ioctl | GPIO sysfs (kernel gpio-pca953x) |
| BMC thermal sensors | TTY `cat /sys/bus/i2c/...` | TTY `cat /sys/bus/i2c/...` |
| BMC fan board | TTY `cat /sys/.../fan*_input` | TTY `cat /sys/.../fan*_input` |
| BMC PSU PMBus | TTY `i2cget -f -y` on BMC | TTY `i2cget -f -y` on BMC |
| QSFP EEPROM | N/A (ONLP only) | sysfs via optoe1 (lazy-registered) |

**Our Python implementation is already comparable to or better than ONL** for
host-side I2C.  The remaining gaps are (1) CPLD kernel driver, (2) lazy optoe1
registration causing DAC cable EEPROM failures, (3) no compiled BMC daemon.

### 2.2 SONiC Approach — Current State

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
   ┌───────────────────────────────────────────────────────────────┐
   │  Host I2C (CP2112 bridge, bus 1–41)                           │
   │  ├─ CPLD/PSU presence:  smbus2.read_byte_data()  [no fork]    │
   │  ├─ PCA9535 presence:   GPIO sysfs read()        [no fork]    │
   │  ├─ QSFP EEPROM:        optoe1 sysfs open/read() [no fork]    │
   │  └─ optoe1 registered:  lazily on first xcvrd access          │
   └───────────────────────────────────────────────────────────────┘
       │
   ┌───────────────────────────────────────────────────────────────┐
   │  BMC interface (/dev/ttyACM0, 57600 8N1)                      │
   │  ├─ Thermal sensors:   bmc.file_read_int() via TTY            │
   │  ├─ Fan presence/RPM:  bmc.file_read_int() via TTY            │
   │  └─ PSU PMBus:         bmc.i2cget_word() via TTY              │
   └───────────────────────────────────────────────────────────────┘
```

**Subprocess usage is now minimal** — eliminated for all host-side I2C.
Remaining subprocess calls are in `accton_wedge100s_util.py` (one-time boot
script) and the util `show` / `set` diagnostic commands.

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
| `sonic-platform-accton-wedge100s-32x.postinst` | ✓ DONE | `depmod -a`; enable+start init service; patches `pmon.sh` for ttyACM0; auto-removes stopped pmon container |

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

**Known defect — DAC cable EEPROM unreadable:**
DAC cables (and other transceivers) present at boot are NOT readable on SONiC
while they ARE readable on Arista EOS on the same hardware.  Root cause: optoe1
devices are registered **lazily** (first xcvrd access per port).  If xcvrd's
`update_port_transceiver_status_table()` fails or the mux deselects before the
first read completes, the EEPROM path does not exist yet when diagnostic tools
try to read it.  Arista pre-registers all transceiver drivers at boot.
**Fix (Phase R27):** register all 32 optoe1 devices in `mknod` during platform
init, before pmon starts.  Eliminates the lazy-init race entirely.

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

### 5.1 pmon Container Device Access

The SONiC pmon Docker container needs two categories of host devices passed
through to operate correctly on this platform.

**I2C buses 1–41** (CPLD, QSFP EEPROMs, PCA9535, system EEPROM):
Handled automatically — `get_pmon_device_mounts()` in `docker_image_ctl.j2`
already matches the pattern `i2c-[0-9]+`, so every `/dev/i2c-N` present on the
host at container-create time is passed through with `--device`.  No manual
action required.

**ttyACM0** (BMC USB-CDC serial interface):
`ttyACM[0-9]*` is not in the upstream regex.  Without it, all 7 BMC thermal
sensors and fan/PSU telemetry time out (~140 s per poll cycle).
This is patched automatically by `sonic-platform-accton-wedge100s-32x.postinst`,
which inserts the following line immediately after the `$(get_pmon_device_mounts)`
call in `/usr/bin/pmon.sh`:

```bash
$(for d in /dev/ttyACM*; do [ -c "$d" ] && echo "--device=$d:$d"; done) \
```

The patch is idempotent (skipped if `ttyACM` is already present in the file).

**Container recreation:** the pmon container must be **recreated** — not merely
restarted — to pick up new `--device` flags.  The postinst handles this
automatically: if the pmon container exists but is stopped it is removed so the
next supervisor start recreates it.  If pmon is running when the package is
installed, the postinst prints a warning; in that case reboot (or manually stop
pmon then run `docker rm pmon`) to pick up the new flags.

**WARNING:** never `docker rm -f pmon` while xcvrd is running — this hangs the
I2C bus and requires a full power cycle to recover.

### 5.2 Platform Init Service

`wedge100s-platform-init.service` runs at boot before `pmon.service` and:
1. Loads: `i2c_dev`, `i2c_i801`, `hid_cp2112`, `i2c_mux_pca954x`, `at24`, `optoe`
2. Registers: 5× pca9548 (i2c-1, 0x70–0x74), pca9535 (i2c-36/0x22, i2c-37/0x23), 24c64 (i2c-40/0x50)
3. Bus numbering is stable on SONiC kernel 6.1.0 if muxes are registered in address order.

### 5.3 Console

`CONSOLE_PORT=0x3f8`, `CONSOLE_DEV=ttyS0`, `CONSOLE_SPEED=57600` (confirmed from
GRUB on hardware).  The OpenBMC console is NOT on ttyS0 — it is only accessible
via ttyACM0 at 57600 baud from the host side.

### 5.4 GRUB Kernel Arguments (from ONL platform-config)

ONL's `platform-config/r0/src/lib/x86-64-accton-wedge100s-32x-r0.yml` specifies
these kernel arguments for the Wedge 100S (designed for kernel 4.9; verify on 6.1):

```
nopat  intel_iommu=off  noapic
console=ttyS0,57600n8  rd_NO_MD  rd_NO_LUKS
```

**Relevant to SSH sluggishness investigation:**
- `noapic` — disables APIC-based interrupt routing; may reduce BCM56960 IRQ storm
  impact on sshd responsiveness.  **Not yet applied to our installer.conf.**
- `intel_iommu=off` — disables VT-d IOMMU; ONL uses this for Broadwell-DE.
- `nopat` — disables PAT memory attribute table (compatibility flag).

These are targeted for Phase R30.

---

## 6. Known Limitations and Open Items

| Item | Severity | Notes |
|------|----------|-------|
| pmon.sh ttyACM0 passthrough | Resolved | Patched automatically by postinst; see Section 5.1 |
| DAC cable EEPROM unreadable | **High** | Lazy optoe1 registration; fix is Phase R27 (pre-register all 32 at boot) |
| SSH interactive sluggishness | **High** | BCM56960 IRQ storm (~150/s); `noapic` kernel arg is Phase R30 |
| Thermal poll cycle ~65s | Medium | 7 sequential BMC TTY reads; compiled daemon (Phase R28) reduces to <5s |
| PSU model/serial | Low | SMBus block-read not in bmc.py; `get_model()` returns `'N/A'` |
| Fan per-tray speed control | N/A | Not supported in hardware; `set_fan_speed.sh` is global |
| QSFP LP_MODE / RESET | N/A | Pins on mux board, not host-accessible |
| BMC cross-process lock | Low | Single process (pmon) accesses TTY; contention not yet observed |
| Bus number stability | Note | Verified on kernel 6.1.0-29-2-amd64 only; re-verify on kernel upgrade |
| No CPLD kernel driver | Medium | smbus2 works but kernel sysfs is cleaner; Phase R26 |

---

## 7. Reference Sources

1. **ONL sfpi.c** (SFP bus map, presence logic, bit-swap):
   `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/onlp/builds/x86_64_accton_wedge100s_32x/module/src/sfpi.c`
2. **ONL platform_lib.c** (BMC TTY pattern — C equivalent of our bmc.py):
   same path, `platform_lib.c`
3. **ONL fani.c / thermali.c / psui.c / ledi.c**:
   same path
4. **ONL platform-config yml** (GRUB/kernel args for Wedge 100S):
   `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/platform-config/r0/src/lib/x86-64-accton-wedge100s-32x-r0.yml`
5. **ONL platform-config __init__.py** (I2C device registration — reference):
   `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/platform-config/r0/src/python/x86_64_accton_wedge100s_32x_r0/__init__.py`
6. **ONL modules/PKG.yml** (confirms no-platform-modules for this platform):
   `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/modules/PKG.yml`
7. **Facebook Wedge 100 SONiC config** (BCM reference — non-S, different product):
   `device/facebook/x86_64-facebook_wedge100-r0/`
8. **Accton AS7712 kernel modules** (reference for CPLD driver pattern):
   `platform/broadcom/sonic-platform-modules-accton/as7712-32x/modules/`
   — `accton_i2c_cpld.c`, `accton_as7712_32x_fan.c`, `leds-accton_as7712_32x.c`
9. **Hardware discovery log**:
   `device/accton/x86_64-accton_wedge100s_32x-r0/i2c_bus_map.json`

---

## 8. Refactoring Roadmap — Driver-Based Architecture (Phases R26–R31)

*Planned 2026-03-11.  Builds on the current smbus2/TTY Python baseline.*
*Priority order matches expected impact; R27 is highest urgency.*

### Why refactor?

The current Python/smbus2/TTY implementation works but has three weaknesses:

1. **DAC cable EEPROMs fail** because optoe1 is lazily registered — not present
   in sysfs at boot, causing xcvrd or diagnostic tools to see empty reads.
2. **BMC poll cycle is slow** (~65 s for 7 thermals) because each TMP75 read
   is a separate TTY round-trip (send cmd, wait for BMC shell output).
3. **No CPLD kernel driver** means the LED and PSU subsystems depend on smbus2
   and have no standard kernel sysfs representation.

The SSH sluggishness is caused by the BCM56960 firing ~150 IRQs/second
(IRQ 16, `linux-kernel-bde`), saturating one CPU's HI softirq queue.
This is **not** caused by the Python I2C layer — but `noapic` (Phase R30)
may reduce its severity.

---

### Phase R26 — CPLD Kernel Driver

**Goal:** Replace smbus2 CPLD access with a standard kernel hwmon/sysfs driver.

**Files to create:**
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c`

**Pattern:** Based on `accton_as7712_32x_fan.c` and `accton_i2c_cpld.c` in the
AS7712 platform modules.  The wedge100s CPLD is much simpler — a single I2C
client at bus 1/0x32 with these relevant registers:

| Register | Purpose | sysfs attribute |
|---|---|---|
| 0x00 | CPLD version major | `cpld_version` |
| 0x10 | PSU presence/pgood | `psu1_present`, `psu1_pgood`, `psu2_present`, `psu2_pgood` |
| 0x3e | SYS LED 1 | `led_sys1` (0=off, 1=red, 2=green, 4=blue, blink +8) |
| 0x3f | SYS LED 2 | `led_sys2` (same encoding) |

**Driver registration:** Load in `kos` list as `modprobe wedge100s_cpld`, then
register device: `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device`

**Platform API update:**
- `psu.py`: replace `platform_smbus.read_byte(1, 0x32, 0x10)` with
  `open('/sys/bus/i2c/devices/1-0032/psu1_present').read()`
- `plugins/led_control.py` + `sonic_platform/` LED functions:
  write to `/sys/bus/i2c/devices/1-0032/led_sys1`

**Build integration:** Add `wedge100s_cpld.ko` to the `debian/rules` module
build step (same pattern as other accton platforms).

---

### Phase R27 — Pre-register All 32 optoe1 QSFP Devices at Boot  *(Critical)*

**Goal:** Ensure all 32 QSFP EEPROM paths exist in sysfs before pmon starts.
This fixes DAC cable EEPROM reads and eliminates the lazy-init race.

**Files to modify:**
- `utils/accton_wedge100s_util.py`: extend `mknod` list

**Change:** After registering the PCA9548 mux tree, add all 32 optoe1 devices:

```python
# Pre-register all QSFP EEPROM optoe1 devices (buses from SFP_BUS_MAP)
# Must come AFTER the PCA9548 muxes are registered so the bus numbers exist.
for bus in SFP_BUS_MAP:
    mknod_qsfp.append(
        'echo optoe1 0x50 > /sys/bus/i2c/devices/i2c-{}/new_device'.format(bus)
    )
```

**sfp.py update:** Remove `_register_device()` lazy registration path.  The
sysfs EEPROM path is always present; `get_transceiver_info()` reads directly
from `/sys/bus/i2c/devices/i2c-{bus}/{bus}-0050/eeprom` without needing to
register first.

**Cleanup path:** `device_uninstall()` must delete all optoe1 registrations in
reverse order (same `delete_device` echo pattern used for PCA9535 and 24c64).

**Expected outcome:** DAC cable EEPROMs readable immediately after platform init,
before xcvrd first polls the port.  Matches Arista EOS behavior.

---

### Phase R28 — Compiled BMC Polling Daemon

**Goal:** Replace per-call Python TTY I/O with a compiled C daemon that owns
the TTY, batches all sensor reads, and writes results to `/run/wedge100s/`.

**Files to create:**
- `utils/wedge100s-bmc-daemon.c` — compiled standalone binary
- `service/wedge100s-bmc-poller.service` — systemd one-shot wrapper
- `service/wedge100s-bmc-poller.timer` — systemd timer (10 s interval)

**Design (based directly on ONL `platform_lib.c`):**

```
wedge100s-bmc-daemon poll
  ├─ Open /dev/ttyACM0 (57600 8N1, blocking, VMIN=1)
  ├─ Login to BMC (if needed)
  ├─ Run one shell pipeline reading ALL sensors:
  │    cat /sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input
  │    ... (7 thermal + 10 fan RPM + 5 fan present + PSU mux+readw)
  ├─ Parse output
  ├─ Write /run/wedge100s/thermal_{1..7}
  │        /run/wedge100s/fan_{1..5}_{front,rear}_rpm
  │        /run/wedge100s/fan_{1..5}_present
  │        /run/wedge100s/psu_{1,2}_{vin,iin,iout,pout}
  └─ Close TTY
```

Single invocation reads ALL 7 thermals + 10 fan RPM + 5 presence + 2×4 PSU
registers in one TTY session (~3 s).  Current Python makes 24+ separate TTY
open/close cycles (~65 s).

**Build integration:** Add to `debian/rules` as a compiled binary target
(`gcc -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c`).  Install to
`/usr/local/bin/`.

**pmon container:** The `/run/wedge100s/` tmpfs directory must be bind-mounted
into the pmon container so Python platform code can read the files.  Add to
postinst (same pattern as ttyACM0 passthrough).

---

### Phase R29 — Python Platform API Update

**Goal:** Update thermal.py, fan.py, psu.py, led_control.py to read from
kernel sysfs (R26) and daemon output files (R28) instead of TTY/smbus2.

**thermal.py:**
```python
# Before (TTY, ~10s per sensor):
raw = bmc.file_read_int('/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input')
# After (file read, ~0.1ms):
with open('/run/wedge100s/thermal_1') as f:
    raw = int(f.read().strip())
```

**fan.py:**  Read from `/run/wedge100s/fan_{n}_{front,rear}_rpm` and
`fan_{n}_present`.  `set_speed()` still calls `bmc.send_command('set_fan_speed.sh %d')` —
no change (write path is infrequent).

**psu.py:** Read presence/pgood from CPLD sysfs (R26 driver attributes).
Read PMBus telemetry from `/run/wedge100s/psu_{n}_{vin,iin,iout,pout}`.

**plugins/led_control.py:** Write to CPLD sysfs attributes instead of
`i2cset` subprocess.  `sonic_platform/` LED functions same.

---

### Phase R30 — GRUB Kernel Args from ONL Config

**Goal:** Apply ONL's proven kernel arguments for this platform to reduce
BCM ASIC interrupt overhead and improve SSH responsiveness.

**Files to modify:**
- `device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf`

**Change:**
```
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"
```

**Verification needed on kernel 6.1:** `noapic` was valid on kernel 4.9 (ONL's
target).  Test effect on BCM IRQ handling before committing.  The BCM ASIC
still needs IRQ 16 to fire — `noapic` changes routing, not masking.

**Alternative:** IRQ affinity — pin `linux-kernel-bde` (IRQ 16) to CPU 0 via
`/proc/irq/16/smp_affinity`, leaving other CPUs free for sshd.  This can be
applied in the platform init script without GRUB changes.

---

### Phase R31 — IPMI/REST Investigation (Optional)

**Goal:** Determine whether OpenBMC on this platform exposes IPMI KCS or
Redfish REST — either would replace the TTY interface entirely.

**Investigation steps:**
1. Check for `/dev/ipmi0` on the host (IPMI KCS over LPC bus):
   ```bash
   ls /dev/ipmi* 2>/dev/null
   modprobe ipmi_si && ls /dev/ipmi*
   ```
2. Check for BMC USB network interface (OpenBMC CDC-ECM gadget):
   ```bash
   ip link show | grep -i usb
   ```
3. If BMC network exists, test Redfish:
   ```bash
   curl -k https://192.168.88.13/redfish/v1/Chassis/chassis/Thermal
   ```

**Expected outcome:** If IPMI KCS or Redfish is available, the TTY interface
can be retired in favour of a well-framed request/response protocol, eliminating
all the TTY-login/prompt-matching fragility.

If neither is available (most likely for this hardware vintage), the compiled
daemon (R28) is the best achievable approach.

---

### Phase Summary

| Phase | Scope | Priority | Key Files |
|---|---|---|---|
| R26 | CPLD kernel driver | Medium | `modules/wedge100s_cpld.c` |
| **R27** | **Pre-register all 32 optoe1** | **Critical** | `utils/accton_wedge100s_util.py` |
| R28 | Compiled BMC daemon | High | `utils/wedge100s-bmc-daemon.c` |
| R29 | Python API → sysfs/files | Follows R26+R28 | `sonic_platform/*.py` |
| R30 | GRUB kernel args + IRQ affinity | Medium | `installer.conf` / init script |
| R31 | IPMI/REST investigation | Low (exploratory) | — |
