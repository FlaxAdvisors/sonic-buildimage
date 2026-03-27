# LP_MODE Readiness Guard, Bus Recovery, and BMC Escalation Fix

Session: 2026-03-27 (continuation)

---

## Background

During the previous session, the verification test (`rm sfp_21_eeprom` + daemon restart)
confirmed the LP_MODE readiness guard worked: byte 220 = `0x0c` (correct SR4-100G
vendor info) appeared after 4 s, absent at 1 s. Shortly after, `show interfaces status`
showed `Type = N/A` for all populated SFP ports.

---

## Root Cause: Stuck CP2112, Not the Guard

The CP2112 USB-HID bridge entered a fully stuck state. Every daemon tick logged:

```
wedge100s-i2c-daemon: PCA9535[0] mux select failed
wedge100s-i2c-daemon: PCA9535[1] mux select failed
```

The daemon could not select the muxes to read presence, so no EEPROM files were
written and `xcvrd` had nothing to read → `Type = N/A`.

The stuck state was caused by the `daemon_init` BMC escalation loop failing silently
(see below). The daemon crash-looped repeatedly, each restart attempt hammering the
CP2112 with HID transactions until the bridge locked up permanently.

Recovery required a hardware GPIO reset via BMC:

```bash
# From dev host or any SSH session to BMC
ssh root@192.168.88.13 '/usr/local/bin/reset_cp2112.sh && sleep 1 && /usr/local/bin/reset_usb.sh'
```

After that, `sudo systemctl restart wedge100s-i2c-daemon` recovered cleanly, and
`xcvrd` restart showed `QSFP28 or later` in the Type column. (verified on hardware 2026-03-27)

---

## Root Cause of Crash Loop: BMC Escalation Failure

`daemon_init()` escalates to BMC on `mux_deselect_all()` failure by SSHing to
`root@fe80::ff:fe00:1%usb0` with `/etc/sonic/wedge100s-bmc-key`. The BMC clears
`authorized_keys` on every BMC reboot. After a BMC reboot the SSH key is gone, so
the `system(SSH_FLUSH)` and `system(SSH_RESET_MUX)` calls fail silently — `(void)system(…)`
discards the return code. The daemon exits with failure, systemd restarts it, the
cycle repeats until the CP2112 locks up from repeated half-completed HID transactions.

### Fix (committed)

Added `/usr/bin/wedge100s-bmc-auth` call **before** the SSH escalation commands:

```c
/* Re-provision BMC SSH key via /dev/ttyACM0 before trying SSH.
 * The BMC clears authorized_keys on every reboot; without this
 * the SSH commands below fail silently and escalation does nothing. */
(void)system("/usr/bin/wedge100s-bmc-auth >/dev/null 2>&1");
(void)system(SSH_FLUSH);
(void)system(SSH_RESET_MUX);
```

`wedge100s-bmc-auth` logs in to the BMC via `/dev/ttyACM0` at 57600 baud and
appends the SSH public key idempotently. It adds up to ~10 s on the error recovery
path (acceptable; it's not the happy path).

---

## LP_MODE Readiness Guard (Task A) — Confirmed Working

The guard itself was correct. Behaviour (1 s tick, presence runs before lpmode):

| Time | Event |
|------|-------|
| t=0  | `daemon_init`: `set_lpmode_hidraw(p, 0)` all ports → stamps `g_lp_deassert_ns` |
| t=1s | tick 1: `poll_presence_hidraw` → `refresh_eeprom_lower_page`, elapsed=1s < 2.5s → SKIP |
| t=1s | tick 1: `poll_lpmode_hidraw` → state file absent → `set_lpmode_hidraw` → timestamp reset to t=1s, state file written |
| t=2s | tick 2: elapsed=1s < 2.5s → SKIP |
| t=3s | tick 3: elapsed=2s < 2.5s → SKIP |
| t=4s | tick 4: elapsed=3s > 2.5s → upper-page read from hardware → write cache |

After tick 1, `poll_lpmode_hidraw` stops re-stamping (state file exists), so the
guard expires naturally at ~4 s from daemon start. `xcvrd` sees correct `Type` column.

Key code path in `refresh_eeprom_lower_page()`:

```c
long long elapsed = now_ns() - g_lp_deassert_ns[port];
if (g_lp_deassert_ns[port] != 0 && elapsed < LP_MODE_READY_NS) {
    mux_deselect(mux_addr);
    return 0;   /* not ready; caller will retry next tick */
}
uint8_t upper_addr = 0x80;
int ur = cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
if (ur != 128) {
    mux_deselect(mux_addr);
    return 0;   /* read failed; do not write zeros */
}
```

The `ur != 128` check (added alongside the guard) is a latent bug fix: previously
the upper-page read return value was unchecked, so a failed read silently wrote
zeros into the cache, corrupting byte 220 permanently.

---

## New Utility: `/usr/bin/wedge100s-bus-reset.sh`

Installed by the platform `.deb` from `utils/wedge100s-bus-reset.sh`.

Use when `journalctl -u wedge100s-i2c-daemon` shows persistent:
- `mux_deselect_all failed`
- `PCA9535[N] mux select failed` (every tick for minutes)
- `daemon_init failed after BMC escalation` (crash loop)

```bash
sudo wedge100s-bus-reset.sh
```

Steps performed:
1. `systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon`
2. `wedge100s-bmc-auth` — re-provisions BMC SSH key via ttyACM0
3. SSH to BMC → `reset_cp2112.sh` + `reset_usb.sh` (hardware GPIO reset)
4. Falls back to USB authorize-cycle if BMC SSH fails
5. Waits for `/dev/hidraw0` re-enumeration (up to 10 s)
6. `systemctl start wedge100s-i2c-daemon wedge100s-bmc-daemon pmon`

---

## New Test: `test_lp_mode_readiness_lock`

Added to `tests/stage_03_platform/test_platform.py`.

Procedure:
1. Find a populated port with a valid EEPROM (byte 0 in [0x01, 0x7f])
2. Restart `wedge100s-i2c-daemon`
3. Delete the port's EEPROM cache file
4. Assert file is **absent** at t+1 s (guard active)
5. Assert file is **present with valid identifier** at t+5 s (guard expired)

Fails with a descriptive message if the file appears within 1 s (guard broken) or
is absent/corrupt at 5 s (read failure). Skips if no populated optical port found.

---

## LLDP Over Fiber (Diagnostic Finding)

`test_ethernet108_lldp_neighbor` was SKIPPED because `show lldp neighbors` had no
entry for Ethernet108, despite `oper=up / admin=up`.

LLDP is medium-agnostic; it works over fiber just as well as DAC.  The skip is
correct: the device at the far end of the SR4 fiber is not running lldpd or has
LLDP disabled on that port.  This is a peer configuration issue, not a platform bug.

The test was improved to distinguish two skip cases:
- `oper=down` → fiber disconnected, skip with "no LLDP expected"
- `oper=up`  → peer has LLDP disabled, skip with explicit message

---

## Platform Utility Locations

All `utils/` files (non-`.c`) are installed to `/usr/bin/` by `debian/rules`:

| Binary | Purpose |
|--------|---------|
| `/usr/bin/wedge100s-i2c-daemon` | QSFP presence + EEPROM cache daemon |
| `/usr/bin/wedge100s-bmc-daemon` | BMC sensor polling daemon |
| `/usr/bin/wedge100s-bmc-auth`   | BMC SSH key provisioning via ttyACM0 |
| `/usr/bin/wedge100s-bus-reset.sh` | Emergency CP2112 bus recovery |
| `/usr/bin/clear_led_diag.sh`    | Clear LED diagnostic mode |
| `/usr/bin/accton_wedge100s_util.py` | Platform utility (legacy) |

BMC reset scripts (run via SSH from SONiC to BMC at `root@fe80::ff:fe00:1%usb0`):

| Script | Purpose |
|--------|---------|
| `/usr/local/bin/reset_cp2112.sh` | Hardware GPIO reset of CP2112 chip |
| `/usr/local/bin/reset_usb.sh`    | Hardware GPIO reset of USB hub |
| `/usr/local/bin/cp2112_i2c_flush.sh` | Software cancel of in-flight CP2112 transaction |
| `/usr/local/bin/reset_qsfp_mux.sh`   | Reset QSFP mux tree |
