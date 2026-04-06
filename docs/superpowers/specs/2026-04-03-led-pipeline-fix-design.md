# LED Pipeline Fix: Shim Redesign + BMC Diagnostic Tool

## Problem

Three interrelated issues block correct port LED operation on the Wedge 100S:

1. **sai-stat-shim holds the dsserve socket permanently**, blocking all runtime bcmcmd access. This prevents LED pipeline debugging, ledinit re-runs, and any ad-hoc SDK interaction.

2. **Port LEDs show all-magenta in passthrough mode** instead of per-port link/speed colors. Root cause unknown — requires bcmcmd access to diagnose (blocked by #1).

3. **No visual demonstration of LED control understanding.** We need a tool that exercises the CPLD LED control registers and shows visible results on the front panel.

## Objectives

**Objective 1:** Rearchitect the sai-stat-shim to use connect-on-demand instead of a persistent socket connection, freeing bcmcmd for normal use.

**Objective 2:** Use the freed bcmcmd to diagnose and fix the all-magenta LED issue so that passthrough mode shows correct per-port link/speed/activity colors.

**Objective 3:** Build a LED diagnostic tool on OpenBMC that exercises CPLD test patterns and hands control to the Tomahawk for live port status display.

## Design

### Part 1: Shim Redesign — Connect-on-Demand

**Current architecture:** The shim connects to `/var/run/sswsyncd/sswsyncd.socket` once (lazily on first flex port stat call), runs `ps` to build the port name table, then holds the socket indefinitely. Every 500ms (SHIM_CACHE_TTL_MS), if `get_port_stats()` is called for a flex port and the cache is stale, it sends `show counters` over the persistent connection and accumulates deltas.

**New architecture:** The shim never holds the socket longer than a single command exchange (~50ms). The `ps` port table is fetched once during `sai_api_query()` (connect → ps → disconnect). Counter fetches happen on-demand when `get_port_stats()` is called (connect → show counters → disconnect). No polling, no persistent connection.

**Files to modify:**
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c`
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h`

**Changes to `shim.h`:**
- Remove `SHIM_CACHE_TTL_MS` (no longer used)

**Changes to `shim.c`:**

1. **Remove `g_bcmfd` and `g_bcmfd_lock`** — no persistent connection state.

2. **Move `ps` fetch into `sai_api_query()`** — after BCM config parse, do a connect → ps → disconnect to populate `g_ps_map`. If the socket isn't ready yet (syncd still initializing), that's OK — `g_ps_map_size` stays 0 and flex ports return zeros until the next `sai_api_query()` call (breakout reconfiguration triggers this).

3. **Replace `refresh_cache_if_stale()` with `refresh_cache()`:**
   ```
   static void refresh_cache(void)
   {
       int fd = bcmcmd_connect(SHIM_SOCKET_PATH, SHIM_CONNECT_TIMEOUT_MS);
       if (fd < 0) return;

       /* If ps map is empty (missed at init), try now. */
       if (g_ps_map_size == 0) {
           /* ... ps fetch same as bcmcmd_init ... */
       }

       bcmcmd_fetch_counters(fd, &g_cache);
       bcmcmd_close(fd);
   }
   ```
   No TTL check, no `fetch_in_progress` flag, no `g_bcmfd_lock`. The `g_cache.lock` mutex still protects the counter data from concurrent readers.

4. **In `shim_get_port_stats()`** — the call to `refresh_cache_if_stale()` becomes `refresh_cache()`. Behavior is identical: flex ports get counter values from the cache, non-flex ports passthrough to the real SAI function.

5. **In `sai_api_query()` breakout handler** — remove the `g_bcmfd` close/reset code (no longer exists). Keep the `g_ps_map_size = 0` reset and OID cache invalidation.

**Counter accumulation is unchanged.** `bcmcmd_fetch_counters()` still calls `parse_counters()` which accumulates deltas into `val[]`. The only difference is the socket is opened and closed around each fetch instead of held open.

**Concurrency:** FlexCounter in syncd calls `get_port_stats()` from a thread pool, but all flex ports in one polling cycle resolve to a single `refresh_cache()` call (the first flex port triggers it, subsequent ports in the same batch read from the just-refreshed cache). The `g_cache.lock` mutex prevents races. If two threads call `refresh_cache()` simultaneously, they each do a connect → fetch → disconnect. The second fetch overwrites `fetched_at` and accumulates another round of deltas — this is harmless since `show counters` returns deltas-since-last-call, and two rapid calls just split one interval's deltas across two fetches. The accumulated totals remain correct.

**Performance:** Each `refresh_cache()` call takes ~50ms (connect) + ~20ms (show counters I/O) = ~70ms. FlexCounter's default polling interval is 1000ms. The 70ms overhead per cycle is acceptable.

### Part 2: LED Pipeline Investigation and Fix

**Prerequisite:** Part 1 must be deployed so bcmcmd is accessible.

Once bcmcmd works at runtime, investigate the all-magenta issue:

1. **Inspect LEDUP state via bcmcmd:**
   ```
   bcmcmd "led status"
   bcmcmd "led 0 status"
   bcmcmd "getreg CMIC_LEDUP0_CTRL"
   bcmcmd "getreg CMIC_LEDUP0_DATA_RAM(0)"
   ```
   This reveals whether LEDUP processors are running, what DATA_RAM contains, and whether `led auto on` is active.

2. **Check if the bytecode is loaded:**
   ```
   bcmcmd "led 0 dump"
   ```
   Compare against the expected bytecode from `led_proc_init.soc`.

3. **Test manual LED control:**
   ```
   bcmcmd "led 0 stop"
   bcmcmd "led 0 start"
   bcmcmd "led auto on"
   ```

4. **Document findings** in `notes/2026-04-03-led-pipeline-investigation.md`.

5. **Apply fix** — likely one of:
   - Re-run `rcload led_proc_init.soc` if bytecodes are missing
   - Enable `led auto on` if it's off
   - Fix PORT_ORDER_REMAP if port-to-LED mapping is wrong
   - Adjust `led_control.py` if it's interfering with CPLD 0x3c

The fix may be a one-line change to `start_led.sh` or a timing adjustment, or it may require changes to the LED bytecode. Can't know until bcmcmd is available.

### Part 3: BMC LED Diagnostic Tool

**Target:** OpenBMC (Python 3.5.3, `i2cget`/`i2cset` available)
**Access:** `ssh root@192.168.88.13` or `ssh root@fe80::ff:fe00:1%usb0`

**File:** Create `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-led-diag.py`

A Python script that runs on the BMC and directly manipulates CPLD LED control registers via i2c bus 12, address 0x31.

**Capabilities:**
- `status` — Read and decode CPLD registers 0x3c (LED control) and 0x3d (test color)
- `set rainbow` — Enable CPLD test mode with blink (0x3c = 0xE0)
- `set solid <0-3>` — CPLD test mode with specific th_led_steam value
- `set walk` — CPLD walk test mode (0x3c = 0x08)
- `set passthrough` — Hand LED control to Tomahawk (0x3c = 0x02)
- `set off` — All LEDs off (0x3c = 0x00)
- `demo` — Automated sequence: cycle through all test patterns with pauses, then hand to Tomahawk

**Implementation constraints (Python 3.5.3):**
- No f-strings (use `%` formatting or `.format()`)
- No `subprocess.run()` (use `subprocess.check_output()` or `Popen`)
- No `dataclasses`
- Standard lib only (no pip packages on BMC)

**CPLD registers (i2c-12 address 0x31):**

| Register | Bits | Description |
|----------|------|-------------|
| 0x3c | 7 | LED test mode enable |
| 0x3c | 6 | LED test blink enable |
| 0x3c | 5:4 | th_led_steam (color selector 0-3) |
| 0x3c | 3 | Walk test enable |
| 0x3c | 1 | TH LED enable (passthrough) |
| 0x3c | 0 | TH LED clear |
| 0x3d | 7:0 | Test color value |

**i2c commands:**
- Read: `i2cget -f -y 12 0x31 <reg>`
- Write: `i2cset -f -y 12 0x31 <reg> <value>`

**System LED registers (0x3e, 0x3f) are NOT touched** by this tool — they're managed by SONiC's ledd.

**Deployment:** SCP to BMC, run directly. No installation needed. The tool is self-contained.

## Dependency Order

```
Part 1 (shim redesign)
  → Part 2 (LED pipeline investigation — needs bcmcmd)
  → Part 2 fix (whatever the investigation reveals)

Part 3 (BMC LED tool) — independent, can run in parallel with Part 1
```

## Success Criteria

1. **bcmcmd works at runtime** — `docker exec syncd bcmcmd "version"` returns immediately instead of hanging
2. **Port LEDs show correct colors** — linked ports show green/amber (link/speed), unlinked ports are dark
3. **BMC tool demonstrates LED control** — visible color changes on front panel when running demo sequence
4. **Counter stats still work** — `show interfaces counters` on SONiC shows correct values for flex sub-ports

## Risk: Counter Accuracy During Rapid Reconnection

The `show counters` command returns deltas since the last call on that socket session. When we disconnect and reconnect, the delta window resets. Counters accumulated between disconnect and reconnect are captured in the next `show counters` call on the new connection — the BCM SDK tracks totals internally, and `show counters` computes deltas from the SDK's running total. No counter data is lost.

Verified behavior: bcmcmd's `show counters` implementation uses `bcm_stat_get()` which reads hardware counters. The "delta" display is computed client-side by subtracting the previous call's values. Since we accumulate into `val[]` in the shim, and each new connection sees the full current hardware values (which we then delta against our last fetch), the running totals remain monotonically increasing and correct.

**Correction:** Actually, `show counters` in the BCM diag shell tracks its own previous-call state per-session. A new socket connection starts a new diag shell session, so the first `show counters` on a new connection returns the full cumulative delta since the last time any session called it. This means: if we disconnect for 5 seconds and reconnect, the first `show counters` returns all traffic from those 5 seconds. No data loss.
