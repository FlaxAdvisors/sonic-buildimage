# Design: xcvr Power Class Initialization via sff_mgr

**Date:** 2026-03-24
**Branch:** wedge100s
**Status:** Approved

---

## Problem

Newly inserted QSFP28 transceivers of Power Class 5, 6, or 7 do not come up `oper up` on the
Accton Wedge 100S-32X. The root cause is that SFF-8636 requires two writes to byte 93 of the
module EEPROM before a PC5–PC7 module is permitted to draw above PC4 (3.5 W) power:

| Byte 93 bit | Name (SFF-8636 Rev 2.10) | Purpose |
|-------------|--------------------------|---------|
| bit 1 | Power Override | 1 = module uses byte 93 for software power control |
| bit 0 | Power Set | 0 = full power (when Override=1), 1 = force low power |
| bit 2 | High Power Class Enable (class 5–7) | 1 = allow power above 3.5 W |
| bit 3 | High Power Class Enable (class 8) | 1 = allow class 8 power |

The existing `_init_power_override()` method in `sonic_platform/sfp.py` sets bit 1 (Power
Override) for any module where byte 129 bits 7:6 = `0b11` (Power Class 4 or higher), but never
examines byte 129 bits 1:0 to distinguish PC5–PC7, and never sets bit 2. PC6 modules are
therefore capped at 3.5 W regardless of their rated 4.5 W ceiling.

**Hardware evidence (2026-03-24):**
- Port 26 (ext_id=0xce → PC6, 4.5 W max): byte93=0x02 — bit 2 missing → no link.
- Ports 19, 21, 25, 27 (ext_id=0xcc → PC4, 3.5 W max): byte93=0x02 — correct, link up.

PC8 modules (byte 129 bit 5 = 1, bits 7:6 = `0b00`) are not detected at all by the current
condition and receive no initialization.

---

## Solution: Enable sff_mgr (Approach B)

SONiC ships `sff_mgr` (`sonic-platform-daemons/sonic-xcvrd/xcvrd/sff_mgr.py`), a task inside
`xcvrd` that is disabled by default. It handles exactly this problem:

- `enable_high_power_class(power_class, True)` — sets byte 93 bit 2 for PC5–PC7, bit 3 for PC8.
- `api.set_lpmode(False)` — called after `enable_high_power_class`; writes byte 93 bit 0
  (harmless on this platform — see §Bit Ordering note below).
- Gated on `host_tx_ready=true` AND `admin_status=up` in STATE_DB, providing deterministic
  TX bring-up.

All EEPROM writes from sff_mgr flow through `Sfp.write_eeprom()` →
`/run/wedge100s/sfp_N_write_req` → `wedge100s-i2c-daemon`, preserving the invariant that the
daemon is the sole I2C initiator. The constraint is not broken.

### Bit Ordering Note

SONiC's `sff8636` mem map names `POWER_OVERRIDE_FIELD` at bit position 0 and `POWER_SET_FIELD`
at bit position 1 — the opposite of the SFF-8636 standard (bit 1 = Power Override, bit 0 =
Power Set). As a result, `api.set_lpmode(False)` writes bit 0 = 1 (labeled "Power Override" in
SONiC, but is SFF-8636's "Power Set"). This is **harmless** on the Wedge 100S because:

1. `_init_power_override` is removed, so bit 1 (SFF-8636 Power Override) stays 0.
2. With bit 1 = 0, the module ignores software byte 93 control entirely and uses the LP_MODE
   hardware pin.
3. The daemon already deasserted the LP_MODE pin before sff_mgr runs; the module is in
   full-power mode via hardware.
4. Bit 2 = 1 (set by `enable_high_power_class`) unlocks the higher power budget — this is
   the write that actually matters.

This behavior matches Arista and Nokia deployments that also use `enable_xcvrd_sff_mgr`.

---

## What Is Changed

### 1. `device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json`

Add `"enable_xcvrd_sff_mgr": true`.

```json
{
    "skip_ledd": false,
    "skip_xcvrd": false,
    "skip_psud": false,
    "skip_thermalctld": false,
    "enable_xcvrd_sff_mgr": true
}
```

The supervisord Jinja2 template (`docker-pmon.supervisord.conf.j2`) picks up this flag and
appends `--enable_sff_mgr` to the xcvrd command line, activating `SffManagerTask`.

### 2. `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`

Remove:
- `_POWER_INIT_MTIME` module-level dict.
- `_init_power_override()` method.
- The `self._init_power_override(cached_data)` call in `read_eeprom()`.

No other changes to sfp.py. The `write_eeprom`, `read_eeprom`, `set_lpmode`, `get_lpmode`,
`get_presence`, and all other methods are untouched.

---

## What Is NOT Changed

- `wedge100s-i2c-daemon.c` — LP_MODE pin deassert logic, EEPROM read/cache, presence polling.
- `Sfp.write_eeprom` / `read_eeprom` protocol (request/ack/response files).
- DOM cache and TTL mechanism (`_DOM_CACHE_TTL`, `_hardware_read_lower_page`).
- No other platform files. No upstream SONiC submodule changes.

---

## Data Flow After Change

```
T+0s   daemon tick (every 3 s via wedge100s-i2c-poller.timer):
         PCA9535 INPUT → module present
         CP2112 HID: read 256-byte EEPROM page 0
         writes /run/wedge100s/sfp_N_eeprom
         poll_lpmode_hidraw(): no state file for this port
           → set_lpmode_hidraw(port, 0)  [LP_MODE pin LOW = deasserted]
           → writes sfp_N_lpmode = "0"
           → calls refresh_eeprom_lower_page() to overwrite DOM snapshot
         (module powered up; PC5–7 caps at 3.5 W until byte93 bit2 set)

T+~1s  xcvrd poll cycle:
         Sfp.get_presence() → "1"
         Sfp.read_eeprom(0, 256) → returns EEPROM bytes (no _init_power_override)
         xcvr_api_factory.create_xcvr_api():
           ident byte 0x11 → Sff8636Api
         posts TRANSCEIVER_INFO to STATE_DB

T+~1s  sff_mgr event loop wakes on TRANSCEIVER_INFO insert:
         xcvr_api.get_power_class() → reads byte 129 bits via POWER_CLASS_FIELD
           ext_id=0xce → code 194 → "Power Class 6 Module (4.5W max.)"  → power_class = 6
         power_class >= 5 → enable_high_power_class(6, True):
           xcvr_eeprom.write(HIGH_POWER_CLASS_ENABLE_CLASS_5_TO_7, True)
           RegBitField(bit2).encode(True, read_current_byte93)
           → Sfp.write_eeprom(93, 1, bytearray([byte93 | 0x04]))
           → /run/wedge100s/sfp_N_write_req  →  daemon writes byte93 bit2=1
           (module now permitted to draw up to 4.5 W)
         api.get_lpmode_support() → True (PC6 != PC1)
         api.set_lpmode(False):
           set_power_override(True, False)
           → writes POWER_OVERRIDE_FIELD(bit0)=1, POWER_SET_FIELD(bit1)=0
           → byte93 = 0x05  (bit2=1, bit0=1; bit1=0 → module ignores SW override)
         module uses LP_MODE pin (already LOW) → full power + class 6 budget → oper up ✓

T+~3s  xcvrd DOM poll TTL expired:
         Sfp._hardware_read_lower_page() → read_req/resp → daemon reads live DOM bytes
         real Rx power, Tx power, temperature, voltage → STATE_DB TRANSCEIVER_DOM_INFO
         pm data available ✓
```

---

## Power Class Behavior Matrix

| Class | Max W | Byte 129 detection | Before | After |
|-------|-------|--------------------|--------|-------|
| PC1 | 1.5 | bits 7:6 = `00`, bit 5 = 0 | LP_MODE pin deasserted, byte93=0x00 | Same — sff_mgr sees lpmode_support=False, skips |
| PC2 | 2.0 | bits 7:6 = `01` | LP_MODE deasserted | Same |
| PC3 | 2.5 | bits 7:6 = `10` | LP_MODE deasserted | Same |
| PC4 | 3.5 | bits 7:6 = `11`, bits 1:0 = `00` | LP_MODE + bit1 set | LP_MODE; sff_mgr skips `enable_high_power_class` (PC4<5), writes bit0 (harmless) |
| PC5 | 4.0 | bits 7:6 = `11`, bits 1:0 = `01` | LP_MODE + bit1; **bit2 never set → capped** | LP_MODE + **bit2 set** ✓ |
| PC6 | 4.5 | bits 7:6 = `11`, bits 1:0 = `10` | LP_MODE + bit1; **bit2 never set → no link** | LP_MODE + **bit2 set → link** ✓ |
| PC7 | 5.0 | bits 7:6 = `11`, bits 1:0 = `11` | LP_MODE + bit1; **bit2 never set → capped** | LP_MODE + **bit2 set** ✓ |
| PC8 | var | bits 7:6 = `00`, bit 5 = 1 | **Not detected, no init** | LP_MODE + **bit3 set** ✓ |

---

## Pre-requisite Compliance

`sff_mgr` documentation states: *"platform needs to keep TX in disabled state after module
coming out-of-reset."* The Wedge 100S daemon deasserts LP_MODE immediately on insertion,
so TX is not explicitly held. However:

- For link-stability use cases, sff_mgr additionally controls TX via byte 86 (Tx Disable)
  once `host_tx_ready` is true. This works correctly via `Sfp.write_eeprom`.
- The brief window between LP_MODE deassert and sff_mgr's first write is acceptable for
  this platform; no link-flap issues have been observed on other modules.
- `host_tx_ready` is confirmed "true" in STATE_DB (db 6, PORT_TABLE) for all Ethernet
  ports on the target (verified 2026-03-24).

If strict TX gating is required in the future, the daemon's `poll_lpmode_hidraw()` initial
deassert could be deferred until an sff_mgr-written sentinel file exists, but this is out of
scope for the current fix.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json` | Add `"enable_xcvrd_sff_mgr": true` |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Remove `_POWER_INIT_MTIME`, `_init_power_override()`, and its call site in `read_eeprom()` |

---

## Testing

1. Hot-insert a PC6 module (ext_id=0xce) into any port.
2. After ~3 s: `show interfaces status` → port should be `U` (oper up).
3. Verify `redis-cli -n 6 hgetall 'PORT_TABLE|EthernetN'` shows `oper_status=up`.
4. Verify `redis-cli -n 6 hgetall 'TRANSCEIVER_DOM_INFO|EthernetN'` shows non-zero
   Rx/Tx power values (pm working).
5. Verify byte93 on hardware:
   ```bash
   ssh admin@192.168.88.12 "python3 -c \"
   with open('/run/wedge100s/sfp_N_eeprom','rb') as f: d=f.read(256)
   print(hex(d[93]))  # expect 0x05 or 0x07 (bit2 set)
   \""
   ```
6. Regression: existing PC1 and PC4 ports remain oper up after restart of pmon.
