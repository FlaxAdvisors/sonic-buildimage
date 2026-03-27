# Continuation Prompt — 2026-03-27 session end

Branch: `wedge100s`. Target: `admin@192.168.88.12`.

---

## Task A — LP_MODE deassert readiness lock in wedge100s-i2c-daemon.c

**Problem:** `refresh_eeprom_lower_page()` can be called at any time — including
immediately after `set_lpmode_hidraw(port, 0)` runs.  When the cache file is absent
(manual `rm`, first insertion, or daemon_init clearing files), the function reads the
upper page directly from hardware.  If this fires within ~2 s of LP_MODE deassert the
module MCU is still resetting: the read returns zeros, and the function writes a
256-byte cache with byte 220 = 0x00, vendor = "\\0\\0\\0…", checksums invalid.  That
corrupt upper page persists **indefinitely** (DOM TTL only refreshes bytes 0-127; upper
page is never re-fetched unless the module is re-plugged).

This is the exact failure mode that corrupted our test bench transceivers in session
2026-03-27.  The BEWARE_EEPROM.md note currently says "don't do that" — a production
operator actively debugging a flaky transceiver will inevitably `rm` the cache file
to force a fresh read and silently brick it.

**Fix:** add a per-port deassert timestamp array; gate upper-page reads behind it.

### Exact changes to `wedge100s-i2c-daemon.c`

**1. Add global after `g_hidraw_fd` (line 137):**

```c
/*
 * Per-port LP_MODE deassert timestamp (CLOCK_MONOTONIC, nanoseconds).
 * Set to clock_gettime() when set_lpmode_hidraw(port, 0) succeeds.
 * refresh_eeprom_lower_page() refuses to read the upper page from
 * hardware until LP_MODE_READY_NS has elapsed, preventing the race where
 * an EEPROM read fires while the module MCU is still resetting.
 * Initialised to 0 (always-expired) so absent/legacy ports are unaffected.
 */
#define LP_MODE_READY_NS  2500000000LL   /* 2.5 s: SFF-8636 module MCU init */
static long long g_lp_deassert_ns[NUM_PORTS];  /* 0 = no recent deassert */
```

Add `#include <time.h>` to the include block if not already present (it isn't — only
`sys/time.h` is included; `clock_gettime` requires `time.h`).

**2. Add a helper after the global (before `refresh_eeprom_lower_page`):**

```c
/* Return current CLOCK_MONOTONIC time in nanoseconds. */
static long long now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}
```

**3. Record timestamp in `set_lpmode_hidraw()` on successful deassert.**

`set_lpmode_hidraw()` is at line 505.  It returns 0 on success.  The call sites that
matter are:
- `poll_lpmode_hidraw()` around line 799: `if (set_lpmode_hidraw(port, 0) == 0)`
- `daemon_init()` loop: `if (set_lpmode_hidraw(p, 0) < 0)`

Add the timestamp record **inside `set_lpmode_hidraw()` itself**, so every call site
is covered automatically.  The function already has the `lpmode` parameter; add before
the `return 0` at the end of the success path:

```c
    /* Record deassert time so refresh_eeprom_lower_page() can gate
     * upper-page reads until the module MCU has had time to initialise. */
    if (!lpmode)
        g_lp_deassert_ns[port] = now_ns();
    return 0;
```

`set_lpmode_hidraw()` has two success-return paths (one for deassert, one for assert).
Only stamp on `!lpmode`.  The function signature is:
```c
static int set_lpmode_hidraw(int port, int lpmode)
```
The final `return 0` at ~line 565 is the only success exit.  Add the stamp there,
guarded by `!lpmode`.

**4. Gate the upper-page hardware read in `refresh_eeprom_lower_page()` (line 482).**

The dangerous path is when the cache file is absent and the function falls through to:
```c
} else {
    uint8_t upper_addr = 0x80;
    cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
}
write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
```

Replace the `else` block:

```c
        } else {
            /* Upper page must be read from hardware — but only after the module
             * MCU has had time to initialise following LP_MODE deassert.
             * If the lock has not expired, skip the write entirely and return 0
             * so the caller retries on the next tick. */
            long long elapsed = now_ns() - g_lp_deassert_ns[port];
            if (g_lp_deassert_ns[port] != 0 && elapsed < LP_MODE_READY_NS) {
                mux_deselect(mux_addr);
                return 0;   /* not ready; caller will retry next tick */
            }
            uint8_t upper_addr = 0x80;
            int ur = cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
            if (ur != 128) {
                /* Upper page read failed; do not write a cache with zero upper page.
                 * Return 0 so caller retries next tick. */
                mux_deselect(mux_addr);
                return 0;
            }
        }
```

Note: also added a return-value check on the upper-page `cp2112_write_read` — it was
previously unchecked.  A failed read now skips the file write rather than persisting
zeros.

### Verification after deploy

```bash
# Restart daemon and immediately force a cache miss on one optical port
ssh admin@192.168.88.12 'sudo systemctl restart wedge100s-i2c-daemon && \
  sudo rm /run/wedge100s/sfp_21_eeprom && sleep 1 && \
  xxd -s 220 -l 1 /run/wedge100s/sfp_21_eeprom 2>/dev/null || echo "not written yet (correct)"'

# After 3+ seconds the file should appear with correct byte 220
ssh admin@192.168.88.12 'sleep 4 && xxd -s 220 -l 1 /run/wedge100s/sfp_21_eeprom'
# Expected: 0c  (for Arista SR4-100G at Ethernet84, sfp index 21)
```

---

## Task B — Remove dead postinst legacy unit migration loop

**File:** `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`

**Problem:** The postinst still contains a migration loop that silently tries to
disable the old `wedge100s-bmc-poller.*` and `wedge100s-i2c-poller.*` timer+oneshot
units.  These units were replaced in commits `88cf7f1b6` (D2) and `ce2e4b0e7` (D3)
and will never exist on any current or future install.  The loop runs on every `dpkg -i`
and every first boot, emitting four "disabled legacy unit X (if present)" lines that
pollute the boot log and confuse operators.

**Fix:** delete the entire block.  Grep for it first to confirm exact lines:

```bash
grep -n "bmc-poller\|i2c-poller" \
  platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
```

Expected to find something like:
```sh
for unit in wedge100s-bmc-poller.timer wedge100s-bmc-poller.service \
            wedge100s-i2c-poller.timer wedge100s-i2c-poller.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
    echo "wedge100s postinst: disabled legacy unit $unit (if present)"
done
```

Delete the entire `for` loop.  No replacement needed.

Rebuild and deploy:
```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb-clean
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 'sudo systemctl stop pmon && \
  sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && \
  sudo systemctl start pmon'
```

Verify the four lines are gone from postinst output.

---

## Task C (after A+B) — Run the test suite

```bash
cd tests && python3 run_tests.py 2>&1 | tee timing.log
```

Known expected gap: `stage_20_traffic` needs a static route or BGP session for peer
route 10.0.1.0/32 — investigate if it fails.

---

## Current hardware state

- `wedge100s-i2c-daemon` + `wedge100s-bmc-daemon` + `pmon` running
- 14/32 ports populated (9 DAC cables, 5 optical); all optical showing DOM correctly
- BREAKOUT_CFG seeded (32 entries)
- LED SYS1=green (D1 clear_led_diag ran)
- Daemon restart stale-lpmode fix is in (Task A adds the MCU readiness lock on top)
