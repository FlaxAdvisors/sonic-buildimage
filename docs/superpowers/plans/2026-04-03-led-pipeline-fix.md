# LED Pipeline Fix: Shim Redesign + BMC Diagnostic Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Free the bcmcmd socket from the sai-stat-shim's persistent hold, enabling runtime LED pipeline debugging, and build a SONiC-side LED diagnostic tool that exercises CPLD control via the bmc-daemon path.

**Architecture:** The shim switches from persistent-connection to connect-on-demand: each counter fetch opens the socket, runs one command, closes. The LED diagnostic tool runs on SONiC and sends CPLD write commands to the BMC via the bmc-daemon's `.set` file dispatch, then reads back actual register values through the same path. This exercises both sides of the SONiC↔BMC communication and reports intended vs actual values. Part 2 (LED investigation) is an interactive hardware session that depends on Part 1 being deployed.

**Tech Stack:** C (shim + bmc-daemon, gcc, pthreads), Python 3 (SONiC LED diag tool), bcmcmd (SONiC syncd container)

---

## File Structure

### Part 1: Shim Redesign
| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h` | Remove `SHIM_CACHE_TTL_MS` define |
| Modify | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c` | Remove persistent socket, add connect-on-demand `refresh_cache()` |
| Keep | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c` | No changes — connect/ps/fetch_counters/close API unchanged |
| Keep | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c` | No changes |
| Keep | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/test_parser.c` | Existing tests — must still pass after changes |
| Keep | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile` | No changes needed |

### Part 3: LED Diagnostic Tool (SONiC-side, via bmc-daemon)
| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c` | Add `led_ctrl_write.set` and `led_color_read.set` dispatch handlers |
| Create | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py` | SONiC-side tool: sends CPLD commands via daemon, reports intended vs actual |
| Output | `/run/wedge100s/cpld_led_ctrl` | Daemon writes readback of 0x3c after any LED operation |
| Output | `/run/wedge100s/cpld_led_color` | Daemon writes readback of 0x3d |
| Output | `/run/wedge100s/led_diag_results.json` | SONiC tool writes structured intended-vs-actual results |

### Part 2: LED Investigation (no files predetermined)
| Action | File | Responsibility |
|--------|------|----------------|
| Create | `notes/2026-04-03-led-pipeline-investigation.md` | Document findings from bcmcmd investigation |

---

## Task 1: Remove Persistent Socket State from shim.h

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h:84`

- [ ] **Step 1: Remove SHIM_CACHE_TTL_MS from shim.h**

In `shim.h`, delete the `SHIM_CACHE_TTL_MS` define on line 84. The line to remove:

```c
#define SHIM_CACHE_TTL_MS        500
```

The remaining defines (`SHIM_SOCKET_PATH`, `SHIM_BCM_CONFIG_ENV`, `SHIM_CONNECT_TIMEOUT_MS`, `SHIM_MAX_PORTS`, etc.) stay as-is.

- [ ] **Step 2: Remove `fetch_in_progress` from `counter_cache_t` in shim.h**

In `shim.h`, in the `counter_cache_t` struct (around line 118), remove the `fetch_in_progress` field. Change:

```c
typedef struct {
    port_row_t      rows[SHIM_MAX_PORTS];
    int             n_rows;
    struct timespec fetched_at;    /* CLOCK_MONOTONIC */
    int             fetch_in_progress;  /* 1 while socket I/O in progress */
    pthread_mutex_t lock;
} counter_cache_t;
```

To:

```c
typedef struct {
    port_row_t      rows[SHIM_MAX_PORTS];
    int             n_rows;
    struct timespec fetched_at;    /* CLOCK_MONOTONIC */
    pthread_mutex_t lock;
} counter_cache_t;
```

- [ ] **Step 3: Verify test_parser still compiles**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && make clean && make test_parser
```
Expected: compiles successfully (test_parser doesn't use `SHIM_CACHE_TTL_MS` or `fetch_in_progress`).

- [ ] **Step 4: Run test_parser**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && ./test_parser
```
Expected: `6 passed, 0 failed`

- [ ] **Step 5: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h
git commit -m "refactor(shim): remove SHIM_CACHE_TTL_MS and fetch_in_progress from header

Connect-on-demand redesign no longer uses TTL-based staleness checks
or in-progress flags. The cache mutex alone protects concurrent access."
```

---

## Task 2: Rewrite shim.c to Connect-on-Demand

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c`

- [ ] **Step 1: Remove persistent socket globals**

Remove these two lines (around lines 43-45 in shim.c):

```c
/* bcmcmd socket fd (-1 = not connected). */
static int g_bcmfd = -1;
static pthread_mutex_t g_bcmfd_lock = PTHREAD_MUTEX_INITIALIZER;
```

- [ ] **Step 2: Move ps fetch into `bcmcmd_init` and make it disconnect**

Replace the current `bcmcmd_init()` function (lines 108-133) with one that connects, fetches ps, populates g_ps_map, and **disconnects** (returning void instead of fd):

```c
/* Connect to bcmcmd socket, run 'ps', populate g_ps_map, disconnect.
 * Safe to call multiple times (idempotent). */
static void bcmcmd_init_ps(void)
{
    const char *sock = SHIM_SOCKET_PATH;
    int fd = bcmcmd_connect(sock, SHIM_CONNECT_TIMEOUT_MS);
    if (fd < 0) {
        syslog(LOG_WARNING, "shim: cannot connect to bcmcmd socket %s: %m", sock);
        return;
    }

    /* Build sdk_port -> port_name table. */
    int sdk_ports[SHIM_MAX_PORTS];
    char names[SHIM_MAX_PORTS][SHIM_PORT_NAME_LEN];
    int n = bcmcmd_ps(fd, sdk_ports, names, SHIM_MAX_PORTS);
    bcmcmd_close(fd);

    if (n < 0) {
        syslog(LOG_WARNING, "shim: bcmcmd 'ps' failed");
        return;
    }
    g_ps_map_size = n;
    for (int i = 0; i < n; i++) {
        g_ps_map[i].sdk_port = sdk_ports[i];
        strncpy(g_ps_map[i].name, names[i], SHIM_PORT_NAME_LEN - 1);
    }
    syslog(LOG_INFO, "shim: bcmcmd ps fetched %d ports (connect-on-demand)", n);
}
```

- [ ] **Step 3: Remove `cache_is_stale()` and replace `refresh_cache_if_stale()` with `refresh_cache()`**

Delete the `cache_is_stale()` function (lines 137-144) and replace `refresh_cache_if_stale()` (lines 147-171) with:

```c
/* Refresh counter cache via a transient bcmcmd connection.
 * Each call: connect -> show counters -> disconnect (~70ms).
 * The g_cache.lock mutex protects concurrent readers.
 * If two threads call simultaneously, both do independent fetches —
 * deltas accumulate correctly since bcmcmd tracks totals internally. */
static void refresh_cache(void)
{
    int fd = bcmcmd_connect(SHIM_SOCKET_PATH, SHIM_CONNECT_TIMEOUT_MS);
    if (fd < 0) return;

    /* If ps map is empty (syncd wasn't ready at init), try now. */
    if (g_ps_map_size == 0) {
        int sdk_ports[SHIM_MAX_PORTS];
        char names[SHIM_MAX_PORTS][SHIM_PORT_NAME_LEN];
        int n = bcmcmd_ps(fd, sdk_ports, names, SHIM_MAX_PORTS);
        if (n > 0) {
            g_ps_map_size = n;
            for (int i = 0; i < n; i++) {
                g_ps_map[i].sdk_port = sdk_ports[i];
                strncpy(g_ps_map[i].name, names[i], SHIM_PORT_NAME_LEN - 1);
            }
            syslog(LOG_INFO, "shim: late ps fetch got %d ports", n);
        }
    }

    bcmcmd_fetch_counters(fd, &g_cache);
    bcmcmd_close(fd);
}
```

- [ ] **Step 4: Update `shim_get_port_stats()` call site**

In `shim_get_port_stats()`, change the call from `refresh_cache_if_stale()` to `refresh_cache()`. Find (around line 260):

```c
    /* Flex path: use bcmcmd counter cache. */
    refresh_cache_if_stale();
```

Replace with:

```c
    /* Flex path: use bcmcmd counter cache. */
    refresh_cache();
```

- [ ] **Step 5: Update `sai_api_query()` — remove persistent fd management, call `bcmcmd_init_ps()`**

In `sai_api_query()`, replace the block that manages `g_bcmfd` (lines 362-366):

```c
    /* Rebuild ps map (port layout may have changed) — reconnect to bcmcmd. */
    pthread_mutex_lock(&g_bcmfd_lock);
    if (g_bcmfd >= 0) { bcmcmd_close(g_bcmfd); g_bcmfd = -1; }
    g_ps_map_size = 0;
    pthread_mutex_unlock(&g_bcmfd_lock);
```

With:

```c
    /* Rebuild ps map (port layout may have changed). */
    g_ps_map_size = 0;
    bcmcmd_init_ps();
```

- [ ] **Step 6: Also remove the `g_bcmfd_lock` init from the one-time init block**

In the `if (!g_initialised)` block (around line 370), the `pthread_mutex_init` calls include `g_oids.lock` and `g_cache.lock`. Neither `g_bcmfd_lock` nor `g_bcmfd` references should remain anywhere. Verify no references remain with a grep:

Run:
```bash
grep -n 'g_bcmfd\|g_bcmfd_lock\|cache_is_stale\|refresh_cache_if_stale\|fetch_in_progress\|SHIM_CACHE_TTL_MS' platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h
```
Expected: no matches.

- [ ] **Step 7: Build the full shim library**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && make clean && make
```
Expected: `libsai-stat-shim.so` builds with no errors and no warnings related to our changes.

- [ ] **Step 8: Run test_parser**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && make test_parser && ./test_parser
```
Expected: `6 passed, 0 failed` — parser logic is unchanged.

- [ ] **Step 9: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c
git commit -m "refactor(shim): connect-on-demand instead of persistent bcmcmd socket

Each refresh_cache() call opens a transient connection (~70ms), runs
'show counters', and disconnects. This frees the bcmcmd socket for
runtime use (LED debugging, bcmcmd CLI, ledinit re-runs).

ps table is fetched once during sai_api_query() and lazily retried
in refresh_cache() if syncd wasn't ready at init time.

Counter accumulation is unchanged — deltas still accumulate into val[]."
```

---

## Task 3: Deploy Shim and Verify on Target Hardware

**Files:**
- No file changes — deployment and verification only

**Prerequisites:** Tasks 1-2 complete (shim compiles cleanly).

- [ ] **Step 1: Build the platform .deb**

Run:
```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```
Expected: .deb builds successfully in `target/debs/trixie/`.

If .deb build is not feasible (long build time), use direct file deployment instead:
```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/libsai-stat-shim.so admin@192.168.88.12:~/
```

- [ ] **Step 2: Deploy to target**

SSH to target and replace the shim library:
```bash
ssh admin@192.168.88.12 'sudo systemctl stop pmon'
ssh admin@192.168.88.12 'sudo docker cp ~/libsai-stat-shim.so syncd:/usr/lib/libsai-stat-shim.so'
ssh admin@192.168.88.12 'sudo docker exec syncd supervisorctl restart syncd'
```

Wait ~30 seconds for syncd to reinitialize.

- [ ] **Step 3: Verify bcmcmd is now accessible**

Run:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "version"'
```
Expected: Returns BCM SDK version string **immediately** (within 1-2 seconds) instead of hanging. This is the primary success criterion for Part 1.

- [ ] **Step 4: Verify counter stats still work**

Run:
```bash
ssh admin@192.168.88.12 'show interfaces counters'
```
Expected: Counters show non-zero values for linked ports (especially flex sub-ports like Ethernet120-127). Compare before/after to confirm no regression.

- [ ] **Step 5: Verify shim syslog messages**

Run:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd grep "shim:" /var/log/syslog | tail -20'
```
Expected: See `"shim: bcmcmd ps fetched N ports (connect-on-demand)"` and no error messages about persistent connections.

- [ ] **Step 6: Start pmon back up**

```bash
ssh admin@192.168.88.12 'sudo systemctl start pmon'
```

- [ ] **Step 7: Document deployment result**

Record pass/fail for each verification step. If bcmcmd hangs, check `dmesg` and syncd logs for socket errors.

---

## Task 4: LED Pipeline Investigation via bcmcmd

**Files:**
- Create: `notes/2026-04-03-led-pipeline-investigation.md`

**Prerequisites:** Task 3 complete (bcmcmd accessible at runtime).

This task is an **interactive hardware investigation**. The steps below are the investigation protocol — the findings will determine what fix is needed. The fix itself cannot be predetermined.

- [ ] **Step 1: Check LEDUP processor state**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led status"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 0 status"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 1 status"'
```
Record: Are LEDUP0/LEDUP1 running? What state are they in?

- [ ] **Step 2: Check LEDUP control registers**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "getreg CMIC_LEDUP0_CTRL"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "getreg CMIC_LEDUP1_CTRL"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "getreg CMIC_LEDUP0_DATA_RAM(0)"'
```
Record: Is LEDUP_EN set? What's in DATA_RAM?

- [ ] **Step 3: Check if bytecode is loaded**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 0 dump"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 1 dump"'
```
Compare against the expected bytecode from `led_proc_init.soc`. Record: Is the program loaded or all zeros?

- [ ] **Step 4: Check led auto mode**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led auto on"'
```
Then check if port LEDs change from all-magenta. Record the result.

- [ ] **Step 5: Test stop/start cycle**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 0 stop"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 0 start"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 1 stop"'
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "led 1 start"'
```
Record: Does the stop/start cycle change LED behavior?

- [ ] **Step 6: Try reloading SOC file**

Run on target:
```bash
ssh admin@192.168.88.12 'sudo docker exec syncd bcmcmd "rcload /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc"'
```
Record: Does this fix the all-magenta issue?

- [ ] **Step 7: Document findings**

Write all findings to `notes/2026-04-03-led-pipeline-investigation.md` with:
- Exact command output for each step
- Root cause determination
- What fix is needed (if any)
- Mark items as `(verified on hardware 2026-04-03)`

- [ ] **Step 8: Implement and deploy fix**

Based on findings, implement the fix. This may be:
- A one-line change to `start_led.sh`
- Enabling `led auto on` in the init sequence
- Fixing `PORT_ORDER_REMAP` configuration
- Adjusting `led_control.py`
- Modifying LED bytecode

The specific implementation depends on Step 7 findings. Commit the fix with a descriptive message referencing the investigation notes.

---

## Task 5: Add LED Control Write Dispatch to bmc-daemon

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c:239-280`

**Prerequisites:** None — independent of shim work.

The bmc-daemon already handles `cpld_led_ctrl.set` (reads 0x3c). We add:
- `led_ctrl_write.set` — reads desired value from file content, writes to CPLD 0x3c, reads back, stores in `cpld_led_ctrl`
- `led_color_read.set` — reads CPLD 0x3d, stores in `cpld_led_color`

- [ ] **Step 1: Restructure dispatch_write_requests to read .set file content before unlink**

The current code unlinks the `.set` file before processing. We need to read the file content first so handlers like `led_ctrl_write.set` can extract the desired value.

In `dispatch_write_requests()`, find the block (around line 256):

```c
        snprintf(path, sizeof(path), RUN_DIR "/%s", ev->name);
        unlink(path);
```

Replace with:

```c
        snprintf(path, sizeof(path), RUN_DIR "/%s", ev->name);

        /* Read .set file content before unlink (some handlers need the value). */
        char setfile_content[64] = "";
        {
            FILE *sf = fopen(path, "r");
            if (sf) {
                if (!fgets(setfile_content, sizeof(setfile_content), sf))
                    setfile_content[0] = '\0';
                setfile_content[strcspn(setfile_content, "\r\n")] = '\0';
                fclose(sf);
            }
        }
        unlink(path);
```

- [ ] **Step 2: Add `led_ctrl_write.set` handler**

After the existing `cpld_led_ctrl.set` handler block (around line 269), add the new handler. Find:

```c
            continue;
        }

        for (i = 0; i < sizeof(write_requests) / sizeof(write_requests[0]); i++) {
```

Insert before the `for` loop:

```c
        /* led_ctrl_write.set → write value to CPLD 0x3c, read back */
        if (strcmp(ev->name, "led_ctrl_write.set") == 0) {
            int desired, actual;
            char *end;
            errno = 0;
            desired = (int)strtol(setfile_content, &end, 0);
            if (end == setfile_content || errno != 0) {
                syslog(LOG_WARNING, "wedge100s-bmc-daemon: bad value in led_ctrl_write.set: '%s'",
                       setfile_content);
                continue;
            }
            syslog(LOG_INFO, "wedge100s-bmc-daemon: writing CPLD 0x3c = 0x%02x", desired);
            if (bmc_ensure_connected() == 0) {
                char bmc_cmd[128];
                snprintf(bmc_cmd, sizeof(bmc_cmd),
                         "i2cset -f -y 12 0x31 0x3c 0x%02x", desired & 0xFF);
                bmc_run(bmc_cmd);
                if (bmc_read_int("i2cget -f -y 12 0x31 0x3c", 0, &actual) == 0) {
                    snprintf(path, sizeof(path), RUN_DIR "/cpld_led_ctrl");
                    write_file(path, actual);
                }
            }
            continue;
        }

        /* led_color_read.set → read CPLD 0x3d, write to cpld_led_color */
        if (strcmp(ev->name, "led_color_read.set") == 0) {
            int val;
            syslog(LOG_INFO, "wedge100s-bmc-daemon: reading CPLD 0x3d");
            if (bmc_ensure_connected() == 0 &&
                bmc_read_int("i2cget -f -y 12 0x31 0x3d", 0, &val) == 0) {
                snprintf(path, sizeof(path), RUN_DIR "/cpld_led_color");
                write_file(path, val);
            }
            continue;
        }

```

- [ ] **Step 3: Build bmc-daemon**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils && gcc -O2 -Wall -Wextra -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c
```
Expected: compiles with no errors. Warnings about unused variables in main are acceptable.

- [ ] **Step 4: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c
git commit -m "feat(bmc-daemon): add led_ctrl_write and led_color_read dispatch

led_ctrl_write.set: reads desired 0x3c value from file content,
writes via i2cset, reads back via i2cget, stores in cpld_led_ctrl.
led_color_read.set: reads 0x3d, stores in cpld_led_color.

Also reads .set file content before unlink so write handlers can
extract values from callers."
```

---

## Task 6: SONiC-side LED Diagnostic Tool (via bmc-daemon)

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py`

**Prerequisites:** Task 5 (bmc-daemon dispatch handlers).

This tool runs on SONiC, sends CPLD commands via bmc-daemon `.set` file dispatch, reads back results from `/run/wedge100s/`, compares intended vs actual, and saves structured results to `/run/wedge100s/led_diag_results.json`.

- [ ] **Step 1: Write the SONiC-side LED diagnostic tool**

Create `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py`:

```python
#!/usr/bin/env python3
"""wedge100s-led-diag-bmc.py -- SONiC-side LED diagnostic tool (via bmc-daemon).

Exercises CPLD LED control registers by sending commands through the
wedge100s-bmc-daemon's .set file dispatch. Reads back actual values via
the daemon's /run/wedge100s/ output files. Reports intended vs actual
to verify both the SONiC→BMC communication path and CPLD register writes.

Usage:
    wedge100s-led-diag-bmc.py status
    wedge100s-led-diag-bmc.py set rainbow
    wedge100s-led-diag-bmc.py set solid <0-3>
    wedge100s-led-diag-bmc.py set walk
    wedge100s-led-diag-bmc.py set passthrough
    wedge100s-led-diag-bmc.py set off
    wedge100s-led-diag-bmc.py demo
"""

import json
import os
import sys
import time

RUN_DIR = "/run/wedge100s"
RESULTS_PATH = os.path.join(RUN_DIR, "led_diag_results.json")

# CPLD 0x3c preset values
PATTERNS = {
    "off":         0x00,
    "passthrough": 0x02,
    "walk":        0x08,
    "solid0":      0x80,
    "solid1":      0x90,
    "solid2":      0xA0,
    "solid3":      0xB0,
    "rainbow":     0xE0,
}

# 0x3c bit field decoders
def decode_led_ctrl(val):
    """Decode 0x3c register into human-readable fields."""
    return {
        "raw": "0x%02x" % val,
        "test_mode_en": bool(val & 0x80),
        "test_blink_en": bool(val & 0x40),
        "th_led_steam": (val >> 4) & 0x03,
        "walk_test_en": bool(val & 0x08),
        "th_led_en": bool(val & 0x02),
        "th_led_clear": bool(val & 0x01),
    }


def daemon_write_led_ctrl(value):
    """Write a value to CPLD 0x3c via bmc-daemon dispatch.

    Writes the desired value to /run/wedge100s/led_ctrl_write.set.
    The bmc-daemon picks this up via inotify, writes to CPLD, reads back,
    and stores the readback in /run/wedge100s/cpld_led_ctrl.
    """
    setfile = os.path.join(RUN_DIR, "led_ctrl_write.set")
    with open(setfile, "w") as f:
        f.write("0x%02x\n" % (value & 0xFF))


def daemon_read_led_ctrl():
    """Trigger a CPLD 0x3c read via bmc-daemon dispatch.

    Writes /run/wedge100s/cpld_led_ctrl.set to trigger a read.
    Returns None — caller must poll cpld_led_ctrl file for result.
    """
    setfile = os.path.join(RUN_DIR, "cpld_led_ctrl.set")
    with open(setfile, "w") as f:
        f.write("\n")


def daemon_read_led_color():
    """Trigger a CPLD 0x3d read via bmc-daemon dispatch."""
    setfile = os.path.join(RUN_DIR, "led_color_read.set")
    with open(setfile, "w") as f:
        f.write("\n")


def read_result_file(name, timeout=5.0):
    """Read an integer result from /run/wedge100s/<name>.

    Polls until the file mtime changes or timeout. Returns int or None.
    """
    path = os.path.join(RUN_DIR, name)

    # Record current mtime (if file exists) so we can detect updates
    old_mtime = 0
    try:
        old_mtime = os.path.getmtime(path)
    except OSError:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            mtime = os.path.getmtime(path)
            if mtime > old_mtime:
                with open(path) as f:
                    return int(f.read().strip())
        except (OSError, ValueError):
            pass
        time.sleep(0.1)

    # Timeout — try reading whatever's there
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_and_verify(value, label=""):
    """Write a CPLD 0x3c value via daemon, read back, compare.

    Returns dict with intended, actual, match fields.
    """
    daemon_write_led_ctrl(value)
    actual = read_result_file("cpld_led_ctrl")

    result = {
        "label": label,
        "intended": "0x%02x" % value,
        "actual": "0x%02x" % actual if actual is not None else "TIMEOUT",
        "match": actual == value if actual is not None else False,
    }

    status = "PASS" if result["match"] else "FAIL"
    print("  %s: intended=0x%02x actual=%s  [%s]" % (
        label or "write", value,
        "0x%02x" % actual if actual is not None else "TIMEOUT",
        status))

    return result


def cmd_status():
    """Read and decode CPLD LED registers via bmc-daemon."""
    daemon_read_led_ctrl()
    daemon_read_led_color()

    ctrl = read_result_file("cpld_led_ctrl")
    color = read_result_file("cpld_led_color")

    if ctrl is None:
        print("ERROR: could not read CPLD 0x3c via daemon (timeout)")
        print("Is wedge100s-bmc-daemon running? Check: systemctl status wedge100s-bmc-daemon")
        sys.exit(1)

    info = decode_led_ctrl(ctrl)
    print("=== CPLD LED Control (0x3c) via bmc-daemon ===")
    print("  raw value:      %s" % info["raw"])
    print("  test_mode_en:   %s" % info["test_mode_en"])
    print("  test_blink_en:  %s" % info["test_blink_en"])
    print("  th_led_steam:   %d" % info["th_led_steam"])
    print("  walk_test_en:   %s" % info["walk_test_en"])
    print("  th_led_en:      %s" % info["th_led_en"])
    print("  th_led_clear:   %s" % info["th_led_clear"])
    if color is not None:
        print("\n=== CPLD Test Color (0x3d) ===")
        print("  raw value:      0x%02x" % color)

    if info["th_led_en"] and not info["test_mode_en"]:
        print("\nMode: PASSTHROUGH (Tomahawk controls LEDs)")
    elif info["test_mode_en"] and info["test_blink_en"]:
        print("\nMode: RAINBOW (test mode + blink)")
    elif info["test_mode_en"]:
        print("\nMode: TEST SOLID (th_led_steam=%d)" % info["th_led_steam"])
    elif info["walk_test_en"]:
        print("\nMode: WALK TEST")
    elif ctrl == 0:
        print("\nMode: ALL OFF")
    else:
        print("\nMode: UNKNOWN (0x%02x)" % ctrl)


def cmd_set(mode, steam=None):
    """Set CPLD LED mode via bmc-daemon, verify readback."""
    if mode == "solid" and steam is not None:
        key = "solid%d" % steam
        label = "solid steam=%d" % steam
    else:
        key = mode
        label = mode

    if key not in PATTERNS:
        print("ERROR: unknown mode '%s'" % key)
        sys.exit(1)

    value = PATTERNS[key]
    print("Setting LED mode: %s (0x3c = 0x%02x)" % (label, value))
    result = write_and_verify(value, label)
    if not result["match"]:
        sys.exit(1)


def cmd_demo():
    """Automated demo: cycle through all patterns, verify each, save results."""
    sequence = [
        ("off", 0x00),
        ("solid steam=0", 0x80),
        ("solid steam=1", 0x90),
        ("solid steam=2", 0xA0),
        ("solid steam=3", 0xB0),
        ("rainbow", 0xE0),
        ("walk", 0x08),
        ("passthrough", 0x02),
    ]

    results = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test": "led_cpld_demo",
        "steps": [],
    }

    all_pass = True
    for label, value in sequence:
        print("\n--- %s ---" % label)
        step = write_and_verify(value, label)
        results["steps"].append(step)
        if not step["match"]:
            all_pass = False
        if label != "passthrough":
            time.sleep(3)

    results["all_pass"] = all_pass
    print("\n=== Summary ===")
    print("Total: %d steps, %d passed, %d failed" % (
        len(results["steps"]),
        sum(1 for s in results["steps"] if s["match"]),
        sum(1 for s in results["steps"] if not s["match"]),
    ))

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print("Results saved to %s" % RESULTS_PATH)

    if not all_pass:
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "set":
        if len(sys.argv) < 3:
            print("Usage: %s set <rainbow|solid|walk|passthrough|off>" % sys.argv[0])
            sys.exit(1)
        mode = sys.argv[2]
        steam = None
        if mode == "solid":
            if len(sys.argv) < 4:
                print("Usage: %s set solid <0-3>" % sys.argv[0])
                sys.exit(1)
            steam = int(sys.argv[3])
            if steam < 0 or steam > 3:
                print("ERROR: steam must be 0-3")
                sys.exit(1)
        cmd_set(mode, steam)
    elif cmd == "demo":
        cmd_demo()
    else:
        print("Unknown command: %s" % cmd)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Syntax check**

Run:
```bash
python3 -c "import ast; ast.parse(open('platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py').read())"
```
Expected: no output (clean parse).

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py
git commit -m "feat(led-diag): add SONiC-side LED diagnostic tool via bmc-daemon

Exercises CPLD LED control registers (0x3c) through the bmc-daemon
.set file dispatch path. Each operation writes intended value, reads
back actual via daemon, compares and reports PASS/FAIL.

Commands: status, set rainbow/solid/walk/passthrough/off, demo.
Demo runs full pattern sequence and saves intended-vs-actual results
to /run/wedge100s/led_diag_results.json."
```

---

## Task 7: Deploy and Verify LED Diagnostic Tool on Target

**Files:**
- No file changes — deployment and verification only

**Prerequisites:** Tasks 5-6 complete, bmc-daemon running on target.

- [ ] **Step 1: Deploy updated bmc-daemon binary**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon admin@192.168.88.12:~/
ssh admin@192.168.88.12 'sudo systemctl stop wedge100s-bmc-daemon'
ssh admin@192.168.88.12 'sudo cp ~/wedge100s-bmc-daemon /usr/local/bin/wedge100s-bmc-daemon'
ssh admin@192.168.88.12 'sudo systemctl start wedge100s-bmc-daemon'
```

- [ ] **Step 2: Deploy SONiC-side tool**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag-bmc.py admin@192.168.88.12:~/
ssh admin@192.168.88.12 'sudo cp ~/wedge100s-led-diag-bmc.py /usr/local/bin/wedge100s-led-diag-bmc.py && sudo chmod +x /usr/local/bin/wedge100s-led-diag-bmc.py'
```

- [ ] **Step 3: Verify status read via daemon path**

```bash
ssh admin@192.168.88.12 'sudo wedge100s-led-diag-bmc.py status'
```
Expected: Shows decoded CPLD 0x3c and 0x3d values with current mode. If it prints "ERROR: could not read CPLD 0x3c via daemon (timeout)", the bmc-daemon dispatch is not working — check `journalctl -u wedge100s-bmc-daemon`.

- [ ] **Step 4: Test single write+verify**

```bash
ssh admin@192.168.88.12 'sudo wedge100s-led-diag-bmc.py set rainbow'
```
Expected: `PASS: intended=0xe0 actual=0xe0  [PASS]` and visible rainbow pattern on front panel.

- [ ] **Step 5: Run full demo sequence**

```bash
ssh admin@192.168.88.12 'sudo wedge100s-led-diag-bmc.py demo'
```
Expected: All 8 steps show `[PASS]`, visible color changes on front panel (3s per pattern), ends in passthrough mode. Results saved to `/run/wedge100s/led_diag_results.json`.

- [ ] **Step 6: Inspect saved results**

```bash
ssh admin@192.168.88.12 'cat /run/wedge100s/led_diag_results.json'
```
Expected: JSON with `"all_pass": true`, each step showing `"match": true` with both intended and actual values.

- [ ] **Step 7: Cross-verify with existing direct-access tool**

After the demo leaves CPLD in passthrough mode (0x02), verify via the existing direct-SSH tool:
```bash
ssh admin@192.168.88.12 'sudo python3 /usr/local/bin/wedge100s-led-diag.py status'
```
Expected: The direct-access tool also shows `th_led_en: True`, confirming both paths see the same register state.

---

## Dependency Graph

```
Task 1 (shim.h cleanup)
  → Task 2 (shim.c rewrite)
    → Task 3 (deploy + verify on target)
      → Task 4 (LED investigation via bcmcmd)

Task 5 (bmc-daemon dispatch)
  → Task 6 (SONiC LED diag tool)
    → Task 7 (deploy + verify LED diag)
```

Tasks 1-4 and Tasks 5-7 are independent chains — they can run in parallel.

## Success Criteria

1. **bcmcmd works at runtime** — `docker exec syncd bcmcmd "version"` returns immediately
2. **Counter stats still work** — `show interfaces counters` shows correct values for flex sub-ports
3. **LED diag demo passes** — all 8 steps show PASS in `/run/wedge100s/led_diag_results.json`, with visible color changes
4. **Cross-path verification** — both daemon-path tool and direct-SSH tool report same CPLD state
5. **Investigation documented** — `notes/2026-04-03-led-pipeline-investigation.md` has findings with exact command output
