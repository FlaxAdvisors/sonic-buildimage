# Accton Wedge 100S-32X — SONiC Platform Architecture

**Hardware:** Accton Wedge 100S-32X (Facebook Wedge 100S), Broadcom Tomahawk ASIC,
Intel Broadwell-DE D1508 host CPU, OpenBMC on a separate ARM SoC.

---

## 1. Design Principle — Why the Kernel I2C Stack Is Bypassed

The CP2112 USB-HID bridge exposes the QSFP mux tree to the Linux kernel via
`hid_cp2112`, which creates virtual I2C buses i2c-2 through i2c-41.  Binding
standard drivers (`i2c_mux_pca954x`, `optoe`, `at24`) against those buses
causes a **probe-write attack surface**: the kernel's mux driver issues a
write to the PCA9548 mux address (0x70-0x73) before each bus transaction.
When two entities (the daemon and the kernel) compete for the same CP2112
channel simultaneously, mux-channel state is corrupted and QSFP EEPROM reads
return garbage or zeroed data.  In extreme cases (observed during live
migration), the kernel hangs while waiting for an I2C adapter removal
(`i2c_del_adapter → wait_for_completion`), leading to a kernel panic.

The solution (Phase 2, verified 2026-03-14) is to load **only** the four
modules that do not touch the mux tree from kernel context, and give a
single compiled C daemon exclusive ownership of `/dev/hidraw0` (the CP2112
raw HID interface).  Python platform code reads only files written by that
daemon — it never initiates I2C transactions in steady-state operation.

This design mirrors Arista EOS on the same hardware, where a single privileged
process owns the CP2112 and all others read cached state.

---

## 2. Kernel Layer

### Modules loaded at platform init

`accton_wedge100s_util.py install` (via `wedge100s-platform-init.service`) runs
four `modprobe` calls:

| Module | Purpose |
|---|---|
| `i2c_dev` | Exposes `/dev/i2c-N` character devices (used by daemon fallback path) |
| `i2c_i801` | Intel PCH SMBus controller (`/dev/i2c-0`); LPC/CPLD alternative path |
| `hid_cp2112` | CP2112 USB-HID bridge; creates `/dev/i2c-1` (CPLD only) and `/dev/hidraw0` |
| `wedge100s_cpld` | Custom CPLD driver; bound to 1-0032 after `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device` |

### Intentionally NOT loaded

| Module | Reason not loaded |
|---|---|
| `i2c_mux_pca954x` | Would create buses i2c-2..41 and compete with daemon for mux-channel state |
| `optoe` | No virtual QSFP buses exist; EEPROM served from daemon cache |
| `at24` | System EEPROM served from daemon cache |
| `lm75` | TMP75 sensors are on the BMC I2C bus, not the host; accessed via TTY |
| `i2c_ismt` | Not present on this platform |
| `gpio_pca953x` | PCA9535 presence chips owned by the daemon via hidraw |

### wedge100s_cpld sysfs interface

Path: `/sys/bus/i2c/devices/1-0032/`

| Attribute | Content |
|---|---|
| `cpld_version` | CPLD firmware version string |
| `psu1_present` | 1 = PSU1 physically present (driver inverts active-low bit 0 of reg 0x10) |
| `psu1_pgood` | 1 = PSU1 power good (bit 1, active-high) |
| `psu2_present` | 1 = PSU2 physically present (driver inverts active-low bit 4 of reg 0x10) |
| `psu2_pgood` | 1 = PSU2 power good (bit 5, active-high) |
| `led_sys1` | System status LED: write 0=off, 1=red, 2=green |

### I2C bus map (Phase 2)

```
/dev/i2c-0   — Intel i801 SMBus (LPC/CPLD alternate path)
/dev/i2c-1   — CP2112 USB-HID bridge (CPLD at 1-0032 only)
/dev/hidraw0 — CP2112 raw HID (daemon owns entire mux tree)
```

Buses i2c-2 through i2c-41 do **not** exist; `i2c_mux_pca954x` is not loaded.

---

## 3. hidraw Layer — CP2112 Mux Tree

`/dev/hidraw0` is the raw AN495 HID interface to the CP2112 USB bridge.
The daemon opens it exclusively on each invocation.  The PCA9548 mux tree
behind the CP2112 is:

```
CP2112 (i2c-1)
  PCA9548 @ 0x70  ch0-7  → buses  2-9   (QSFP ports 0-7 EEPROMs)
  PCA9548 @ 0x71  ch0-7  → buses 10-17  (QSFP ports 8-15 EEPROMs)
  PCA9548 @ 0x72  ch0-7  → buses 18-25  (QSFP ports 16-23 EEPROMs)
  PCA9548 @ 0x73  ch0-7  → buses 26-33  (QSFP ports 24-31 EEPROMs)
  PCA9548 @ 0x74  ch2    → PCA9535 @ 0x22 (ports  0-15 presence)
             ch3    → PCA9535 @ 0x23 (ports 16-31 presence)
             ch6    → 24c64  @ 0x50  (system EEPROM, 8 KiB)
```

The daemon serializes all HID report exchanges.  No kernel driver or Python
code writes to the mux while the daemon is running.  CPLD accesses (address
0x32, no mux involved) are safe to interleave because they do not alter mux
channel state.

---

## 4. Compiled Daemons

Both daemons are C binaries built from source in
`platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/` and
installed to `/usr/bin/`.  They are invoked by systemd timers, not as
persistent daemons — they run, write their output, and exit.

### 4.1 `wedge100s-i2c-daemon poll-presence`

Source: `utils/wedge100s-i2c-daemon.c`

**Interval:** invoked every 3 s by `wedge100s-i2c-poller.timer`
(`OnBootSec=5s`, `OnUnitActiveSec=3s`).

**Runtime path selection (at each invocation):**
- **Phase 2 (normal):** opens `/dev/hidraw0`; all mux-tree I2C goes through
  raw CP2112 HID reports (AN495 protocol).
- **Phase 1 (fallback):** `/dev/hidraw0` unavailable; uses `i2c-dev` ioctl on
  buses 36/37 for PCA9535 and sysfs for EEPROM.

**Actions per invocation:**

1. Creates `/run/wedge100s/` if absent.
2. **System EEPROM** (once at first boot, skipped if cache exists):
   selects PCA9548 0x74 ch6, reads 24c64 @ 0x50 in 512-byte chunks
   (2-byte 16-bit addressing), validates `TlvInfo\x00` magic, writes
   `/run/wedge100s/syseeprom` (8192 bytes binary).
3. **QSFP presence:**
   reads PCA9535 @ 0x22 (INPUT0+INPUT1) via mux 0x74 ch2 (ports 0-15)
   and PCA9535 @ 0x23 via mux 0x74 ch3 (ports 16-31).
   Decodes XOR-1 interleave (ONL `sfpi.c`: line = `(port % 16) ^ 1`) and
   active-low polarity to produce 32 binary presence bits.
4. **Per-port logic:**
   - Absent: deletes `sfp_N_eeprom`, writes `sfp_N_present="0"`.
   - Stable present (cache exists, SFF-8024 identifier byte 0x01-0x7f valid):
     rewrites `sfp_N_present="1"`, skips EEPROM I2C.
   - Insertion or invalid cached identifier: selects per-port mux channel,
     reads 128 bytes (lower page, addr 0x00) + 128 bytes (upper page 0,
     addr 0x80) from optoe @ 0x50; writes `sfp_N_eeprom` only if identifier
     byte is valid (0x01-0x7f). Invalid reads are retried next tick.
   - Removal: `sfp_N_eeprom` is deleted immediately (stale data not served).

**Output files in `/run/wedge100s/`:**

| File | Content | When written |
|---|---|---|
| `syseeprom` | 8192 bytes binary (ONIE TlvInfo) | Once at first boot |
| `sfp_N_present` (N=0..31) | ASCII "0" or "1" | Every 3 s |
| `sfp_N_eeprom` (N=0..31) | 256 bytes binary (page 0) | On insertion; deleted on removal |

### 4.2 `wedge100s-bmc-daemon`

Source: `utils/wedge100s-bmc-daemon.c`

**Interval:** invoked every 10 s by `wedge100s-bmc-poller.timer`
(`OnBootSec=15`, `OnUnitActiveSec=10`).

**Transport:** opens `/dev/ttyACM0` at 57600 8N1; keeps the session open for
all commands in one invocation (avoids the ~65 s per-cycle overhead of
re-opening and re-logging in per command).  Prompt pattern `:~# ` matches
any OpenBMC root shell hostname.

**Commands issued per invocation:**

| Source | Command | Output file |
|---|---|---|
| BMC sysfs (i2c-3) | `cat /sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input` | `thermal_1` |
| BMC sysfs (i2c-3) | `cat /sys/bus/i2c/devices/3-0049/hwmon/*/temp1_input` | `thermal_2` |
| BMC sysfs (i2c-3) | `cat /sys/bus/i2c/devices/3-004a/hwmon/*/temp1_input` | `thermal_3` |
| BMC sysfs (i2c-3) | `cat /sys/bus/i2c/devices/3-004b/hwmon/*/temp1_input` | `thermal_4` |
| BMC sysfs (i2c-3) | `cat /sys/bus/i2c/devices/3-004c/hwmon/*/temp1_input` | `thermal_5` |
| BMC sysfs (i2c-8) | `cat /sys/bus/i2c/devices/8-0048/hwmon/*/temp1_input` | `thermal_6` |
| BMC sysfs (i2c-8) | `cat /sys/bus/i2c/devices/8-0049/hwmon/*/temp1_input` | `thermal_7` |
| BMC sysfs (i2c-8/0x33) | `cat .../8-0033/fantray_present` | `fan_present` |
| BMC sysfs (i2c-8/0x33) | `cat .../8-0033/fan{1,3,5,7,9}_input` | `fan_{1-5}_front` |
| BMC sysfs (i2c-8/0x33) | `cat .../8-0033/fan{2,4,6,8,10}_input` | `fan_{1-5}_rear` |
| BMC i2cset mux | `i2cset -f -y 7 0x70 0x02` (PSU1 select) | — |
| BMC i2cget PMBus | `i2cget -f -y 7 0x59 0x88 w` (READ_VIN) | `psu_1_vin` |
| BMC i2cget PMBus | `i2cget -f -y 7 0x59 0x89 w` (READ_IIN) | `psu_1_iin` |
| BMC i2cget PMBus | `i2cget -f -y 7 0x59 0x8c w` (READ_IOUT) | `psu_1_iout` |
| BMC i2cget PMBus | `i2cget -f -y 7 0x59 0x96 w` (READ_POUT) | `psu_1_pout` |
| BMC i2cset mux | `i2cset -f -y 7 0x70 0x01` (PSU2 select) | — |
| BMC i2cget PMBus | (same four regs, addr 0x5a) | `psu_2_{vin,iin,iout,pout}` |

All values are stored as plain decimal integers.  Thermal values are in
millidegrees C.  PSU PMBus values are raw LINEAR11 16-bit words; Python decodes
them to SI units.

---

## 5. Python Platform API (`sonic_platform/`)

Installed to `/usr/lib/python3/dist-packages/sonic_platform/`.
All modules read `/run/wedge100s/` files produced by the daemons;
none of them initiate I2C transactions in steady-state operation.

### `chassis.py`

- Constructs 8 `Thermal`, 5 `FanDrawer`, 2 `Psu`, 32 `Sfp`, and one
  `SysEeprom` object.
- `_bulk_read_presence()`: primary path reads `/run/wedge100s/sfp_N_present`
  for N=0..31; fallback (first ~5 s of boot) uses `platform_smbus.read_byte()`
  on PCA9535 buses 36/37.
- `get_change_event()`: calls `_bulk_read_presence()` in a loop (sleeps 3 s
  between polls to match daemon rate); returns xcvrd-style `{'sfp': {idx: '0'|'1'}}`.
- `set_status_led()` / `get_status_led()`: reads/writes
  `/sys/bus/i2c/devices/1-0032/led_sys1` (CPLD sysfs).

### `eeprom.py`

- Primary: `/run/wedge100s/syseeprom` (8 KiB binary, ONIE TlvInfo).
  Validates `TlvInfo\x00` magic before returning data.
- Fallback: `/sys/bus/i2c/devices/40-0050/eeprom` (sysfs, Phase 1 / first boot).
- `get_eeprom()` decodes TLV entries to a `dict` keyed by hex type code
  (e.g., `"0x21"` → product name `"WEDGE100S12V"`).  Result is in-process
  cached after first successful decode.

### `sfp.py`

- `get_presence()`: reads `/run/wedge100s/sfp_N_present`; checks mtime against
  `_PRESENCE_MAX_AGE_S = 8` s.  If stale, falls back to `platform_smbus` on
  PCA9535 with XOR-1 interleave decoding.
- `read_eeprom(offset, num_bytes)`: reads `/run/wedge100s/sfp_N_eeprom` (256 bytes,
  page 0); falls back to sysfs `SfpOptoeBase.read_eeprom()` under `_eeprom_bus_lock`
  if cache is absent and port is not known absent.
- `get_eeprom_path()`: returns the daemon cache path if file exists; otherwise
  the sysfs path `/sys/bus/i2c/devices/i2c-{bus}/{bus}-0050/eeprom` as a
  predictable non-None fallback for xcvrd.
- LP_MODE and RESET pins are not accessible from the host CPU on this platform;
  `get_lpmode()`, `set_lpmode()`, `get_reset_status()`, and `reset()` all return
  `False`.

### `thermal.py`

- Index 0 (`CPU Core`): globs
  `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input` on the host;
  returns the maximum reading across all matched files (millidegrees → °C).
- Indices 1-7 (`TMP75-1` through `TMP75-7`): reads `/run/wedge100s/thermal_N`
  (millidegrees integer written by bmc-daemon); divides by 1000.0 to get °C.
- High threshold: 70.0 °C; high critical threshold: 80.0 °C (all TMP75).
- CPU Core thresholds: 95.0 °C / 102.0 °C.

### `fan.py`

- `_cached_fantray_present()`: reads `/run/wedge100s/fan_present` (decimal
  bitmask; bit N set = tray N+1 absent; 0x00 = all present).  Cached 2 s.
- `_cached_rpm_pair(fan_index)`: reads `/run/wedge100s/fan_N_front` and
  `/run/wedge100s/fan_N_rear`.  Cached 2 s per tray.
- `get_speed_rpm()`: returns `min(front_rpm, rear_rpm)` per ONL `fani.c` policy.
- `set_speed(pct)`: sends `set_fan_speed.sh <pct>` to BMC via `bmc.send_command()`;
  invalidates RPM cache on success.  All 5 trays controlled simultaneously.
- Max RPM constant: `_MAX_FAN_SPEED = 15400` (from `fani.c`).
- Fan direction: fixed `FAN_DIRECTION_INTAKE` (front-to-back).

### `psu.py`

- `get_presence()` / `get_powergood_status()`: reads
  `/sys/bus/i2c/devices/1-0032/psu{N}_present` and `psu{N}_pgood` from CPLD sysfs.
- PMBus telemetry (`get_voltage()`, `get_current()`, `get_power()`,
  `get_input_voltage()`, `get_input_current()`): reads
  `/run/wedge100s/psu_{1,2}_{vin,iin,iout,pout}` (raw LINEAR11 decimal words);
  decodes via `_pmbus_decode_linear11()`.
  DC output voltage is computed as `POUT / IOUT` (avoids LINEAR16 VOUT_MODE
  complexity, mirrors ONL `psui.c`).  Telemetry cached 30 s.
- Rated capacity: 650 W (AC type).

### `platform_smbus.py`

Thread-safe SMBus handle pool.  Opens each `/dev/i2c-N` file descriptor once
per process lifetime and keeps it open (eliminates CP2112 USB HID setup
overhead on repeated `smbus2.SMBus()` construction).  All bus operations
serialized under a single `threading.Lock()`.  Used only in fallback paths;
not exercised in steady-state daemon-cache operation.

---

## 6. systemd Timer Units

| Unit | OnBootSec | OnUnitActiveSec | What it runs |
|---|---|---|---|
| `wedge100s-i2c-poller.timer` | 5 s | 3 s | `wedge100s-i2c-daemon poll-presence` |
| `wedge100s-bmc-poller.timer` | 15 s | 10 s | `wedge100s-bmc-daemon` |

Both timers have `AccuracySec=1` to reduce drift.  Both one-shot service units
have `LogLevelMax=notice` to suppress the high-volume Start/Finish journal
entries (the i2c timer fires ~28,800 times/day; the bmc timer ~8,640 times/day).

`wedge100s-i2c-poller.service` depends on `wedge100s-platform-init.service`
via `After=`.  `wedge100s-bmc-poller.service` depends on it via both `After=`
and `Requires=`.

---

## 7. Boot Sequence

```
systemd
  └─ wedge100s-platform-init.service  (Before=pmon.service, oneshot)
       accton_wedge100s_util.py install
         modprobe i2c_dev
         modprobe i2c_i801
         modprobe hid_cp2112
         modprobe wedge100s_cpld
         echo wedge100s_cpld 0x32 > .../i2c-1/new_device
         mkdir /run/wedge100s
       ↓ (completes ~3 s after boot)

  └─ wedge100s-i2c-poller.timer  (OnBootSec=5s)
       t=5s: wedge100s-i2c-daemon poll-presence
         → /run/wedge100s/syseeprom       (written once)
         → /run/wedge100s/sfp_N_present   (all 32 ports)
         → /run/wedge100s/sfp_N_eeprom    (inserted ports only)
       repeats every 3 s

  └─ wedge100s-bmc-poller.timer  (OnBootSec=15s)
       t=15s: wedge100s-bmc-daemon
         → /run/wedge100s/thermal_{1..7}
         → /run/wedge100s/fan_present, fan_{1-5}_{front,rear}
         → /run/wedge100s/psu_{1,2}_{vin,iin,iout,pout}
       repeats every 10 s

  └─ pmon.service  (starts after platform-init; after multi-user.target)
       loads sonic_platform package
         Chassis.__init__() builds all subsystem objects
       ↓ launches pmon daemons (xcvrd, thermalctld, psud, etc.)
```

The i2c daemon fires at t=5 s and pmon starts after `platform-init` completes
(typically t=3-4 s).  There is a brief window (~1-2 s) where pmon is running
but the i2c cache has not yet been written; the Python fallback paths cover this.
The bmc daemon fires at t=15 s, ensuring its output is present before
thermalctld's first poll cycle.

---

## 8. pmon Daemon Interactions

`pmon.service` runs inside the `pmon` Docker container.  It loads
`sonic_platform.platform.Platform()` which instantiates `Chassis`.

| pmon daemon | Python calls | Data source |
|---|---|---|
| `xcvrd` | `chassis.get_change_event()` | `/run/wedge100s/sfp_N_present` (daemon) |
| `xcvrd` | `sfp.get_presence()` | `/run/wedge100s/sfp_N_present` (daemon) |
| `xcvrd` | `sfp.read_eeprom()` / `sfp.get_eeprom_path()` | `/run/wedge100s/sfp_N_eeprom` (daemon) |
| `thermalctld` | `thermal.get_temperature()` | `/run/wedge100s/thermal_N` (bmc-daemon) or host sysfs coretemp |
| `thermalctld` | `fan.get_speed()`, `fan.get_presence()` | `/run/wedge100s/fan_*` (bmc-daemon) |
| `thermalctld` | `fan.set_speed()` | BMC SSH command `set_fan_speed.sh <pct>` |
| `psud` | `psu.get_presence()`, `psu.get_powergood_status()` | CPLD sysfs `1-0032/psu{N}_{present,pgood}` |
| `psud` | `psu.get_voltage()`, `psu.get_power()`, etc. | `/run/wedge100s/psu_N_{vin,iin,iout,pout}` (bmc-daemon) |
| `ledd` / `healthd` | `chassis.set_status_led()` | CPLD sysfs `1-0032/led_sys1` |

`xcvrd` calls `get_change_event()` in a tight loop with a timeout; the
implementation sleeps 3 s between polls (matching the daemon's write rate) to
avoid unnecessary CPU consumption.

---

## 9. Known Limitations

- **Autoneg not active at ASIC level:** The BCM56960 (Tomahawk) requires
  explicit `portctrl` configuration for autoneg on QSFP28 ports.  Current port
  configuration uses fixed-speed entries.  Link negotiation with peer devices
  running different autoneg policies may fail to come up automatically.

- **LP_MODE and RESET not accessible from host CPU:** The QSFP LP_MODE and
  RESET control pins are wired to the mux board, not to a GPIO accessible from
  the x86 host.  `set_lpmode()` and `reset()` return False on all ports.

- **PSU model/serial not readable:** PMBus `MFR_MODEL` (0x9a) and `MFR_SERIAL`
  require an SMBus block-read transaction, which is not implemented in the
  bmc-daemon.  `get_model()` and `get_serial()` return `"N/A"`.

- **Live kernel module migration is unsafe:** Transitioning from Phase 1
  (with `i2c_mux_pca954x` + `gpio_pca953x` loaded) to Phase 2 without a clean
  reboot causes a kernel hang.  Always transition via a clean boot with the
  updated `accton_wedge100s_util.py` already in place.
