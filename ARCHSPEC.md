# ARCHSPEC: SONiC Platform Support for Accton Wedge 100S-32X

*Generated: 2026-02-25*

---

## 1. Workspace State

### 1.1 sonic-buildimage (this repo, branch: `wedge100s`)

**Device directory:** `device/accton/x86_64-accton_wedge100s_32x-r0/`

| File | State | Notes |
|------|-------|-------|
| `Accton-WEDGE100S-32X/port_config.ini` | **DONE** | 32x100G, correct Tomahawk lane assignments |
| `Accton-WEDGE100S-32X/sai.profile` | **DONE** | References `th-wedge100s-32x100G.config.bcm` |
| `Accton-WEDGE100S-32X/th-wedge100s-32x100G.config.bcm` | Present, **UNVERIFIED** | Needs comparison vs Facebook Wedge 100 BCM config |
| `default_sku` | **DONE** | `Accton-WEDGE100S-32X t1` |
| `platform_asic` | **DONE** | `broadcom` |
| `installer.conf` | **INCOMPLETE** | Only `CONSOLE_SPEED=57600`; missing `CONSOLE_PORT`, `CONSOLE_DEV` |
| `pmon_daemon_control.json` | Placeholder | All pmon daemons skipped; correct for now |
| `plugins/eeprom.py` | **WRONG** | Path is `1-0050`; correct path is `40-0050` |
| `plugins/psuutil.py` | **WRONG** | Reads register `0x60`; correct register is `0x10` |
| `plugins/sfputil.py` | **WRONG** | Stubbed with CP2112 placeholder; actual access is standard I2C via mux tree |

**Platform directory:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/`

| File | State | Notes |
|------|-------|-------|
| `utils/accton_wedge100s_util.py` | Partially correct | Mux init is right; IDPROM should target i2c-40, not i2c-1 |
| `sonic_platform/fan.py` | **WRONG** | Uses `ipmitool`; platform has no IPMI, requires BMC TTY |
| `modules/` (kernel drivers) | Present | C kernel drivers from ONL; need build integration audit |
| `onl/` | Present | Full ONL ONLP layer; not directly used by SONiC but is authoritative reference |

### 1.2 OpenNetworkLinux (workspace: `/home/dbahi/git/OpenNetworkLinux`)

Primary reference: `packages/platforms/accton/x86-64/wedge100s-32x/`

Authoritative ONL source files (verified):
- `onlp/builds/x86_64_accton_wedge100s_32x/module/src/platform_lib.c` — BMC TTY interface
- `onlp/builds/x86_64_accton_wedge100s_32x/module/src/thermali.c` — TMP75 sensors
- `onlp/builds/x86_64_accton_wedge100s_32x/module/src/fani.c` — Fan board via BMC
- `onlp/builds/x86_64_accton_wedge100s_32x/module/src/psui.c` — PSU via CPLD + BMC PMBus
- `onlp/builds/x86_64_accton_wedge100s_32x/module/src/sfpi.c` — QSFP via I2C mux tree

---

## 2. Hardware Architecture (Verified from ONL Source)

### 2.1 CPU and BMC Split — VERIFIED on hardware (SONiC 6.1.0-29-2-amd64)

The Wedge 100S has **two processing domains**:

```
Host CPU (Intel Broadwell-DE D1508)
  ├─ i2c-0: Intel SMBus I801 (driver: i2c_i801)
  │    ├─ 0x08: RTC/Clock
  │    ├─ 0x44: Voltage monitor
  │    └─ 0x48: ADS1015 12-bit ADC
  │
  └─ USB → CP2112 HID I2C Bridge (10c4:ea90, /dev/hidraw0)
       └─ i2c-1: CP2112 SMBus Bridge  [driver: hid_cp2112 — exposes as standard i2c adapter]
            ├─ 0x32: System CPLD  [v2.6, board ID 0x65]
            └─ PCA9548 mux tree (0x70–0x74)  [driver: i2c_mux_pca954x]
                 ├─ i2c-2 to i2c-33: 32x QSFP28 EEPROMs at 0x50/0x51
                 ├─ i2c-36: PCA9535 GPIO @ 0x22 (ports 0–15 presence)
                 ├─ i2c-37: PCA9535 GPIO @ 0x23 (ports 16–31 presence)
                 └─ i2c-40: System EEPROM (24c64) @ 0x50  [TlvInfo, S/N: AI09019591]

NOTE: i2c_ismt is NOT present. The iSMT controller does not exist on this board.
      The CP2112 USB-HID bridge serves as the sole I2C master for platform management.

OpenBMC (ARM processor, separate chassis management module)
  ├─ /dev/ttyACM0 on host (57600 8N1) ← only host-side interface
  ├─ BMC i2c-3: TMP75 thermal sensors @ 0x48–0x4c
  ├─ BMC i2c-7: PSU PMBus via mux @ 0x70 (PSU1@0x59, PSU2@0x5a)
  └─ BMC i2c-8: Fan board controller @ 0x33, TMP75 @ 0x48/0x49
       └─ fan<N>_input (sysfs), fantray_present, set_fan_speed.sh
```

**Critical correction to PORTINGNOTES.md:**
- PORTINGNOTES.md incorrectly claims NO PCA9548 muxes and that QSFP access requires CP2112 USB-HID. Both are wrong. QSFP modules are accessed via standard I2C through the PCA9548 mux tree on the host CPU.
- PORTINGNOTES.md's "CPLD-based thermal/fan" is also wrong for this platform. Thermal and fan subsystems live on the BMC, accessed via BMC TTY.
- There is no IPMI. BMC communication is exclusively via TTY serial (OpenBMC console).

### 2.2 I2C Mux Tree (Host CPU)

5x PCA9548 8-channel muxes on i2c-1:

| Mux Address | Creates Buses | Primary Use |
|-------------|---------------|-------------|
| 0x70 | i2c-2 to i2c-9 | QSFP ports 0–7 |
| 0x71 | i2c-10 to i2c-17 | QSFP ports 8–15 |
| 0x72 | i2c-18 to i2c-25 | QSFP ports 16–23 |
| 0x73 | i2c-26 to i2c-33 | QSFP ports 24–31 |
| 0x74 | i2c-34 to i2c-41 | SFP GPIO expanders, IDPROM |

QSFP port-to-bus mapping (from `sfpi.c`, zero-indexed ports):
```
Port 0→bus3,  1→bus2,  2→bus5,  3→bus4,  4→bus7,  5→bus6,  6→bus9,  7→bus8,
Port 8→bus11, 9→bus10, 10→bus13,11→bus12,12→bus15,13→bus14,14→bus17,15→bus16,
Port16→bus19,17→bus18,18→bus21,19→bus20,20→bus23,21→bus22,22→bus25,23→bus24,
Port24→bus27,25→bus26,26→bus29,27→bus28,28→bus31,29→bus30,30→bus33,31→bus32
```

### 2.3 Confirmed Device Map

| Subsystem | Bus | Address | Register/Notes |
|-----------|-----|---------|----------------|
| System CPLD | i2c-1 (host) | 0x32 | PSU presence/status at reg 0x10 |
| PSU1 present | i2c-1/0x32 | — | reg 0x10, bit 0 (0=present) |
| PSU1 power good | i2c-1/0x32 | — | reg 0x10, bit 1 |
| PSU2 present | i2c-1/0x32 | — | reg 0x10, bit 4 (0=present) |
| PSU2 power good | i2c-1/0x32 | — | reg 0x10, bit 5 |
| System EEPROM | i2c-40 (host) | 0x50 | 24c64, ONIE TLV format |
| QSFP EEPROM | i2c-2 to i2c-33 (host) | 0x50 | Mapped per sfp_bus_index[] |
| QSFP DOM | i2c-2 to i2c-33 (host) | 0x51 | — |
| SFP presence 0–15 | i2c-36 (host) | 0x22 | PCA9535; offsets 0 (p0–7), 1 (p8–15) |
| SFP presence 16–31 | i2c-37 (host) | 0x23 | PCA9535; offsets 0 (p16–23), 1 (p24–31) |
| Thermal 1–5 (MB) | BMC i2c-3 | 0x48–0x4c | TMP75, via BMC TTY cat sysfs |
| Thermal 6–7 (MB) | BMC i2c-8 | 0x48–0x49 | TMP75, via BMC TTY cat sysfs |
| Fan board | BMC i2c-8 | 0x33 | fantray_present, fan<N>_input sysfs |
| Fan control | BMC | — | `set_fan_speed.sh <pct>` via TTY |
| PSU1 PMBus | BMC i2c-7 via mux@0x70 | 0x59 | VIN/IIN/IOUT/POUT/model |
| PSU2 PMBus | BMC i2c-7 via mux@0x70 | 0x5a | VIN/IIN/IOUT/POUT/model |

### 2.4 BMC TTY Interface

All BMC-resident subsystem access (thermals, fans, PSU telemetry) follows this pattern:
- Open `/dev/ttyACM0` at 57600 baud, 8N1
- Login: username `root`, password `0penBmc`
- Send shell commands; read response terminated by `@bmc:` prompt
- Timeout: ~60s per I2C command

This is inherently slow and serialized. SONiC pmon daemons polling at their default rates will overwhelm the TTY. Fan control and thermal policy must be implemented with appropriate poll intervals.

---

## 3. ONL vs SONiC Platform Tooling

### 3.1 ONL (OpenNetworkLinux) Approach

ONL uses the **ONLP (ONL Platform)** library: a C API abstraction layer consumed by `onlpd` and CLI tools (`onlpdump`).

- Platform code: C shared library (`libonlp-platform.so`)
- Hardware access: direct function calls to ONLP subsystem implementations
- BMC comms: blocking TTY I/O in C (platform_lib.c pattern)
- No daemon separation—monolithic `onlpd` polls all subsystems
- Port config: static C arrays in sfpi.c
- No SAI/ASIC integration in ONLP layer

### 3.2 SONiC Approach

SONiC uses a layered daemon + Python platform API:

```
SONiC CLI / APP layer
       │
    Redis DB
       │
   pmon daemons (xcvrd, psud, thermalctld, ledd, syseepromd)
       │
   Platform API (Python)
   ├─ Legacy:  device/*/plugins/{sfputil,psuutil,eeprom}.py
   └─ Modern:  platform/*/sonic_platform/{chassis,fan,thermal,sfp,psu,eeprom}.py
       │
   Kernel drivers / sysfs / direct I2C (i2c-dev)
```

Key differences:
- Each subsystem is a separate daemon with its own poll cycle
- Hardware access is primarily via sysfs, i2c-dev (`/dev/i2c-N`), or subprocess
- SAI/BCM SDK integration is separate from platform management
- Two API tiers: legacy `plugins/` (simpler) and modern `sonic_platform/` (full featured)
- Build system integration via `setup.py` and platform wheel

### 3.3 Translation Strategy

| ONL Component | SONiC Equivalent |
|---------------|-----------------|
| `platform_lib.c` BMC TTY | Python BMC TTY helper class |
| `thermali.c` | `sonic_platform/thermal.py` |
| `fani.c` | `sonic_platform/fan.py` |
| `psui.c` (presence) | `plugins/psuutil.py` + `sonic_platform/psu.py` |
| `sfpi.c` (eeprom) | `plugins/sfputil.py` + `sonic_platform/sfp.py` |
| `sysi.c` EEPROM | `plugins/eeprom.py` + `sonic_platform/eeprom.py` |
| `ledi.c` | `sonic_platform/led.py` (or `ledd` control) |
| ONLP mux init | `accton_wedge100s_util.py` (already partially done) |

---

## 4. Implementation Plan

### Phase 0: I2C Topology Discovery on SONiC Kernel — **COMPLETE AND HARDWARE-VERIFIED (2026-02-25)**

**Deliverable committed:** `device/accton/x86_64-accton_wedge100s_32x-r0/i2c_bus_map.json`

**Key findings from hardware discovery (SONiC kernel 6.1.0-29-2-amd64, hare-lorax):**
- **No iSMT controller present.** The Wedge 100S uses a CP2112 USB HID I2C bridge
  (`10c4:ea90`, driver `hid_cp2112`) — NOT iSMT. The original discovery procedure
  below is preserved for methodology reference; Steps 1–4 should use `hid_cp2112`
  and look for the `CP2112` adapter, not iSMT.
- **Bus numbers match ONL exactly** on SONiC 6.1.0 (same probe order):
  i2c-1 = CP2112, i2c-2..i2c-41 = PCA9548 mux channels,
  i2c-36 = PCA9535 (ports 0–15), i2c-37 = PCA9535 (ports 16–31), i2c-40 = IDPROM
- **0c logic bugs** all fixed: eeprom.py path (40-0050), psuutil.py register (0x10),
  sfputil.py access method (mux tree), fan.py (BMC TTY), installer.conf (CONSOLE_SPEED=57600)

---

The hardware map in Section 2 was derived from ONL source code running on ONL's kernel. SONiC runs a different kernel. Linux I2C bus numbering is assigned dynamically at driver probe time — enumeration order depends on driver load order, PCI scan order, and kernel version. Bus numbers that are `i2c-1`, `i2c-36`, `i2c-40` etc. under ONL may be completely different numbers under SONiC's kernel. Any plugin or utility that hardcodes bus numbers derived from ONL is unreliable until verified on the actual SONiC boot.

**Goal:** Produce a verified device-role-to-bus-number map for the running SONiC kernel and commit it as the authoritative reference for all subsequent phases.

#### 0a. Establish Discovery Methodology

The correct approach is role-based discovery: identify buses by the signature of devices they carry, not by assumed numbering.

**Device roles and their signatures:**

| Role | Identifying Characteristic |
|------|-----------------------------|
| Root iSMT bus | `/sys/class/i2c-adapter/i2c-N/name` contains `iSMT` or `ismt` |
| Root SMBus | name contains `SMBus` or `I801` |
| Mux channel buses | name contains `i2c-N-mux` |
| CPLD bus | Root or mux bus where address `0x32` ACKs |
| QSFP buses | Mux channel buses where address `0x50` ACKs (after module inserted) |
| IDPROM bus | Mux channel bus where `at24`-bound EEPROM at `0x50` has TlvInfo header |
| SFP presence buses | Mux channel buses where `0x22` or `0x23` (PCA9535) ACKs |

**Discovery procedure (run as root on SONiC target):**

```
Step 1: Load prerequisite modules
  modprobe i2c_dev i2c_i801 hid_cp2112 i2c_mux_pca954x at24
  NOTE: i2c_ismt is NOT present on this platform. Use hid_cp2112 for the CP2112 bridge.

Step 2: Read /sys/class/i2c-adapter/i2c-*/name for ALL buses
  → Classify each as: smbus | cp2112 | mux-channel | other
  → The CP2112 USB HID bridge (not iSMT) is the root for the mux tree
  → Look for adapter name containing "CP2112" or check /sys/bus/usb/devices/ for 10c4:ea90

Step 3: On the CP2112 root bus (i2c-1 on SONiC 6.1.0), probe 0x70–0x74 (PCA9548 mux addresses)
  → Each that ACKs: instantiate via new_device, record which buses appear

Step 4: Probe 0x32 on the CP2112 root bus
  → Record the bus number where CPLD lives

Step 5: After mux instantiation, count newly-appeared buses
  → Should see ~40 new buses (5 muxes × 8 channels)
  → Record the base bus number of the mux tree

Step 6: Probe 0x22 and 0x23 on newly created buses
  → These are PCA9535 GPIO expanders for QSFP presence
  → Record bus numbers

Step 7: Try to instantiate 24c64 at 0x50 on each mux-channel bus in turn
  → Read first 8 bytes; if starts with 'TlvInfo' → this is IDPROM bus
  → Record bus number

Step 8: Write discovered map to /etc/sonic/platform_bus_map.json
```

#### 0b. Evaluate and Extend Existing Discovery Tools

Two scripts already exist; neither is adequate alone:

- `wedge100s_i2c_discovery.py` — Has useful bus enumeration and TLV decoder, but its "summary" section has CP2112 assumptions baked in as hardcoded text. The mux topology analysis assumes specific bus numbers rather than deriving them. The CPLD probing calls `read_cpld_registers(1, 0x32)` — hardcodes bus 1.
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/bus_hunter.py` — Minimal; only probes a handful of addresses, no role mapping, no sysfs name inspection.

Neither script has been run on the SONiC kernel. Both should be treated as drafts.

**Required capability in revised tooling:**

1. Read `/sys/class/i2c-adapter/i2c-*/name` to classify all present buses before any probing
2. Dynamically locate the iSMT root bus (not assume it is i2c-1)
3. Instantiate muxes and record which bus numbers appear afterward (delta method)
4. Probe for CPLD, IDPROM, PCA9535 presence GPIOs by address signature
5. Output a structured map: `{ "cpld_bus": N, "idprom_bus": N, "sfp_presence_buses": [N, N], "qsfp_bus_map": [port0→N, ...] }`
6. This map file is consumed by all subsequent platform code; no bus numbers are hardcoded in plugin or platform files

#### 0c. Logic Bugs to Fix (After Bus Numbers Are Confirmed)

These have wrong logic independent of bus numbering — fix after discovery provides confirmed numbers:

1. **`plugins/eeprom.py`** — Path `1-0050` is wrong. Correct format is `<bus>-0050` where `<bus>` is the IDPROM bus from discovery. Do not hardcode until discovery confirms.
2. **`plugins/psuutil.py`** — Reads CPLD register `0x60`; ONL `psui.c` confirms the correct register is `0x10`. Bit mask logic is also wrong (see Section 2.3). Bus number for CPLD must come from discovery.
3. **`plugins/sfputil.py`** — CP2112 stub is wrong approach entirely; replace with I2C mux tree via sysfs EEPROM files. Port-to-bus mapping must use the map produced in 0b, not ONL's `sfp_bus_index[]` directly.
4. **`sonic_platform/fan.py`** — Uses `ipmitool`; platform has no IPMI. Replace with BMC TTY (Phase 2). This is a logic error independent of bus discovery.
5. **`installer.conf`** — `CONSOLE_PORT` and `CONSOLE_DEV` are missing. Values must be confirmed from GRUB config on the actual hardware, not assumed.

#### 0d. Deliverable

A committed file `device/accton/x86_64-accton_wedge100s_32x-r0/i2c_bus_map.json` (or equivalent), populated by running the Phase 0 discovery tooling on the SONiC target, containing the authoritative bus numbers for this kernel. All platform code in Phases 1–9 references this file; no I2C bus numbers are hardcoded anywhere else.

### Phase 1: Platform Init Service — **COMPLETE AND HARDWARE-VERIFIED (2026-02-25)**

Goal: On boot, reliably stand up the I2C mux tree and register kernel devices.

**Verification (hare-lorax, `accton_wedge100s_util.py install`):**
- ALL 8 devices registered: 5x pca9548 (1-0070..1-0074), pca9535 (36-0022, 37-0023), 24c64 (40-0050)
- PCA9535 drivers bound (gpiochip2/3); IDPROM eeprom sysfs node present at 40-0050/eeprom
- `show` output: PSU1=present/power FAIL (no AC in lab — expected), PSU2=present/power good
- `show` output: QSFP Port 1=present (physical cable confirmed), ports 2–32=absent
- Service active and enabled (`wedge100s-platform-init.service`), runs before pmon

**Files changed:**
- `utils/accton_wedge100s_util.py` — complete rewrite: kos list, mknod list, install/clean/show/sff/set
- `utils/README` — rewritten (was a verbatim as7712 copy with wrong hardware facts)
- `service/wedge100s-platform-init.service` — verified correct, no changes needed
- `plugins/eeprom.py`, `plugins/psuutil.py`, `plugins/sfputil.py`, `plugins/fan.py` — Phase 0c fixes applied
- `installer.conf` — CONSOLE_SPEED=57600 confirmed; CONSOLE_PORT/CONSOLE_DEV still incomplete (low priority)
- `device/.../i2c_bus_map.json` — committed as Phase 0 deliverable

Tasks:
- Audit/complete `accton_wedge100s_util.py` install path: ✓ DONE
  - Load: `i2c_dev`, `i2c_i801`, `hid_cp2112`, `i2c_mux_pca954x`, `at24`
  - Do NOT load `i2c_ismt` — no iSMT controller present
  - Do NOT load `lm75` — thermal sensors are on BMC I2C bus, not host
  - Register 5x pca9548 on i2c-1 (in address order 0x70→0x74 to preserve bus numbering)
  - Register 24c64 EEPROM at i2c-40/0x50
  - Register PCA9535 presence GPIOs at i2c-36/0x22 and i2c-37/0x23
- Wire up `wedge100s-platform-init.service` systemd unit: ✓ DONE
- Ensure service runs before `pmon` starts: ✓ DONE (`Before=pmon.service`)

### Phase 2: BMC TTY Helper — **COMPLETE AND HARDWARE-VERIFIED (2026-02-25)**

Goal: Python class replicating `platform_lib.c` behavior.

**Verification (hare-lorax):**
- `send_command('echo hello_bmc')` → correct echo + response ✓
- `file_read_int('/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input')` → 23750 ✓
- All 7 BMC thermal sensors readable; all 10 fan RPM inputs readable ✓
- `fantray_present` returns 0 (all present) with `base=0` ✓

**Three deviations from `platform_lib.c` required by this hardware:**

| Issue | C code assumption | Hardware reality | Fix in bmc.py |
|---|---|---|---|
| `O_NONBLOCK` + `select()` | C keeps `O_NDELAY`; assumed same works in Python | `ttyACM` (USB CDC) does not signal `select()` readiness under `O_NONBLOCK` on this kernel | Blocking mode + `VMIN=1` |
| Login prompt string | `"@bmc:"` (matches `root@bmc:~#`) | Actual: `root@hare-lorax-bmc:~# ` — `@bmc:` is not a substring | `_TTY_PROMPT = b':~# '` |
| Login detection | `"bmc login:"` | Actual: `hare-lorax-bmc.sesame.lab login:` | Check `b' login:'` |

**Additional hardware findings recorded for downstream phases:**
- Thermal paths: `devices/<bus>/hwmon/*/temp1_input` (NOT `drivers/lm75/<bus>/temp1_input` which is wrong on OpenBMC)
- Fan RPMs: `/sys/bus/i2c/devices/8-0033/fan1..10_input`; `fantray_present` returns hex
- PSU1 @ BMC i2c-7/0x59 confirmed ACKing via i2cdetect

Tasks:
- `sonic_platform/bmc.py`: TTY open, login, command send, response parse: ✓ DONE
- `bmc_file_read_int(path)` and `bmc_i2cget(bus, addr, reg)` equivalents: ✓ DONE
- Login state, prompt detection, timeout, retry: ✓ DONE
- Threading lock for within-process serialisation: ✓ DONE

### Phase 3: Thermal Implementation — **COMPLETE AND HARDWARE-VERIFIED (2026-02-25)**

Source: `thermali.c`

Tasks:
- `sonic_platform/thermal.py`: ✓ DONE
  - CPU core: glob `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input`, report max across all cores
    - NOTE: ARCHSPEC previously had the wrong path (missing `hwmon/hwmon*` intermediate). Corrected.
    - 3 temp inputs confirmed on hardware (temp1/2/3), all reading 46–47 °C
  - TMP75-1 to TMP75-5: BMC TTY `cat /sys/bus/i2c/devices/3-0048..4c/hwmon/*/temp1_input`
  - TMP75-6, TMP75-7: BMC TTY `cat /sys/bus/i2c/devices/8-0048..9/hwmon/*/temp1_input`
    - NOTE: `drivers/lm75/` path (from thermali.c) is WRONG on OpenBMC. Use `devices/<dev>/hwmon/*/` (hwmon wildcard required — hwmonN not fixed). Already documented in MEMORY.md.
  - 8 sensors total; CPU thresholds 95/102 °C, TMP75 thresholds 70/80 °C
- `sonic_platform/chassis.py`: ✓ DONE — minimal stub, populates `_thermal_list` only
- `sonic_platform/platform.py`: ✓ DONE — `Platform → Chassis`, satisfies `from sonic_platform.platform import Platform`
- `sonic_platform/__init__.py`: ✓ DONE — updated to `from .platform import Platform` (was importing from orphaned `plat.py`)
- `pmon_daemon_control.json`: ✓ DONE — `skip_thermalctld: false`

**pmon.sh fix required (Phase 10 build integration):**
`/usr/bin/pmon.sh` does not pass `/dev/ttyACM0` into the pmon Docker container by default.
Without this fix all 7 BMC sensors time out (10 retries × 20 open-retries × 0.1 s ≈ 142 s per
poll cycle, matching the syslog warning seen on first boot). Fixed for development by inserting
after the `ipmi0` device line in `docker create`:
```
$(if [ -e "/dev/ttyACM0" ]; then echo "--device=/dev/ttyACM0:/dev/ttyACM0"; fi) \
```
This change is already applied on hare-lorax. It must be incorporated into the build system in
Phase 10. The conditional form is harmless on platforms without ttyACM0.

### Phase 4: Fan Implementation

Source: `fani.c`

Tasks:
- `sonic_platform/fan.py`:
  - Presence: BMC TTY cat `/sys/bus/i2c/devices/8-0033/fantray_present`
  - RPM: BMC TTY cat `/sys/bus/i2c/devices/8-0033/fan<N>_input` (front: odd N, rear: even N)
  - Speed set: BMC TTY `set_fan_speed.sh <pct>`
  - Max RPM: 15400
  - Direction: F2B (fixed per ONL)
- Enable `thermalctld` fan control

### Phase 5: PSU Implementation

Source: `psui.c`

Tasks:
- `plugins/psuutil.py` (fix immediately in Phase 0):
  - Presence: `i2cget -y 1 0x32 0x10`, check bits (PSU1: bit 0, PSU2: bit 4; 0=present)
  - Power good: same reg, PSU1: bit 1, PSU2: bit 5
- `sonic_platform/psu.py` (for full pmon support):
  - All above plus PMBus telemetry via BMC TTY
  - PSU1 mux value 0x02 → addr 0x59; PSU2 mux value 0x01 → addr 0x5a on BMC i2c-7
  - Read VIN(0x88), IIN(0x89), IOUT(0x8c), POUT(0x96), model(0x9a)
- Enable `psud` in `pmon_daemon_control.json`

### Phase 6: QSFP/SFP Implementation

Source: `sfpi.c`

Tasks:
- `plugins/sfputil.py` (replace in Phase 0):
  - `port_to_eeprom_mapping`: map port N to `/sys/class/i2c-adapter/i2c-<bus>/` using `sfp_bus_index[]`
  - Presence: read PCA9535 via `i2cget -y 36 0x22 <offset>` and `i2cget -y 37 0x23 <offset>`
  - Apply bit-swap per `onlp_sfpi_reg_val_to_port_sequence()` in sfpi.c (alternating even/odd bits)
- `sonic_platform/sfp.py` (full xcvrd support):
  - Instantiate QSFP `at24` device on first access if not present
  - Read 256 bytes from sysfs EEPROM file
  - DOM access at 0x51
- Enable `xcvrd` in `pmon_daemon_control.json`

### Phase 7: System EEPROM

Tasks:
- `plugins/eeprom.py`: Fix path to `/sys/bus/i2c/devices/40-0050/eeprom`
- Ensure `accton_wedge100s_util.py` registers `24c64 0x50` on i2c-40 at init
- Enable `syseepromd`

### Phase 8: BCM Config Verification

Tasks:
- Compare `th-wedge100s-32x100G.config.bcm` against `device/facebook/x86_64-facebook_wedge100-r0/Facebook-W100-C32/th-wedge100-32x100G.config.bcm`
- Both platforms: BCM56960 Tomahawk, 32x100G; config should be very similar
- Verify port lane assignment in BCM config matches `port_config.ini`
- Check `sai.profile` for correct SAI profile selection

### Phase 9: LED Control

Source: `ledi.c`

Tasks:
- Determine if SONiC `ledd` daemon writes are sufficient or need custom CPLD LED map
- ONL uses CPLD reg 0x3e (LED_SYS1) and 0x3f (LED_SYS2) on i2c-1/0x32
- Color encoding: 0=off, 1=red, 2=green, 4=blue; +8=blinking

### Phase 10: Build Integration Audit

Tasks:
- Verify `setup.py` packages correct modules
- Check kernel module build (`modules/Makefile`) compiles against target kernel
- Ensure `sonic-platform-modules-accton` is referenced in platform Makefile chain
- Confirm `pmon_daemon_control.json` correctly enables daemons as each phase completes

---

## 5. Known Risks and Open Questions

| Issue | Risk | Action |
|-------|------|--------|
| BMC TTY contention | High | Only one process can own TTY; need serialization in `bmc.py` |
| BMC login state | Medium | BMC may be in use by other processes; TTY singleton must handle |
| `set_fan_speed.sh` existence on BMC | Medium | Verify script exists on OpenBMC image on hardware |
| BCM config accuracy | High | Lane mapping wrong = no ports; must verify on hardware |
| PCA9535 bit-swap logic | Medium | ONL's `onlp_sfpi_reg_val_to_port_sequence()` swaps even/odd bits; must replicate exactly |
| QSFP i2c instantiation | Low | Kernel may need `new_device` write before sysfs EEPROM is accessible |
| Console port number | Low | `installer.conf` needs correct ttyS assignment; verify from GRUB on hardware |
| Fan direction per tray | Low | ONL hardcodes F2B; verify no mixed airflow trays in test units |

---

## 6. File Inventory by Status

### Must Fix (Bugs)
- [plugins/eeprom.py](device/accton/x86_64-accton_wedge100s_32x-r0/plugins/eeprom.py) — wrong EEPROM path
- [plugins/psuutil.py](device/accton/x86_64-accton_wedge100s_32x-r0/plugins/psuutil.py) — wrong CPLD register
- [plugins/sfputil.py](device/accton/x86_64-accton_wedge100s_32x-r0/plugins/sfputil.py) — wrong access method
- [sonic_platform/fan.py](platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/fan.py) — uses ipmitool

### Must Create
- `sonic_platform/bmc.py` — BMC TTY helper
- `sonic_platform/thermal.py` — 8 sensor implementation
- `sonic_platform/psu.py` — full PSU with PMBus
- `sonic_platform/sfp.py` — QSFP with mux tree
- `sonic_platform/chassis.py` — chassis container
- `sonic_platform/eeprom.py` — ONIE TLV at i2c-40/0x50
- `sonic_platform/__init__.py` — already present
- `sonic_platform/platform.py` — already present (verify content)

### Must Complete
- [utils/accton_wedge100s_util.py](platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py) — IDPROM path, PCA9535 registration
- [installer.conf](device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf) — add console port/dev

### Must Verify
- `Accton-WEDGE100S-32X/th-wedge100s-32x100G.config.bcm` — BCM lane mapping
- `service/wedge100s-platform-init.service` — systemd unit for init script
- `modules/Makefile` — kernel module build chain

---

## 7. Reference Sources

1. ONL sfpi.c (SFP bus map, presence logic): `OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/onlp/builds/x86_64_accton_wedge100s_32x/module/src/sfpi.c`
2. ONL platform_lib.c (BMC TTY pattern): same path, `platform_lib.c`
3. ONL fani.c / thermali.c / psui.c: same path
4. Facebook Wedge 100 SONiC config (BCM reference): `device/facebook/x86_64-facebook_wedge100-r0/`
5. Accton AS7712 (similar Tomahawk, fuller SONiC implementation): `device/accton/x86_64-accton_as7712_32x-r0/`
6. SONiC Platform API spec: https://github.com/sonic-net/SONiC/blob/master/doc/platform_api/new_platform_api.md
7. SONiC Porting Guide: https://github.com/sonic-net/SONiC/wiki/Porting-Guide
