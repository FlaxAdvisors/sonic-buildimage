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

- `SffManagerTask.enable_high_power_class(xcvr_api, lport)` — wrapper that calls
  `xcvr_api.set_high_power_class(power_class, True)`, which sets byte 93 bit 2 for PC5–PC7
  or bit 3 for PC8.
- `api.set_lpmode(False)` — called after `enable_high_power_class`; writes byte 93 bit 0
  (harmless on this platform — see §Bit Ordering note below).
- Gated on `host_tx_ready=true` AND `admin_status=up` in STATE_DB, providing deterministic
  TX bring-up.

All EEPROM writes from sff_mgr flow through `Sfp.write_eeprom()` →
`/run/wedge100s/sfp_N_write_req` → `wedge100s-i2c-daemon`, preserving the invariant that the
daemon is the sole I2C initiator. The constraint is not broken.

### Bit Ordering Note

SONiC's `sff8636` mem map (`sonic_xcvr/mem_maps/public/sff8636.py`) uses the **same bit
positions** as SFF-8636 but has the **field names swapped**:

The **SFF-8636 spec is authoritative** on byte 93 bit assignments. SONiC's `sff8636` mem map
uses the same bit positions but has the field names swapped relative to the spec:

| SFF-8636 Rev 2.10 name (authoritative) | SFF-8636 bit | SONiC field name (incorrectly named) | SONiC bit position |
|-----------------------------------------|--------------|--------------------------------------|--------------------|
| Power Set (0=full power, 1=force low)  | 0            | `POWER_OVERRIDE_FIELD`               | 0                  |
| Power Override (1=use byte 93 SW ctrl) | 1            | `POWER_SET_FIELD`                    | 1                  |

In short: SONiC's `POWER_OVERRIDE_FIELD` is SFF-8636's Power Set; SONiC's `POWER_SET_FIELD`
is SFF-8636's Power Override. The bit positions are correct; only the names are swapped.

`api.set_lpmode(False)` calls `set_power_override(True, False)`, which writes:
- SONiC `POWER_OVERRIDE_FIELD` (= SFF Power Set, bit 0) = True → bit 0 = 1
- SONiC `POWER_SET_FIELD` (= SFF Power Override, bit 1) = False → bit 1 = 0

Per SFF-8636: **Power Override (bit 1) = 0 means the module ignores byte 93 bits 0–1
entirely and uses the hardware LP_MODE pin for power mode.** With bit 1 = 0, the Power
Set bit 0 = 1 written by SONiC is irrelevant — the module never reads it.

This is **harmless** on the Wedge 100S because:

1. `_init_power_override` is removed, so SFF Power Override (bit 1) stays 0.
2. With bit 1 = 0, the module uses only the LP_MODE hardware pin, which the daemon
   deasserted at insertion time → module is already in full-power mode.
3. SFF High Power Class Enable (bit 2, set by `set_high_power_class`) is the write that
   actually matters for PC5–PC7 — it is orthogonal to the Power Override/Set pair.

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
         power_class >= 5 → SffManagerTask.enable_high_power_class(xcvr_api, lport):
           xcvr_api.set_high_power_class(6, True)
           xcvr_eeprom.write(HIGH_POWER_CLASS_ENABLE_CLASS_5_TO_7, True)
           RegBitField(bit2).encode(True, read_current_byte93)  [read-before-write]
           → Sfp.write_eeprom(93, 1, bytearray([byte93 | 0x04]))
           → /run/wedge100s/sfp_N_write_req  →  daemon writes byte93 bit2=1 to module EEPROM
           (module now permitted to draw up to 4.5 W)
         api.get_lpmode_support() → True (PC6 != PC1)
         api.set_lpmode(False):
           set_power_override(True, False)
           → POWER_OVERRIDE_FIELD(SONiC bit0 = SFF Power Set)=1 [read-before-write]
           → POWER_SET_FIELD(SONiC bit1 = SFF Power Override)=0
           → byte93 on module = 0x05  (bit2=1, bit0=1; bit1=0)
           → SFF Power Override (bit1) = 0 → module ignores bits 0–1, uses LP_MODE pin
         module uses LP_MODE pin (already LOW) → full power + class 6 budget → oper up ✓

         sff_mgr also controls TX disable via byte 86 based on host_tx_ready + admin_status:
           api.get_tx_disable() → Sfp.read_eeprom() [triggers DOM refresh on cold cache;
             uses sfp_N_read_req/resp IPC; 5s timeout, acceptable before host_tx_ready gate]
           target_tx_disable = not (host_tx_ready=true AND admin_status=up) = False
           → api.set_tx_disable_channel() → Sfp.write_eeprom() → daemon → byte 86 = 0x00
             (all TX channels enabled) ✓

T+~3s  xcvrd DOM poll TTL expired:
         Sfp._hardware_read_lower_page() → read_req/resp → daemon reads live DOM bytes
         real Rx power, Tx power, temperature, voltage → STATE_DB TRANSCEIVER_DOM_INFO
         pm data available ✓
```

---

## Power Class Behavior Matrix

| Class | Max W | Byte 129 detection | Before (byte93 result) | After (byte93 result) |
|-------|-------|--------------------|------------------------|------------------------|
| PC1 | 1.5 | bits 7:6=`00`, bit5=0, bits1:0=any | LP_MODE deasserted, byte93=0x00 | Same — sff_mgr sees lpmode_support=False, skips all writes |
| PC2 | 2.0 | bits 7:6=`01`, bits1:0=any | LP_MODE deasserted, byte93=0x00 | LP_MODE + byte93=0x01 (bit0 set by api.set_lpmode); harmless — bit1=0 so module ignores SW override, uses LP_MODE pin |
| PC3 | 2.5 | bits 7:6=`10`, bits1:0=any | LP_MODE deasserted, byte93=0x00 | LP_MODE + byte93=0x01 (bit0 set by api.set_lpmode); harmless — bit1=0 so module ignores SW override, uses LP_MODE pin |
| PC4 | 3.5 | bits 7:6=`11`, bits1:0=`00` | LP_MODE + byte93=0x02 (bit1 set by _init_power_override) | LP_MODE + byte93=0x01 (bit0 set by api.set_lpmode; _init_power_override removed); **harmless, link unaffected** |
| PC5 | 4.0 | bits 7:6=`11`, bits1:0=`01` | LP_MODE + byte93=0x02; **bit2 missing → capped at 3.5W** | LP_MODE + byte93=0x05 (**bit2 set** → 4.0W) ✓ |
| PC6 | 4.5 | bits 7:6=`11`, bits1:0=`10` | LP_MODE + byte93=0x02; **bit2 missing → no link** | LP_MODE + byte93=0x05 (**bit2 set** → 4.5W → link) ✓ |
| PC7 | 5.0 | bits 7:6=`11`, bits1:0=`11` | LP_MODE + byte93=0x02; **bit2 missing → capped** | LP_MODE + byte93=0x05 (**bit2 set** → 5.0W) ✓ |
| PC8 | var | bits 7:6=`00`, bit5=1, bits1:0=any | **Not detected, no init** | LP_MODE + byte93=0x09 (**bit3 set** → class 8 budget) ✓ |

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
- sff_mgr's `get_tx_disable()` call (to determine current TX state before writing byte 86)
  triggers a DOM refresh on cold cache (first read after insertion). This goes through
  `Sfp._hardware_read_lower_page()` → `sfp_N_read_req`/`sfp_N_read_resp` IPC with a 5 s
  timeout. This is acceptable: the 5 s window is well within the `host_tx_ready` propagation
  time, and the daemon prioritizes read requests over tick-cycle work.

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
5. Verify byte93 via live hardware read (the EEPROM cache file is not updated by
   `write_eeprom`; use the read_req IPC to read from the module directly):
   ```bash
   ssh admin@192.168.88.12 "python3 -c \"
   import json, os, time
   p = N  # replace with port number (0-based)
   req = f'/run/wedge100s/sfp_{p}_read_req'
   rsp = f'/run/wedge100s/sfp_{p}_read_resp'
   try: os.unlink(rsp)
   except: pass
   open(req, 'w').write(json.dumps({'offset': 93, 'length': 1}))
   deadline = time.monotonic() + 5
   while time.monotonic() < deadline:
       if os.path.exists(rsp): break
       time.sleep(0.05)
   result = open(rsp).read().strip()
   print(hex(bytes.fromhex(result)[0]))  # expect 0x05 (bit2 set)
   \""
   ```
6. Verify TX channels enabled (byte 86 = 0x00):
   Use the same read_req IPC with `{'offset': 86, 'length': 1}` → expect `0x00`.
7. Regression: existing PC1 and PC4 ports remain oper up after restart of pmon.
8. Cold-boot regression: with a PC6 module installed, reboot the switch (not just pmon)
   and verify the port comes up `oper up` without manual re-insertion. sff_mgr replays
   all TRANSCEIVER_INFO entries from STATE_DB on xcvrd startup, so the `enable_high_power_class`
   write must fire on boot as well as on hot-insert.
