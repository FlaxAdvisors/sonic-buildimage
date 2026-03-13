# Link Status Investigation — 2026-03-04

## Symptom
All 32 QSFP ports show Oper:down with N/A type in `show interface status`.
BCM shell `ps` showed 128 ports (flex config): 32 ce "down", 96 xe "!ena".
All ce ports had KR4 interface type — DAC cables require CR4.

## Root Cause (verified 2026-03-04)

### Issue 1: Forced fiber/KR4 mode in BCM config
The Wedge100S BCM config (derived from Facebook Wedge100) explicitly set:
- `serdes_fiber_pref=0x1` — forces fiber (KR4) interface type
- `phy_an_c73=0x0` — disables CL73 autoneg (needed for 100G CR4 negotiation)
- `serdes_automedium=0x0` — disables auto medium detection

The AS7712-32X (known-working TH1 32x100G platform) has **none** of these settings,
letting the SDK defaults handle both copper DAC (CR4) and optical (KR4).

With DAC cables, KR4 won't bring up the link. Since autoneg and auto-medium
are disabled, the SDK cannot discover the cable is copper and switch to CR4.

### Issue 2: sai.profile pointed to flex config
`sai.profile` loaded `th-wedge100s-32x-flex.config.bcm` (128 portmap entries).
This created 128 BCM ports when SONiC only expects 32 (per `port_config.ini`).
The non-flex `th-wedge100s-32x100G.config.bcm` creates exactly 32 ports.

## Fixes Applied

1. **Switched sai.profile** to `th-wedge100s-32x100G.config.bcm` (non-flex, 32 ports)
2. **Removed from non-flex BCM config:**
   - `phy_an_c73=0x0` (let SDK default — enables CL73 autoneg for 100G)
   - `serdes_automedium=0x0` (let SDK default — enables auto medium detection)
   - `serdes_fiber_pref=0x1` (let SDK default — copper preference)
3. **Kept:** `phy_an_c37=0x3` (CL37 autoneg for lower speeds, harmless)
4. **Kept:** All serdes_preemphasis and xgxs lane map values (board-specific PCB trace tuning)

## Config Comparison

| Setting | Facebook Wedge100 | Wedge100S (before) | Wedge100S (after) | AS7712-32X |
|---|---|---|---|---|
| phy_an_c73 | 0x0 | 0x0 | (removed) | (absent) |
| serdes_automedium | 0x0 | 0x0 | (removed) | (absent) |
| serdes_fiber_pref | 0x1 | 0x1 | (removed) | (absent) |
| serdes_preemphasis | per-port | per-port (same) | per-port (same) | per-lane |
| xgxs lane maps | per-port | per-port (same) | per-port (same) | per-port |
| serdes_driver_current | absent | absent | absent | per-lane (0x8) |

## Notes
- Facebook Wedge100 config likely designed for optics (fiber), not DAC
- The serdes preemphasis and lane maps are identical between FB Wedge100 and Wedge100S
  (same chassis/PCB design)
- `serdes_driver_current` absent in FB/Wedge100S but present in AS7712 (0x8 per lane);
  SDK defaults should be adequate for initial bring-up
- `port_breakout_config_db.json` defines 128 ports at 25G (full 4x25G breakout template);
  this is a DPB reference, not loaded at boot — not an issue
- ONL repo has no BCM config files for wedge100 platforms

## Files Changed
- `device/.../Accton-WEDGE100S-32X/sai.profile` — point to non-flex config
- `device/.../Accton-WEDGE100S-32X/th-wedge100s-32x100G.config.bcm` — remove forced fiber settings

## Deployment Verification (2026-03-04, image wedge100s.0-4f57d1dbd)

BCM config fix IS deployed. `sai.profile` → `th-wedge100s-32x100G.config.bcm` (non-flex).
BCM `ps` shows exactly 32 ce ports (correct). All ports enabled, Forward state, KR4 interface.
KR4 may be the SDK default when no link partner is connected — not necessarily the forced-fiber
issue recurring. Need a live link partner to confirm CR4 negotiation works.

---

## Issue 3: pmon container missing /dev/i2c-* devices (2026-03-04)

### Symptom
After deploying image wedge100s.0-4f57d1dbd and booting:
- `show interface transceiver presence` — all 32 ports "Not present"
- `show interface status` — all ports Type "N/A"
- `watchdog-control.service` — FAILED (`No module named 'sonic_platform'`)
- `system-health.service` — FAILED (same root cause, then `UnboundLocalError: sysmon`)
- `docker exec pmon ls /dev/i2c-*` — no i2c character devices in container
- Host `/dev/i2c-*` — also missing (0 nodes)
- Host `/sys/bus/i2c/devices/` — I2C sysfs tree was present (muxes registered)

### Root Cause: i2c_dev module not loaded

The platform init script `accton_wedge100s_util.py` has a `driver_check()` function that
gates whether to run `driver_install()` (which calls `modprobe` for all required modules).

**Bug:** `driver_check()` only tested for `hid_cp2112`:
```python
def driver_check():
    ret, _ = log_os_system("lsmod | grep -q hid_cp2112", 0)
    return ret == 0
```

At boot, `hid_cp2112` was already auto-loaded by udev (USB HID device detected), so
`driver_check()` returned True and `do_install()` skipped `driver_install()` entirely.
This meant `i2c_dev` was never loaded. Without `i2c_dev`, the kernel creates I2C adapter
entries in sysfs but does NOT create `/dev/i2c-*` character device nodes.

**Cascade of failures:**
1. No `/dev/i2c-*` on host → pmon's `get_pmon_device_mounts()` discovers zero i2c devices
2. pmon container created without any `--device=/dev/i2c-*` mappings
3. xcvrd inside pmon cannot access optoe EEPROM or PCA9535 GPIO → all transceivers "Not present"
4. `sonic_platform` wheel was shipped in device dir but never pip-installed on host → host-side
   services (`watchdog-control`, `system-health`) fail with `ModuleNotFoundError`

### Second bug: sonic_platform wheel not pip-installed on host

The wheel file `sonic_platform-1.0-py3-none-any.whl` was correctly built and placed at
`/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl`.

Inside pmon, the `docker_init.j2` script auto-installs it via pip3. But host-side services
(`watchdogutil`, `healthd`) also import `sonic_platform` and it was never installed on the host.

Other Accton platforms (AS9726, AS4630, AS7312, AS5835) handle this by calling
`do_sonic_platform_install()` from their platform util's `do_install()` function.
Our wedge100s util was missing this.

### Live Recovery Steps (verified on hardware 2026-03-04)

```bash
# 1. Load missing i2c_dev module
sudo modprobe i2c_dev
# Result: 42 /dev/i2c-* nodes appeared

# 2. Install sonic_platform wheel on host
sudo pip3 install /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl
# Result: Successfully installed sonic-platform-1.0

# 3. Restart failed host services
sudo systemctl restart watchdog-control.service   # now active (running)
sudo systemctl restart system-health.service       # now active (running)

# 4. Recreate pmon container (stop/rm/start, not just restart!)
#    systemctl stop/start alone does NOT recreate the container — it reuses the
#    existing one with the old device list. Must `docker rm` to force recreation.
sudo systemctl stop pmon
docker rm pmon
sudo systemctl start pmon
# Result: pmon recreated with 42 /dev/i2c-* devices mounted
```

After recovery:
```
show interface transceiver presence:
  Ethernet0    Present      (port 1, DAC cable — reads as "GBIC" identifier)
  Ethernet16   Present      (port 5, DAC cable — reads as "GBIC" identifier)
  Ethernet32   Present      (port 9, DAC cable — reads as "GBIC" identifier)
  Ethernet80   Present      (port 21, QSFP28 or later)
  Ethernet112  Present      (port 29, QSFP28 or later)
```

Ports still Oper down — no link partner connected (or DAC cables need autoneg partner).

### Fixes Applied to Source

1. **`accton_wedge100s_util.py` — `driver_check()` now also checks `i2c_dev`:**
   ```python
   def driver_check():
       ret, _ = log_os_system("lsmod | grep -q hid_cp2112", 0)
       if ret != 0:
           return False
       ret, _ = log_os_system("lsmod | grep -q i2c_dev", 0)
       return ret == 0
   ```

2. **`accton_wedge100s_util.py` — added `do_sonic_platform_install()` / `do_sonic_platform_clean()`:**
   Called from `do_install()` and `do_uninstall()` respectively. Uses same pattern as AS9726/AS4630:
   ```python
   pip3 install /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl
   ```

3. **`wedge100s-platform-init.service` — removed executable permission bits:**
   Was 755, now 644. Eliminates systemd warning on every boot.

### How pmon gets /dev/i2c-* devices (reference)

The mechanism is in `pmon.sh` (generated from `docker_image_ctl.j2` at build time):

```bash
get_pmon_device_mounts() {
    local devregex='i2c-[0-9]+|ipmi[0-9]+|sd[a-z]+|...'
    for device in $(find /dev/ -maxdepth 1 | grep -E "$devpathregex"); do
        echo "--device=$device"
    done
}
```

This runs at `docker create` time (NOT `docker start`). The service ordering is correct:
`wedge100s-platform-init.service` has `Before=pmon.service`, ensuring I2C topology is
established before pmon container creation discovers devices.

**Critical dependency chain:**
```
udev auto-loads hid_cp2112 (USB HID)
  → wedge100s-platform-init.service runs do_install():
      → driver_install(): modprobe i2c_dev (creates /dev/i2c-* nodes)
      → device_install(): register PCA9548 muxes → /dev/i2c-2 through i2c-41
      → do_sonic_platform_install(): pip3 install sonic_platform wheel
  → pmon.service starts:
      → pmon.sh docker create ... $(get_pmon_device_mounts) → --device=/dev/i2c-*
      → docker_init.j2: pip3 install sonic_platform wheel (inside container)
      → xcvrd: uses optoe driver via /dev/i2c-* to detect transceivers
```

### Files Changed
- `platform/.../wedge100s-32x/utils/accton_wedge100s_util.py` — driver_check + wheel install
- `platform/.../wedge100s-32x/service/wedge100s-platform-init.service` — chmod 644

### Reboot Verification (verified on hardware 2026-03-04)

After live recovery, system was rebooted. Post-reboot verification:
```
i2c_dev module:          loaded (driver_check fix worked — loaded at boot +11s)
/dev/i2c-* in pmon:      present (42 devices, docker preserved container across reboot)
Transceivers detected:   6 present (Ethernet0, 16, 32, 48, 80, 112)
Failed services:         0
sonic_platform on host:  installed (pip persisted in overlay)
```

Boot timing (from journalctl):
```
[  5.42s] wedge100s-platform-init.service started
[ 11.46s] i2c_dev loaded (by driver_install, because driver_check saw it missing)
[ 16.63s] wedge100s-platform-init.service finished (muxes registered, wheel installed)
[116.60s] pmon.service started (100s after platform-init — massive timing margin)
```

### Fresh .bin Build Path Verification

The source changes will be correctly packaged into a fresh `.bin` image:

1. **`debian/rules` (override_dh_auto_install):**
   - Line 88: `cp wedge100s-32x/utils/* → /usr/bin/` — includes updated `accton_wedge100s_util.py`
   - Line 90: `cp wedge100s-32x/service/*.service → /lib/systemd/system/` — includes fixed service file
   - `.install` file: ships wheel to device directory

2. **First boot sequence for a fresh .bin:**
   ```
   rc.local → dpkg -i platform.deb → installs util + service + wheel file
     → postinst → systemctl enable + start wedge100s-platform-init
       → do_install():
           driver_check() returns False (i2c_dev not yet loaded)
           driver_install() → modprobe i2c_dev → /dev/i2c-* nodes created
           device_install() → register PCA9548 muxes → i2c-2 through i2c-41
           do_sonic_platform_install() → pip3 install sonic_platform wheel
       → [~16s] platform init complete, all /dev/i2c-* exist
   featured → systemctl start pmon → [~116s]
     → pmon.sh → docker create $(get_pmon_device_mounts) → discovers /dev/i2c-*
     → docker_init.j2 → pip3 install wheel inside container
     → xcvrd → reads optoe/PCA9535 → transceivers detected
   ```

3. **Subsequent boots:** overlay persists — util, service, pip-installed wheel all survive.
   Docker preserves pmon container with device mappings. Platform init still runs
   (ensures modules loaded), but `driver_check()` returns True so modprobes are skipped.

---

## Issue 4: SFP presence detection converted from i2cget to GPIO sysfs (2026-03-04)

### Problem
`sfp.py` used `subprocess.check_output('i2cget -f -y ...')` to read PCA9535 GPIO
expanders for QSFP presence detection.  This caused:
1. **Bus contention** — `-f` flag bypasses kernel I2C driver locking, racing with
   EEPROM reads on the same CP2112 bus
2. **Subprocess overhead** — 32 `i2cget` process spawns per presence poll cycle
3. **Force flag** — `-f` overrides kernel driver ownership of the PCA9535 chip

### Solution
Converted to kernel GPIO sysfs interface (`/sys/class/gpio/gpioN/value`):
- PCA9535 chips already registered as kernel gpiochips by `device_install()`
- GPIO bases discovered dynamically from `/sys/class/gpio/gpiochip*/label`
  (labels: `36-0022` for ports 0-15, `37-0023` for ports 16-31)
- Port-to-GPIO mapping: `gpio = base + ((port % 16) ^ 1)` (XOR-1 corrects
  interleaved even/odd wiring per ONL sfpi.c)
- GPIOs pre-exported by `_export_presence_gpios()` in platform init
- Lazy export fallback in sfp.py for cases where init didn't run

### GPIO Chip Layout (verified on hardware 2026-03-04)
```
gpiochip596: base=596, ngpio=16, label=36-0022 (ports 0-15)
gpiochip612: base=612, ngpio=16, label=37-0023 (ports 16-31)
```

### Verification (verified on hardware 2026-03-04)
All 7 physically present modules correctly detected via GPIO sysfs:
```
Port  0 (Ethernet0):   GPIO 597 → PRESENT
Port  4 (Ethernet16):  GPIO 601 → PRESENT
Port  8 (Ethernet32):  GPIO 605 → PRESENT
Port 12 (Ethernet48):  GPIO 609 → PRESENT
Port 16 (Ethernet64):  GPIO 613 → PRESENT  (EEPROM fails CMIS validation — pre-existing)
Port 20 (Ethernet80):  GPIO 617 → PRESENT
Port 28 (Ethernet112): GPIO 625 → PRESENT
```
GPIO sysfs accessible inside pmon container via /sys bind mount.
xcvrd restart confirmed: presence detection works, EEPROM reads proceed normally
for ports with valid QSFP28 EEPROMs.

Ethernet64 note: GPIO presence detection correctly identifies the module,
but xcvrd logs "SFP EEPROM is not ready" — this is the pre-existing EEPROM
quality issue (identifier byte 0x01 = GBIC), not a presence detection problem.

### Files Changed
- `platform/.../wedge100s-32x/sonic_platform/sfp.py` — GPIO sysfs presence detection
- `platform/.../wedge100s-32x/sonic_platform/chassis.py` — updated comment
- `platform/.../wedge100s-32x/utils/accton_wedge100s_util.py` — `_export_presence_gpios()`

---

## Issue 5: 100G DAC links down — missing RS-FEC (2026-03-04)

### Setup
Four 100G DAC cables connected between hare-lorax (Wedge100S, SONiC) and
rabbit-lorax (Wedge100S, Arista EOS 4.27.0F, 192.168.88.14).

### Symptom
All ports showed KR4 interface type, autoneg=No, link=down despite physical
signal being present (BCM PHY DSC showed SD=1, CDR lock on all 4 lanes,
clean eye diagram).

### Root Cause
Arista EOS defaults to **RS-FEC** (CL91) for 100G CR4 links. Without matching
FEC on the SONiC side, the PCS layer cannot sync even though PHY signal is good.

Additionally, the BCM SDK on Tomahawk 1 defaults to **KR4** (backplane) interface
when FEC is not configured. Setting RS-FEC causes the SDK to automatically
switch to **CR4** (copper) interface — the correct type for DAC cables.

### Fix
```bash
sudo config interface fec Ethernet16 rs
sudo config interface fec Ethernet32 rs
# ... (all ports)
sudo config save -y
```

### Result (verified on hardware 2026-03-04)
```
Ethernet16  (port 5)  ↔ rabbit:Ethernet13/1  100G CR4 RS-FEC  UP
Ethernet32  (port 9)  ↔ rabbit:Ethernet14/1  100G CR4 RS-FEC  UP
Ethernet48  (port 13) ↔ rabbit:Ethernet15/1  100G CR4 RS-FEC  UP
Ethernet112 (port 29) ↔ rabbit:Ethernet16/1  100G CR4 RS-FEC  UP
```
LLDP confirmed: rabbit-lorax is "Arista Networks EOS version 4.27.0F running
on a Facebook WEDGE100S12V".

### Autoneg behavior on Tomahawk 1
Enabling CL73 autoneg (`port ceN an=on`) causes the port to negotiate down to
25G KR2 — wrong for 100G DAC. The correct approach is forced 100G CR4 with
RS-FEC and no autoneg. The RS-FEC setting in SONiC config_db triggers the
BCM SDK to select CR4 automatically.

### Source change for next build
Added `fec` column to `port_config.ini` with `rs` for all 32 ports (following
the Wedge100BF pattern). This ensures first-boot config_db.json includes
`"fec": "rs"` for every port.

### EEPROM identifier issue (not fixable in software)
Two types of DAC cables are present:

| Ports | Identifier | Compliance | xcvr_api | Notes |
|-------|-----------|------------|----------|-------|
| 0,4,8,12,16 | 0x01 (GBIC) | Unknown | None | Cheap DACs, minimal EEPROM |
| 20,28 | 0x11 (QSFP28) | 100GBASE-CR4 | Sff8636Api | Better DACs, proper EEPROM |

The cheap DAC cables have genuinely wrong EEPROM identifier bytes (0x01 instead
of 0x0D or 0x11). SONiC's xcvr library doesn't map identifier 0x01 to any QSFP
API, so `get_xcvr_api()` returns None. xcvrd still reads basic EEPROM data via
a fallback path for ports 0/4/8/12, but port 16 (Ethernet64) failed on first
attempt and never recovered.

The "GBIC" type display in `show interface status` is correct per the cable's
EEPROM data — the cables are simply mislabeled/unprogrammed. This does NOT
affect link state or data plane — all 4 connected ports pass traffic at 100G.

### Files Changed
- `device/.../Accton-WEDGE100S-32X/port_config.ini` — added `fec: rs` column

### Remaining Items
- Ethernet64 (port 16): present per GPIO but xcvrd can't read EEPROM (no link
  partner connected; same cheap DAC cable type as working ports 0/4/8/12)
- Consider adding `serdes_driver_current=0x8` per-lane to BCM config (AS7712 has
  this, Wedge100S does not — may improve signal margin)
- User mentioned two of the four DAC links are LAG-configured on the Arista side
