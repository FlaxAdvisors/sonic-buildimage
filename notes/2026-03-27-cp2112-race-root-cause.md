# CP2112 Race Condition — Root Cause and Fix (2026-03-27)

## Symptoms
- `wedge100s-i2c-daemon` PCA9535 reads fail seconds after clean start
- "PCA9535[N] reg X read failed" and "initial deassert port N failed" flood the journal
- Eventually: "PCA9535[N] mux select failed" (fully stuck)
- xcvrd sees no EEPROM data → `show interface status` Type = N/A
- BMC flush (`cp2112_i2c_flush.sh + reset_qsfp_mux.sh`) temporarily fixes it, then fails again

## Root Cause

**Two distinct bugs, both fixed:**

### Bug 1: LP_MODE deassert EEPROM read race (wedge100s-i2c-daemon.c ~line 799)

In `poll_lpmode_hidraw()`, after `set_lpmode_hidraw(port, 0)` deasserts LP_MODE, the code
immediately calls `refresh_eeprom_lower_page(port, eeprom_path)`. During LP_MODE exit, the
QSFP module's MCU resets and cannot respond to I2C for 2-5 seconds. The `cp2112_write_read(0x50, 
..., 128)` hangs the I2C bus. `cp2112_wait_complete()` times out (2.2s). The internal 
`cp2112_cancel()` cannot clear a hardware-hung I2C bus. The subsequent `mux_deselect()` also 
fails (CP2112 still BUSY), leaving the mux channel selected and the CP2112 permanently stuck.

**Fix:** Removed the `refresh_eeprom_lower_page()` call after LP_MODE deassert. The EEPROM cache
written by `poll_presence_hidraw()` earlier in the same tick (while module was in LP_MODE, when
it CAN respond) is sufficient — the identifier byte is stable across LP_MODE transitions.

### Bug 2: Stale HID report interference from inotify path (wedge100s-i2c-daemon.c ~line 1333)

`service_write_requests()` (called on every inotify `IN_CLOSE_WRITE` event from `/run/wedge100s/`)
calls `apply_led_writes()` which accesses CPLD sysfs via the kernel's hid-cp2112 I2C driver. The
kernel driver leaves 1-2 STATUS_RESPONSE HID reports in the CP2112's USB input buffer as "stale"
data. When `service_write_requests()` fires again (triggered by wedge100s-bmc-daemon writing fan/
PSU files every ~5 seconds), `poll_lpmode_hidraw()` runs and its `mux_select()` → 
`cp2112_wait_complete()` receives the stale STATUS_ERROR or STATUS_COMPLETE from the CPLD access
instead of the response to its own DATA_WRITE_REQUEST. This causes spurious failures.

The timer tick path has `cp2112_cancel()` at the top to drain these stale reports. The inotify
path (`service_write_requests`) had no such drain.

**Fix:** Added `cp2112_cancel()` at the top of `service_write_requests()` to drain stale reports
before any hidraw operations.

## Why It Appeared Now (Not Earlier)

Bug 1: Always latent. Masked if modules are not present or quickly exit LP_MODE.
Bug 2: Only triggered when `sfp_N_present` files from a previous run exist in `/run/wedge100s/`
at daemon startup. Without stale present files, `poll_lpmode_hidraw()` finds no present ports on
inotify events and skips all operations, so no race occurs. With stale files (from the session
where we were investigating Bug 1), the race was exposed.

## Verification (verified on hardware 2026-03-27)

- Daemon starts clean with `daemon_init OK`
- Zero errors in journal for 30+ seconds continuous operation
- EEPROM files appear in `/run/wedge100s/sfp_N_eeprom` for all present ports
- `show interface status` Type column: "QSFP28 or later" for ports with plugged modules, N/A elsewhere
