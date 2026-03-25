# LED Control & Daemon Architecture Design
**Date:** 2026-03-24
**Branch:** wedge100s
**Status:** Approved for implementation planning

---

## Problem Statement

Three independent issues motivate this design:

1. **Port LED rainbow pattern** — syscpld register 0x3c defaults to `0xe0` (LED test mode) at hardware power-on. No BMC boot script clears it. The BCM LEDUP output cannot reach physical front-panel LEDs until `th_led_en=1` and `led_test_mode_en=0`. Every NOS (Cumulus, EOS, SONiC) must arrange for this register to be set. The current SONiC approach (bmc-daemon writes it every 10s) is correct but fragile — it requires active SONiC↔BMC communication indefinitely.

2. **Timer+oneshot churn** — both i2c-daemon and bmc-daemon run as systemd-timer-activated oneshot services. At 3s (i2c) and 10s (bmc) intervals this means repeated open/close of `/dev/hidraw0` and SSH ControlMaster setup per cycle. Confirmed stale-report behavior (two HID reports left after each CPLD sysfs access) is currently managed implicitly by device re-open; this is fragile and masks bugs.

3. **Write-request latency** — pmon writes desired LED state, LP_MODE changes, and DOM read requests to `/run/wedge100s/` files; the daemon services them on its next tick. At 3s intervals this means up to 3s lag for LED updates and DOM refreshes.

---

## Hardware Constraints (established by prior investigation)

- `syscpld` (BMC i2c-12 / addr `0x31`) is **BMC-only** — confirmed `--` at `0x31` on host `i2cdetect -y 1`. No host-side path exists.
- PCA9535 `INT_L` is **not routed to any host CPU GPIO** — confirmed by live GPIO enumeration and reference platform survey. Polling is the only option on this hardware.
- BMC clears `authorized_keys` on every BMC reboot — SSH key must be re-pushed before each SSH session that may follow a BMC reboot.

---

## Deliverables

Four independent, sequentially buildable deliverables:

| # | Name | Risk | Dependency |
|---|---|---|---|
| D1 | BMC LED init patch | Low — one-time SSH write to BMC filesystem | None |
| D2 | i2c-daemon persistent | Medium — C daemon rewrite | D1 (removes syscpld_led_ctrl) |
| D3 | bmc-daemon persistent | Medium — C daemon rewrite | wedge100s-bmc-auth (from D2 build) |
| D4 | LED link+speed verify | Low — hardware test, fix only if needed | D1 |

---

## Deliverable 1 — BMC `setup_board.sh` Patch

### Root cause
syscpld register `0x3c` hardware power-on default is `0xe0` (test mode, rainbow). No BMC init script ever clears it. `S60setup_i2c.sh` registers the syscpld device; `S80setup_board.sh` runs next but does not touch LED registers; `S85power-on.sh` boots the host. The register is `0xe0` by the time ONIE or SONiC runs.

### Solution
`wedge100s-platform-init.service` (already runs before pmon) gains a one-time idempotent step to patch the BMC's own `setup_board.sh`. After this patch is applied, the BMC self-manages LED test mode at every BMC boot — no further SONiC involvement needed.

### `clear_led_diag.sh` — BMC-side utility

The four LED register writes are factored into a named BMC utility at `/usr/local/bin/clear_led_diag.sh`. This means:

- Any operator can clear the rainbow at any time: `ssh root@bmc 'clear_led_diag.sh'`
- SONiC platform-init calls it directly on every boot — no SONiC reboot required after BMC reflash
- `setup_board.sh` just calls it — no register writes embedded in setup_board.sh itself

```sh
#!/bin/sh
# clear_led_diag.sh — disable syscpld LED test pattern, enable TH LEDUP output
# Installed by SONiC platform-init. Safe to run at any time.
. /usr/local/bin/board-utils.sh
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_mode_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_blink_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/walk_test_en
echo 1 > ${SYSCPLD_SYSFS_DIR}/th_led_en
```

### Platform-init sequence (revised)

```
wedge100s-platform-init:
  1. Load kernel modules, create /run/wedge100s/
  2. wedge100s-bmc-auth              ← push SSH key via /dev/ttyACM0
  3. SSH probe: ssh -O check OR 'echo ok'  ← gate for steps 4-5
  4. If SSH reachable:
       a. Deploy clear_led_diag.sh to BMC /usr/local/bin/ if absent or changed
       b. grep -q "clear_led_diag.sh" /etc/init.d/setup_board.sh
          If absent: append "    clear_led_diag.sh" inside board_rev >= 2 block
       c. ssh bmc 'clear_led_diag.sh'   ← run immediately every boot
       d. Log result to syslog
     If SSH unreachable: log warning, continue (non-fatal)
  5. Continue with i2c init, register devices
```

Step 3 is the gate. SSH failure skips the clear (LEDs show rainbow), but does not block platform init. On the next successful SSH connection (e.g., after BMC recovers), platform-init is not re-run — but an operator can manually run `ssh root@bmc 'clear_led_diag.sh'` or trigger it via a future SONiC CLI command.

Step 4c runs `clear_led_diag.sh` **directly and unconditionally** on every platform-init run. This means:
- First boot after BMC reflash: platform-init deploys the script and clears the rainbow immediately, without any SONiC reboot
- Subsequent boots: script is already deployed, idempotent run clears any transient test-mode state

### `setup_board.sh` patch

One line appended inside the existing `if [ $board_rev -ge 2 ]` block:

```sh
    # SONiC LED init
    clear_led_diag.sh
```

### Impact on bmc-daemon
`syscpld_led_ctrl` read and `.set` file write-back are **removed** from bmc-daemon. This register is now owned entirely by the BMC.

### Impact on `accton_wedge100s_util.py`
`_request_led_init()` and its call from `do_install()` are **removed**. After D1 ships nobody reads `syscpld_led_ctrl.set`; leaving the call in produces a misleading syslog entry and accumulates a stale file in `/run/wedge100s/` indefinitely.

### BMC reflash note
The patch is written to the BMC's writable overlay (OpenBMC squashfs + NAND overlay). A BMC firmware reflash wipes the overlay, reverting `setup_board.sh` to factory state and removing `clear_led_diag.sh`. Two recovery paths exist — no SONiC reboot is required for either:

| Path | How |
|---|---|
| **Operator** | `ssh root@192.168.88.13 'clear_led_diag.sh'` — works after platform-init has re-deployed it on the current SONiC boot |
| **Automatic** | Next SONiC platform-init run re-deploys `clear_led_diag.sh`, re-patches `setup_board.sh`, and runs the script immediately (step 4c) |

### BMC boot order (for reference)
```
S59 openbmc_gpio_setup.py    — GPIO mux config
S60 setup_i2c.sh             — registers syscpld at 12-0031
S80 setup_board.sh           — board init + LED init (after patch)
S85 power-on.sh              — powers on host CPU
```

---

## Shared Utility — `wedge100s-bmc-auth`

Both platform-init (shell) and bmc-daemon (C) need to push the BMC SSH authorized key via TTY. This logic is factored into a single standalone binary to avoid duplication.

### Interface
```
wedge100s-bmc-auth
  Exits 0:   key appended to BMC /root/.ssh/authorized_keys successfully
  Exits 1:   TTY unavailable, login failed, or write failed

Constants (compiled in):
  TTY_DEV   /dev/ttyACM0
  TTY_BAUD  57600
  BMC login  root / 0penBmc
  PUBKEY    /etc/sonic/wedge100s-bmc-key.pub
  TIMEOUT   10s per step (open, login, command, close)
```

### Callers
```sh
# platform-init (shell):
wedge100s-bmc-auth || logger -t platform-init "WARNING: BMC key push failed"

# bmc-daemon (C):
if (system("wedge100s-bmc-auth") != 0) {
    syslog(LOG_ERR, "bmc-daemon: key push failed");
    return -1;
}
```

### Build
The `gcc` invocation lives in `debian/rules` at `override_dh_auto_build` (alongside the existing daemon build steps), not in `platform-modules-accton.mk` (which contains only `.deb` target declarations). A corresponding `unlink` must be added to `override_dh_auto_clean`. The installed binary goes to `/usr/local/bin/` alongside the two daemons.

---

## Deliverable 2 — i2c-daemon: Persistent Daemon

### Architecture change

| Aspect | Current (timer+oneshot) | Proposed (persistent daemon) |
|---|---|---|
| `/dev/hidraw0` lifetime | opened/closed each invocation | opened at startup, held open |
| CP2112 stale-report drain | implicit on `open()` | explicit `cp2112_cancel()` at top of each tick |
| Tick mechanism | systemd timer fires ExecStart | internal `timerfd_create(CLOCK_MONOTONIC)` at **1s** |
| Write-request response latency | up to 3s (next tick) | ~50ms (inotify `IN_CLOSE_WRITE`) |
| systemd unit type | `Type=oneshot` | `Type=simple`, `Restart=on-failure`, `RestartSec=2s` |
| Timer unit | `wedge100s-i2c-poller.timer` | **removed** |

### Daemon init / restart recovery

On every startup (including systemd restart after crash), before entering the main loop:

```c
daemon_init():
  1. open("/dev/hidraw0", O_RDWR)
  2. cp2112_cancel() + drain up to 8 stale reports   // flush in-flight transfer
  3. mux_deselect_all():
       for each addr in {0x70, 0x71, 0x72, 0x73, 0x74}:
           cp2112_write(addr, [0x00], 1)             // deselect all PCA9548 channels
  4. if steps 2-3 fail → BMC escalation via SSH:
       ssh BMC 'cp2112_i2c_flush.sh'                 // pulse i2c_flush_en on syscpld
       ssh BMC 'reset_qsfp_mux.sh'                   // pulse mux reset lines
       retry steps 1-3
  5. if still failing → syslog error, exit(1)        // systemd Restart= handles backoff
```

Step 3 (`mux_deselect_all`) is critical for crash recovery: a daemon that died mid-transaction may have left a PCA9548 mux channel selected, causing all subsequent I2C addresses to be mis-routed. Writing `0x00` to each mux address unconditionally resets all five PCA9548s to "no channel selected" before any real work begins.

### Main loop

```c
// fd setup
hidraw_fd  = open("/dev/hidraw0", O_RDWR);          // kept open
inotify_fd = inotify_init1(IN_NONBLOCK);
inotify_add_watch(inotify_fd, RUN_DIR, IN_CLOSE_WRITE);
timer_fd   = timerfd_create(CLOCK_MONOTONIC, 0);
timerfd_settime(timer_fd, 0, &(itimerspec){.it_interval={1,0}, .it_value={1,0}}, NULL);

struct pollfd pfds[2] = {
    { .fd = timer_fd,   .events = POLLIN },
    { .fd = inotify_fd, .events = POLLIN },
};

while (1) {
    poll(pfds, 2, -1);

    if (pfds[1].revents & POLLIN) {
        drain_inotify(inotify_fd);
        service_write_requests();      // immediate response to *.set / *_req files
    }

    if (pfds[0].revents & POLLIN) {
        uint64_t exp; read(timer_fd, &exp, 8);
        cp2112_cancel();               // explicit drain at tick start
        poll_syseeprom_hidraw();       // boot-once if /run/wedge100s/syseeprom absent
        poll_presence_hidraw();        // 1s presence scan (PCA9535)
        poll_lpmode_hidraw();          // LP_MODE read + apply
        poll_read_requests_hidraw();   // service pending DOM read_req
        poll_write_requests_hidraw();  // service pending EEPROM write_req
        apply_led_writes();            // LED write-through to CPLD sysfs
        poll_cpld();                   // see cpld_version note below
    }
}
```

**CPLD sysfs ordering preserved:** `apply_led_writes()` and `poll_cpld()` run **last within the timer event handler**, after all five hidraw poll functions. Each CPLD sysfs access via `hid_cp2112` leaves two stale HID input reports in the buffer; the explicit `cp2112_cancel()` at the next tick-start drains them.

**inotify write-request handling:** `service_write_requests()` must **scan the directory** for pending `*_req` and `*.set` files rather than replaying filenames from inotify events. This handles burst drops (multiple files simultaneously) and the edge case where a caller overwrites a `_req` file before `poll()` wakes — inotify coalesces duplicate `IN_CLOSE_WRITE` events, so directory scan is the only reliable way to find all pending work.

### `cpld_version` — cached once like `syseeprom`

`cpld_version` is static hardware info (CPLD firmware version). It cannot change without a CPLD reflash and reboot. Polling it every tick wastes one CP2112 transaction per second.

```c
poll_cpld():
    // cpld_version: read once at first tick, cache permanently
    if (!file_exists(RUN_DIR "/cpld_version"))
        seed_cpld_version_from_sysfs();

    // psu state: dynamic — read every tick
    mirror_sysfs_to_run("psu1_present");
    mirror_sysfs_to_run("psu1_pgood");
    mirror_sysfs_to_run("psu2_present");
    mirror_sysfs_to_run("psu2_pgood");
```

### systemd units
- **Remove:** `wedge100s-i2c-poller.timer`, `wedge100s-i2c-poller.service`
- **Add:** `wedge100s-i2c-daemon.service`
  - `Type=simple`
  - `Restart=on-failure`
  - `RestartSec=2s`
  - `After=wedge100s-platform-init.service`

---

## Deliverable 3 — bmc-daemon: Persistent Daemon

### Architecture change

| Aspect | Current (timer+oneshot) | Proposed (persistent daemon) |
|---|---|---|
| SSH ControlMaster | established fresh each invocation | established at startup, kept alive |
| BMC authorized_key | pushed by platform-init only | pushed by `wedge100s-bmc-auth` on init **and** reconnect |
| BMC reconnect | automatic (new process) | explicit: dead socket → key re-push → reconnect |
| Write-request response | up to 10s | ~50ms (inotify) |
| `syscpld_led_ctrl` | read + write every 10s | **removed** (D1 supersedes) |
| `qsfp_led_position` | read every 10s | read once at startup/reconnect |
| systemd unit type | `Type=oneshot` | `Type=simple`, `Restart=on-failure`, `RestartSec=5s` |
| Timer unit | `wedge100s-bmc-poller.timer` | **removed** |

### Connection management

```c
static int bmc_connect(void) {
    // Always re-push key: BMC may have rebooted and cleared authorized_keys
    if (system("wedge100s-bmc-auth") != 0) {
        syslog(LOG_ERR, "bmc-daemon: key push via TTY failed");
        return -1;
    }
    if (ssh_master_connect() < 0) {
        syslog(LOG_ERR, "bmc-daemon: SSH ControlMaster failed");
        return -1;
    }
    // Re-read one-time values on every (re)connect
    read_qsfp_led_position();    // writes /run/wedge100s/qsfp_led_position once
    return 0;
}

static int bmc_ensure_connected(void) {
    if (ssh_control_check() == 0) return 0;      // ControlMaster alive
    ssh_control_exit();
    unlink(CTL_SOCK);
    return bmc_connect();                          // full reconnect with key re-push
}
```

If `bmc_ensure_connected()` returns -1, the tick is skipped. Existing `/run/wedge100s/` files retain their last-good values. `Restart=on-failure` with `RestartSec=5s` handles persistent BMC unavailability without spinning.

### Main loop

```c
timerfd at 10s + inotify on /run/wedge100s/

while (1) {
    poll(pfds, 2, -1);

    if (inotify event) {
        drain_inotify();
        // No active BMC write requests after D1; placeholder for future use
    }

    if (timer event) {
        uint64_t exp; read(timer_fd, &exp, 8);
        if (bmc_ensure_connected() < 0) continue;
        poll_thermals();     // TMP75 × 7 via SSH
        poll_fans();         // fancpld RPM + presence via SSH
        poll_psus();         // PMBus × 2 via SSH
        poll_qsfp_int();     // gpio31 diagnostic → /run/wedge100s/qsfp_int
    }
}
```

**Note on inotify in bmc-daemon:** The `inotify_add_watch` on `/run/wedge100s/` is present as infrastructure for future BMC write-request consumers. After D1, no active write-request handler exists on the bmc side. The inotify machinery is retained deliberately to avoid a second refactor when the first write-request consumer is added (e.g., fan speed control); the cost is negligible.

```
```

### systemd units
- **Remove:** `wedge100s-bmc-poller.timer`, `wedge100s-bmc-poller.service`
- **Add:** `wedge100s-bmc-daemon.service`
  - `Type=simple`
  - `Restart=on-failure`
  - `RestartSec=5s`
  - `After=wedge100s-platform-init.service`

---

## Deliverable 4 — Link + Speed LED Verification

### Current state
`led_proc_init.soc` contains identical bytecode to AS7712-32X. LEDUP0 drives the green channel, LEDUP1 drives the amber channel per port. The bytecode is consistent with speed-based color selection (green = one speed tier, amber = another). Whether green=100G or green=lower-speed requires hardware confirmation.

### Test procedure (after D1 ships)
1. Confirm `th_led_en=1`: `ssh root@192.168.88.13 'cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en'`
2. Bring up a port with a known-speed QSFP28: `sudo config interface startup Ethernet0`
3. Observe physical front-panel LED color and link state
4. Test at 100G and (if possible) lower speed to identify the mapping

### Outcomes
- **Colors correct:** No code change. Document hardware-observed green/amber speed mapping in `notes/SUBSYSTEMS_LED.md`.
- **Colors incorrect or inverted:** Write corrected `led_proc_init.soc` bytecode with explicit mapping. Reload with `led 0 prog ... ; led 1 prog ...; led auto on` via bcmcmd.
- **All LEDs off despite `th_led_en=1`:** Investigate `QSFP_LED_POSITION` strap (gpio59=1) for chain scan direction impact; may require port-order remap table adjustment.

### Future (option d — LedPolicy)
Speed+link is a static BCM LED program. Option d (per-port policy, blink rates, user control) requires a userspace daemon that updates a BCM-accessible data table read by the LED program at runtime. This is a separate future deliverable.

---

## Complete `/run/wedge100s` Data Map

### i2c-daemon — 1s timerfd (after D2)

| File(s) | I²C operation | Trigger | Effective rate | Consumer |
|---|---|---|---|---|
| `sfp_{0-31}_present` | PCA9535 × 2 chips × 2 regs via hidraw | every tick | **1s** | `sfp.py` |
| `sfp_{N}_eeprom` upper (0x80–0xFF) | QSFP EEPROM 128B read via hidraw | insertion or retry | once per insertion | `sfp.py` |
| `sfp_{N}_eeprom` lower (0x00–0x7F, DOM) | QSFP lower-page 128B read via hidraw | `sfp_{N}_read_req` (inotify) | max staleness 20s per port; bank-interleaved (ports 0–15 even ticks, 16–31 odd ticks at 1s timer) | `sfp.py` → xcvrd |
| `sfp_{N}_read_req` / `sfp_{N}_read_resp` | request/response for DOM refresh | `sfp.py` file drop + inotify | on-demand; serviced within ~50ms | `sfp.py` |
| `sfp_{N}_write_req` / `sfp_{N}_write_ack` | EEPROM write passthrough | `sfp.py` file drop + inotify | on-demand; serviced within ~50ms | `sfp.py` |
| `sfp_{N}_lpmode` / `sfp_{N}_lpmode_req` | PCA9535 LP_MODE read + write via hidraw | every tick | **1s** | `sfp.py` |
| `syseeprom` | system EEPROM 8KB read via hidraw | startup if absent | **boot-once** | `eeprom.py` |
| `cpld_version` | CPLD sysfs read | startup if absent | **boot-once** (was 3s) | `component.py` |
| `psu{1,2}_present`, `psu{1,2}_pgood` | CPLD sysfs read | every tick | **1s** | `psu.py` |
| `led_sys1`, `led_sys2` | write-through to CPLD sysfs | inotify on file write + every tick (seed) | **~50ms** on write; 1s seed | `chassis.py`, `led_control.py` |

### bmc-daemon — 10s timerfd (after D3)

| File(s) | BMC operation | Trigger | Effective rate | Consumer |
|---|---|---|---|---|
| `thermal_{1-7}` | SSH → TMP75 sysfs (BMC i2c-3/8) | every tick | 10s | `thermal.py` |
| `fan_present` | SSH → fancpld sysfs | every tick | 10s | `fan.py` |
| `fan_{1-5}_front`, `fan_{1-5}_rear` | SSH → fancpld RPM sysfs | every tick | 10s | `fan.py` |
| `psu_{1,2}_{vin,iin,iout,pout}` | SSH → PMBus via mux | every tick | 10s | `psu.py` |
| `qsfp_int` | SSH → BMC gpio31 value | every tick | 10s | diagnostic |
| `qsfp_led_position` | SSH → BMC gpio59 value | startup / reconnect | once | future LED mapping |
| ~~`syscpld_led_ctrl`~~ | ~~SSH → syscpld 0x3c~~ | **removed** — D1 supersedes | — | — |

### Write-request latency summary

| File | Writer | Serviced by | Latency before | Latency after |
|---|---|---|---|---|
| `led_sys1`, `led_sys2` | `chassis.py`, `led_control.py` | i2c-daemon (inotify) | up to 3s | **~50ms** |
| `sfp_{N}_read_req` | `sfp.py` (DOM refresh) | i2c-daemon (inotify) | up to 3s | **~50ms** |
| `sfp_{N}_write_req` | `sfp.py` (EEPROM write) | i2c-daemon (inotify) | up to 3s | **~50ms** |
| `sfp_{N}_lpmode_req` | `sfp.py` | i2c-daemon (inotify) | up to 3s | **~50ms** |
| ~~`syscpld_led_ctrl.set`~~ | ~~`accton_wedge100s_util.py`~~ | **removed** | up to 10s | — |

---

## Files Added / Removed / Modified

### New files
| File | Description |
|---|---|
| `utils/wedge100s-bmc-auth.c` | Shared TTY key-push utility |
| `utils/clear_led_diag.sh` | BMC-side utility: clears LED test mode, enables TH LEDUP; deployed to BMC `/usr/local/bin/` by platform-init |
| `service/wedge100s-i2c-daemon.service` | Persistent i2c-daemon unit (replaces poller pair) |
| `service/wedge100s-bmc-daemon.service` | Persistent bmc-daemon unit (replaces poller pair) |

### Removed files
| File | Reason |
|---|---|
| `service/wedge100s-i2c-poller.timer` | Replaced by internal timerfd in daemon |
| `service/wedge100s-i2c-poller.service` | Replaced by persistent daemon unit |
| `service/wedge100s-bmc-poller.timer` | Replaced by internal timerfd in daemon |
| `service/wedge100s-bmc-poller.service` | Replaced by persistent daemon unit |

### Modified files
| File | Change |
|---|---|
| `utils/wedge100s-i2c-daemon.c` | Rewrite main loop: timerfd + inotify + persistent hidraw0; add daemon_init() bus recovery; cpld_version boot-once |
| `utils/wedge100s-bmc-daemon.c` | Rewrite main loop: timerfd + inotify + persistent ControlMaster; add bmc_connect() / bmc_ensure_connected(); remove syscpld_led_ctrl; qsfp_led_position boot-once |
| `utils/accton_wedge100s_util.py` | Remove `_request_led_init()` and its call from `do_install()` (D1) |
| `debian/rules` | Add `gcc` build + `unlink` clean steps for `wedge100s-bmc-auth` in `override_dh_auto_build` / `override_dh_auto_clean` |
| `debian/sonic-platform-accton-wedge100s-32x.postinst` | Replace `wedge100s-{i2c,bmc}-poller.timer` enable/start with `wedge100s-{i2c,bmc}-daemon.service` enable/start |
| `platform-init` or equivalent | Add: call wedge100s-bmc-auth, SSH probe, setup_board.sh patch step |
| `notes/SUBSYSTEMS_LED.md` | Add port LED section: syscpld 0x3c path, BCM LEDUP capabilities, D4 hardware findings |

---

## Performance Notes

- **1s i2c tick:** presence detection improves from 3s to 1s. DOM/EEPROM data rates are unchanged (governed by 20s TTL + bank-interleaving in `sfp.py`). Monitor for SSH/console delay regressions; if observed, revert to 2s or 3s via single constant change.
- **Persistent hidraw0:** eliminates ~86,400 open/close cycles per day. The explicit `cp2112_cancel()` at tick-start replaces the implicit drain-on-open; behavior is equivalent but explicit.
- **`cpld_version` boot-once:** saves one CP2112 transaction per second (86,400/day) with no functional impact.
