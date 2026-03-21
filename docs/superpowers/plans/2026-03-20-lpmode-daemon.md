# LP_MODE Daemon-Owned Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the i2c daemon the sole I2C owner of the LP_MODE PCA9535 pins, deassert LP_MODE for all present ports at startup, and reduce sfp.py to pure file I/O for get/set_lpmode.

**Architecture:** The daemon's `poll-presence` invocation already owns the CP2112 bus exclusively. We extend it to (a) deassert LP_MODE for every present port that has no `/run/wedge100s/sfp_N_lpmode` state file yet, and (b) process `/run/wedge100s/sfp_N_lpmode_req` request files written by sfp.py on each poll cycle. sfp.py's get/set_lpmode become pure file operations: no smbus2, no I2C.

**Tech Stack:** C (daemon extension), Python 3 (sfp.py), pytest (hardware stage)

---

## Hardware Reference

LP_MODE PCA9535 topology (from `notes/i2c_topology.json`):
- Mux 0x74 ch0 → bus 34 → PCA9535 **0x20** (ports 0–15)
- Mux 0x74 ch1 → bus 35 → PCA9535 **0x21** (ports 16–31)
- Config regs: 0x06 (bits 0–7), 0x07 (bits 8–15) — bit=1 means INPUT, bit=0 means OUTPUT
- Output regs: 0x02 (bits 0–7), 0x03 (bits 8–15)
- Boot state: all 0xFF (INPUT = pull-up HIGH = LP_MODE ASSERTED = lasers OFF)
- Deassert (enable TX): drive output LOW → configure as OUTPUT
- Assert (force low-power): release to INPUT (pull-up HIGH)
- XOR-1 bit interleave: `line = (port % 16) ^ 1`; `reg = line / 8`; `bit = line % 8`

Run-dir files:
- `/run/wedge100s/sfp_N_lpmode` — state, written by daemon: "0"=deasserted, "1"=asserted
- `/run/wedge100s/sfp_N_lpmode_req` — request, written by sfp.py, deleted by daemon after apply

---

## File Map

| File | Action | What changes |
|---|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c` | Modify | Add `LP_PCA9535_ADDR[]`, `LP_PCA9535_CHAN[]` constants; `i2c_write_byte_data()` helper; `set_lpmode_hidraw()`; `poll_lpmode_hidraw()`; `poll_lpmode_sysfs()`; wire into `main()` |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Modify | Replace `get_lpmode()` and `set_lpmode()` bodies with file I/O only |
| `tests/stage_21_lpmode/` | Create | New test stage: `__init__.py` + `test_lpmode.py` |

---

## Task 1: Write the failing LP_MODE daemon test

**Files:**
- Create: `tests/stage_21_lpmode/__init__.py`
- Create: `tests/stage_21_lpmode/test_lpmode.py`

- [ ] **Step 1.1: Create stage directory and `__init__.py`**

```bash
mkdir -p tests/stage_21_lpmode
touch tests/stage_21_lpmode/__init__.py
```

- [ ] **Step 1.2: Write `test_lpmode.py`**

```python
"""Stage 21 — LP_MODE daemon control.

Verifies:
  - All present QSFP ports have /run/wedge100s/sfp_N_lpmode state files.
  - Default daemon state is "0" (LP_MODE deasserted = TX lasers enabled).
  - sfp.py set_lpmode(True) → req file written → daemon applies within one tick.
  - sfp.py get_lpmode() returns correct value from state file.
  - Round-trip: assert then deassert returns port to "0".

Requires: wedge100s-i2c-daemon running (systemd timer or manual invocation).

TEST ORDERING NOTE: test_default_state_is_deasserted deletes all sfp_N_lpmode
files and must run AFTER test_state_files_exist_for_present_ports.  Do not
reorder tests within TestLpmodeDaemon — pytest runs class methods in declaration
order which is the correct order here.
"""

import time
import pytest

NUM_PORTS = 32
RUN_DIR = "/run/wedge100s"


def _present_ports(ssh):
    """Return list of 0-based port indices that are currently present."""
    ports = []
    for idx in range(NUM_PORTS):
        out, _, _ = ssh.run(
            f"cat {RUN_DIR}/sfp_{idx}_present 2>/dev/null", timeout=5
        )
        if out.strip() == "1":
            ports.append(idx)
    return ports


def _daemon_tick(ssh):
    """Force one daemon poll cycle and wait for it to complete."""
    ssh.run(
        "wedge100s-i2c-daemon poll-presence",
        timeout=30,
    )
    time.sleep(0.5)


class TestLpmodeDaemon:

    def test_state_files_exist_for_present_ports(self, ssh):
        """All present ports must have a /run/wedge100s/sfp_N_lpmode file after one daemon tick."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted — cannot test LP_MODE state files")

        missing = []
        for idx in present:
            out, _, rc = ssh.run(
                f"test -f {RUN_DIR}/sfp_{idx}_lpmode && echo ok", timeout=5
            )
            if out.strip() != "ok":
                missing.append(idx)

        assert not missing, (
            f"LP_MODE state files missing for present ports: {missing}"
        )

    def test_default_state_is_deasserted(self, ssh):
        """All present ports should default to lpmode=0 (TX enabled) after daemon init.

        Clears all existing lpmode state files first so the daemon's initial-deassert
        logic fires fresh regardless of prior test or operator state.
        """
        # Remove any pre-existing state files so daemon sees them as uninitialized.
        for idx in range(NUM_PORTS):
            ssh.run(f"rm -f {RUN_DIR}/sfp_{idx}_lpmode", timeout=5)

        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        bad = []
        for idx in present:
            out, _, _ = ssh.run(
                f"cat {RUN_DIR}/sfp_{idx}_lpmode 2>/dev/null", timeout=5
            )
            val = out.strip()
            if val != "0":
                bad.append((idx, val))

        assert not bad, (
            f"Expected lpmode=0 for all present ports after fresh init; got: {bad}"
        )

    def test_request_file_processed_within_one_tick(self, ssh):
        """Writing sfp_N_lpmode_req triggers daemon to update state and delete req file."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]

        # Teardown: always restore to lpmode=0 even if assertions fail mid-test.
        def _restore():
            ssh.run(f"rm -f {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run(f"echo 0 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)

        try:
            # Request LP_MODE assert
            ssh.run(f"echo 1 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            _daemon_tick(ssh)

            # State file should now be "1", req file should be gone
            state, _, _ = ssh.run(f"cat {RUN_DIR}/sfp_{port}_lpmode 2>/dev/null", timeout=5)
            req_exists, _, _ = ssh.run(
                f"test -f {RUN_DIR}/sfp_{port}_lpmode_req && echo yes || echo no", timeout=5
            )
            assert state.strip() == "1", f"Port {port} lpmode state should be 1, got '{state.strip()}'"
            assert req_exists.strip() == "no", "Request file should be deleted after processing"

        finally:
            _restore()
            # No assertion here: _restore() is best-effort cleanup. If the daemon
            # fails during teardown, the port may stay in lpmode=1 but that is
            # visible in the next test run. Asserting here would mask the original
            # test failure if the daemon had an I2C error.

    def test_get_lpmode_reads_state_file(self, ssh):
        """Platform API get_lpmode() must return value from daemon state file (no I2C)."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]
        eth = f"Ethernet{port * 4}"

        # Read state file directly
        file_val, _, _ = ssh.run(
            f"cat {RUN_DIR}/sfp_{port}_lpmode 2>/dev/null", timeout=5
        )
        expected_lpmode = file_val.strip() == "1"

        # Read via platform API
        api_out, _, rc = ssh.run(
            f"python3 -c \""
            f"from sonic_platform.platform import Platform; "
            f"p = Platform(); "
            f"sfp = p.get_chassis().get_sfp({port}); "
            f"print(sfp.get_lpmode())"
            f"\"",
            timeout=15,
        )
        assert rc == 0, f"Platform API call failed: {api_out}"
        api_val = api_out.strip().lower() == "true"
        assert api_val == expected_lpmode, (
            f"get_lpmode() returned {api_val}, expected {expected_lpmode} "
            f"(file value: {file_val.strip()!r})"
        )

    def test_set_lpmode_writes_req_file(self, ssh):
        """Platform API set_lpmode() must write req file and not touch I2C directly."""
        _daemon_tick(ssh)
        present = _present_ports(ssh)
        if not present:
            pytest.skip("No QSFP modules inserted")

        port = present[0]

        def _restore():
            ssh.run(f"rm -f {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run(f"echo 0 > {RUN_DIR}/sfp_{port}_lpmode_req", timeout=5)
            ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)

        try:
            # Call set_lpmode(True) via platform API
            out, _, rc = ssh.run(
                f"python3 -c \""
                f"from sonic_platform.platform import Platform; "
                f"p = Platform(); "
                f"sfp = p.get_chassis().get_sfp({port}); "
                f"print(sfp.set_lpmode(True))"
                f"\"",
                timeout=15,
            )
            assert rc == 0 and "True" in out, f"set_lpmode(True) failed: {out}"

            # Verify req file written
            req_val, _, _ = ssh.run(
                f"cat {RUN_DIR}/sfp_{port}_lpmode_req 2>/dev/null", timeout=5
            )
            assert req_val.strip() == "1", (
                f"Expected req file to contain '1', got '{req_val.strip()}'"
            )

        finally:
            _restore()
            # No assertion here: _restore() is best-effort. See note in
            # test_request_file_processed_within_one_tick.
```

- [ ] **Step 1.3: Run tests against hardware to confirm they all FAIL (expected)**

```bash
cd tests && pytest stage_21_lpmode/ -v 2>&1 | tail -20
```

Expected: All 5 tests FAIL. Specifically:
- `test_state_files_exist_for_present_ports` → FAIL (no `sfp_N_lpmode` files)
- `test_default_state_is_deasserted` → FAIL (files missing)
- `test_request_file_processed_within_one_tick` → FAIL (req ignored by daemon)
- `test_get_lpmode_reads_state_file` → FAIL or ERROR (falls through to smbus2)
- `test_set_lpmode_writes_req_file` → FAIL (writes I2C instead of file)

---

## Task 2: Add LP_MODE constants and `set_lpmode_hidraw()` to daemon

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`

- [ ] **Step 2.1: Add LP_MODE PCA9535 constants after the existing PCA9535_MUX_CHAN[] declaration (around line 88)**

Insert after the existing `PCA9535_MUX_CHAN` declaration:

```c
/* PCA9535 LP_MODE chips (mux 0x74 ch0/ch1) */
static const int LP_PCA9535_ADDR[2] = { 0x20, 0x21 };
static const int LP_PCA9535_CHAN[2] = { 0,    1    };
```

- [ ] **Step 2.2: Add `i2c_write_byte_data()` helper between `i2c_read_byte_data()` closing brace (line 359) and the `/* ── CPLD sysfs path` comment block (line 361)**

```c
/*
 * Write a single byte to I2C device at (bus, addr, reg) via SMBus ioctl.
 * Returns 0 on success, -1 on error.
 */
static int i2c_write_byte_data(int bus, int addr, int reg, uint8_t val)
{
    char devpath[32];
    int fd;
    union i2c_smbus_data data;
    struct i2c_smbus_ioctl_data args;

    snprintf(devpath, sizeof(devpath), "/dev/i2c-%d", bus);
    fd = open(devpath, O_RDWR);
    if (fd < 0) return -1;

    if (ioctl(fd, I2C_SLAVE_FORCE, addr) < 0) {
        close(fd);
        return -1;
    }

    data.byte = val;
    args.read_write = I2C_SMBUS_WRITE;
    args.command    = (unsigned char)reg;
    args.size       = I2C_SMBUS_BYTE_DATA;
    args.data       = &data;

    if (ioctl(fd, I2C_SMBUS, &args) < 0) {
        close(fd);
        return -1;
    }

    close(fd);
    return 0;
}
```

- [ ] **Step 2.3: Add `set_lpmode_hidraw()` before the `poll_lpmode_hidraw()` function (insert before `poll_cpld()`)**

```c
/*
 * Apply LP_MODE state for one port via CP2112 hidraw.
 *
 * lpmode=0: deassert (allow high power) — drive PCA9535 pin LOW as output.
 * lpmode=1: assert (force low power)   — release pin to INPUT (pull-up → HIGH).
 *
 * XOR-1 interleave: line = (port % 16) ^ 1
 * Config regs: 0x06 (port0 bits 0-7), 0x07 (port1 bits 8-15)
 * Output regs: 0x02 (port0 bits 0-7), 0x03 (port1 bits 8-15)
 *
 * Returns 0 on success, -1 on error.
 */
static int set_lpmode_hidraw(int port, int lpmode)
{
    int group = port / 16;
    int line  = (port % 16) ^ 1;  /* XOR-1 interleave (ONL sfpi.c) */
    int reg   = line / 8;
    int bit   = line % 8;
    int chip  = LP_PCA9535_ADDR[group];
    int chan  = LP_PCA9535_CHAN[group];

    uint8_t cfg_reg = (uint8_t)(0x06 + reg);
    uint8_t out_reg = (uint8_t)(0x02 + reg);
    uint8_t cfg_val = 0, out_val = 0;

    if (mux_select(0x74, chan) < 0) return -1;

    if (cp2112_write_read((uint8_t)chip, &cfg_reg, 1, &cfg_val, 1) < 0) {
        mux_deselect(0x74); return -1;
    }

    if (lpmode) {
        /* Assert: release pin to INPUT so pull-up drives HIGH */
        uint8_t write_buf[2] = { cfg_reg, (uint8_t)(cfg_val | (1u << bit)) };
        if (cp2112_write((uint8_t)chip, write_buf, 2) < 0) {
            mux_deselect(0x74); return -1;
        }
    } else {
        /* Deassert: drive output LOW first, then configure as OUTPUT */
        if (cp2112_write_read((uint8_t)chip, &out_reg, 1, &out_val, 1) < 0) {
            mux_deselect(0x74); return -1;
        }
        uint8_t out_buf[2] = { out_reg, (uint8_t)(out_val & ~(1u << bit)) };
        if (cp2112_write((uint8_t)chip, out_buf, 2) < 0) {
            mux_deselect(0x74); return -1;
        }
        uint8_t cfg_buf[2] = { cfg_reg, (uint8_t)(cfg_val & ~(1u << bit)) };
        if (cp2112_write((uint8_t)chip, cfg_buf, 2) < 0) {
            mux_deselect(0x74); return -1;
        }
    }

    mux_deselect(0x74);
    return 0;
}
```

---

## Task 3: Add `poll_lpmode_hidraw()` and `poll_lpmode_sysfs()` to daemon

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`

- [ ] **Step 3.1: Add `poll_lpmode_hidraw()` after `set_lpmode_hidraw()`**

```c
/*
 * Process LP_MODE state on each daemon invocation (hidraw path).
 *
 * Two actions in order:
 *
 * 1. Request files: for each sfp_N_lpmode_req, apply the requested lpmode
 *    state to hardware, update the sfp_N_lpmode state file, and delete the
 *    request file.  Req file content: "0" = deassert, "1" = assert.
 *
 * 2. Initial deassert: for each present port with no sfp_N_lpmode file,
 *    drive LP_MODE LOW (allow high power) and write sfp_N_lpmode="0".
 *    This fires once per port on first boot or hot-plug, overriding the
 *    hardware default of all-asserted (all-inputs, pull-up HIGH).
 *
 * Presence state is read from the sfp_N_present files written earlier in
 * this same invocation by poll_presence_hidraw().
 */
static void poll_lpmode_hidraw(void)
{
    int port;
    char req_path[80], state_path[80], present_path[64];

    /* 1. Process pending request files */
    for (port = 0; port < NUM_PORTS; port++) {
        snprintf(req_path,   sizeof(req_path),   RUN_DIR "/sfp_%d_lpmode_req", port);
        snprintf(state_path, sizeof(state_path), RUN_DIR "/sfp_%d_lpmode",     port);

        FILE *f = fopen(req_path, "r");
        if (!f) continue;

        char val[4] = {0};
        if (fgets(val, (int)sizeof(val), f))
            val[strcspn(val, "\r\n")] = '\0';
        fclose(f);

        int lpmode = (val[0] == '1') ? 1 : 0;
        if (set_lpmode_hidraw(port, lpmode) == 0) {
            write_str_file(state_path, lpmode ? "1" : "0");
            unlink(req_path);
        } else {
            fprintf(stderr,
                    "wedge100s-i2c-daemon: set_lpmode port %d -> %d failed\n",
                    port, lpmode);
        }
    }

    /* 2. Initial deassert for present ports with no state file */
    for (port = 0; port < NUM_PORTS; port++) {
        snprintf(present_path, sizeof(present_path), RUN_DIR "/sfp_%d_present", port);
        snprintf(state_path,   sizeof(state_path),   RUN_DIR "/sfp_%d_lpmode",  port);

        /* Skip absent ports */
        char pval[4] = {0};
        FILE *pf = fopen(present_path, "r");
        if (!pf) continue;
        if (fgets(pval, (int)sizeof(pval), pf))
            pval[strcspn(pval, "\r\n")] = '\0';
        fclose(pf);
        if (pval[0] != '1') continue;

        /* Skip ports already initialized (state file exists) */
        struct stat st;
        if (stat(state_path, &st) == 0) continue;

        /* Deassert LP_MODE (allow high power), record state */
        if (set_lpmode_hidraw(port, 0) == 0)
            write_str_file(state_path, "0");
        else
            fprintf(stderr,
                    "wedge100s-i2c-daemon: initial deassert port %d failed\n",
                    port);
    }
}
```

- [ ] **Step 3.2: Add `poll_lpmode_sysfs()` after `poll_lpmode_hidraw()` (Phase 1 fallback)**

Note: `LP_BUS` is a **file-scope** static constant inserted between `poll_lpmode_hidraw` and `set_lpmode_sysfs`. Do not put it inside a function body.

```c
/*
 * LP_MODE processing via i2c-dev ioctl (Phase 1 fallback).
 *
 * LP_MODE PCA9535 chips are on buses 34 (group 0) and 35 (group 1),
 * accessible when i2c_mux_pca954x has built the mux tree.
 * Uses i2c_read_byte_data() / i2c_write_byte_data() helpers.
 */
/* File-scope constant: LP_MODE bus numbers for Phase 1 sysfs fallback. */
static const int LP_BUS[2] = { 34, 35 };

static int set_lpmode_sysfs(int port, int lpmode)
{
    int group = port / 16;
    int line  = (port % 16) ^ 1;
    int reg   = line / 8;
    int bit   = line % 8;
    int bus   = LP_BUS[group];
    int chip  = LP_PCA9535_ADDR[group];

    int cfg_reg = 0x06 + reg;
    int out_reg = 0x02 + reg;

    if (lpmode) {
        int cfg_val = i2c_read_byte_data(bus, chip, cfg_reg);
        if (cfg_val < 0) return -1;
        return i2c_write_byte_data(bus, chip, cfg_reg,
                                   (uint8_t)(cfg_val | (1 << bit)));
    } else {
        int out_val = i2c_read_byte_data(bus, chip, out_reg);
        if (out_val < 0) return -1;
        if (i2c_write_byte_data(bus, chip, out_reg,
                                (uint8_t)(out_val & ~(1 << bit))) < 0)
            return -1;
        int cfg_val = i2c_read_byte_data(bus, chip, cfg_reg);
        if (cfg_val < 0) return -1;
        return i2c_write_byte_data(bus, chip, cfg_reg,
                                   (uint8_t)(cfg_val & ~(1 << bit)));
    }
}

static void poll_lpmode_sysfs(void)
{
    int port;
    char req_path[80], state_path[80], present_path[64];

    for (port = 0; port < NUM_PORTS; port++) {
        snprintf(req_path,   sizeof(req_path),   RUN_DIR "/sfp_%d_lpmode_req", port);
        snprintf(state_path, sizeof(state_path), RUN_DIR "/sfp_%d_lpmode",     port);

        FILE *f = fopen(req_path, "r");
        if (!f) continue;

        char val[4] = {0};
        if (fgets(val, (int)sizeof(val), f))
            val[strcspn(val, "\r\n")] = '\0';
        fclose(f);

        int lpmode = (val[0] == '1') ? 1 : 0;
        if (set_lpmode_sysfs(port, lpmode) == 0) {
            write_str_file(state_path, lpmode ? "1" : "0");
            unlink(req_path);
        } else {
            fprintf(stderr,
                    "wedge100s-i2c-daemon: set_lpmode (sysfs) port %d -> %d failed\n",
                    port, lpmode);
        }
    }

    for (port = 0; port < NUM_PORTS; port++) {
        snprintf(present_path, sizeof(present_path), RUN_DIR "/sfp_%d_present", port);
        snprintf(state_path,   sizeof(state_path),   RUN_DIR "/sfp_%d_lpmode",  port);

        char pval[4] = {0};
        FILE *pf = fopen(present_path, "r");
        if (!pf) continue;
        if (fgets(pval, (int)sizeof(pval), pf))
            pval[strcspn(pval, "\r\n")] = '\0';
        fclose(pf);
        if (pval[0] != '1') continue;

        struct stat st;
        if (stat(state_path, &st) == 0) continue;

        if (set_lpmode_sysfs(port, 0) == 0)
            write_str_file(state_path, "0");
        else
            fprintf(stderr,
                    "wedge100s-i2c-daemon: initial deassert (sysfs) port %d failed\n",
                    port);
    }
}
```

---

## Task 4: Wire LP_MODE poll into `main()`, build and deploy

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`

- [ ] **Step 4.1: Add LP_MODE calls in `main()` after the existing poll calls**

In `main()`, the current structure is:
```c
    poll_cpld();

    if (g_hidraw_fd >= 0) {
        cp2112_cancel();
        poll_syseeprom_hidraw();
        poll_presence_hidraw();
        close(g_hidraw_fd);
        g_hidraw_fd = -1;
    } else {
        poll_syseeprom();
        poll_presence();
    }
```

Change to:
```c
    poll_cpld();

    if (g_hidraw_fd >= 0) {
        cp2112_cancel();
        poll_syseeprom_hidraw();
        poll_presence_hidraw();
        poll_lpmode_hidraw();
        close(g_hidraw_fd);
        g_hidraw_fd = -1;
    } else {
        poll_syseeprom();
        poll_presence();
        poll_lpmode_sysfs();
    }
```

- [ ] **Step 4.2: Build the daemon binary on the switch**

```bash
ssh admin@192.168.88.12 \
  "gcc -O2 -o /tmp/wedge100s-i2c-daemon \
   /usr/share/sonic/platform/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c \
   2>&1"
```

Wait — the source is on the dev host, not the switch. Build steps:

1. Verify kernel headers are present on the switch (needed for `<linux/i2c-dev.h>`):

```bash
ssh admin@192.168.88.12 "dpkg -l linux-libc-dev 2>/dev/null | grep '^ii' || echo MISSING"
```

If MISSING: `ssh admin@192.168.88.12 "sudo apt-get install -y linux-libc-dev"`

2. Copy updated source to switch:

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c \
    admin@192.168.88.12:/tmp/
```

3. Build on switch:

```bash
ssh admin@192.168.88.12 \
  "gcc -O2 -o /tmp/wedge100s-i2c-daemon /tmp/wedge100s-i2c-daemon.c 2>&1"
```

Expected output: no errors, no warnings.

- [ ] **Step 4.3: Quick smoke test of new binary (before deploying)**

```bash
ssh admin@192.168.88.12 \
  "sudo /tmp/wedge100s-i2c-daemon poll-presence && \
   ls /run/wedge100s/sfp_*_lpmode 2>/dev/null | head -5 && echo DONE"
```

Expected: Several `sfp_N_lpmode` files appear for present ports. No errors on stderr.

- [ ] **Step 4.4: Deploy binary**

```bash
ssh admin@192.168.88.12 \
  "sudo cp /tmp/wedge100s-i2c-daemon /usr/bin/wedge100s-i2c-daemon"
```

- [ ] **Step 4.5: Run daemon tests — expect 3 of 5 to pass now**

```bash
cd tests && pytest stage_21_lpmode/ -v -k "state_files or default_state or request_file" 2>&1 | tail -20
```

Expected: `test_state_files_exist_for_present_ports`, `test_default_state_is_deasserted`, `test_request_file_processed_within_one_tick` all **PASS**.

`test_get_lpmode_reads_state_file` and `test_set_lpmode_writes_req_file` still FAIL (sfp.py not yet updated).

---

## Task 5: Refactor `sfp.py` `get_lpmode()` and `set_lpmode()` to file-only

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`

- [ ] **Step 5.1: Add lpmode file path constants near existing cache path constants (around line 63)**

After the existing `_I2C_PRESENT_CACHE` line, add:

```python
_LP_MODE_STATE = '/run/wedge100s/sfp_{}_lpmode'
_LP_MODE_REQ   = '/run/wedge100s/sfp_{}_lpmode_req'
```

- [ ] **Step 5.2: Replace `get_lpmode()` body**

Current `get_lpmode()` opens an SMBus and reads PCA9535 hardware.
Replace the entire body with:

```python
    def get_lpmode(self):
        """
        Return LP_MODE state from daemon state file.

        Returns True if LP_MODE is asserted (low-power, TX off),
        False if deasserted (high-power, TX enabled).

        If the state file does not exist (daemon not yet run), returns True
        (conservative: hardware default is asserted via PCB pull-ups).
        """
        state_file = _LP_MODE_STATE.format(self._port)
        try:
            with open(state_file) as f:
                return f.read().strip() == '1'
        except OSError:
            return True  # hardware default: all LP_MODE asserted at boot
```

- [ ] **Step 5.3: Replace `set_lpmode()` body**

Current `set_lpmode()` opens an SMBus and writes PCA9535 hardware.
Replace the entire body with:

```python
    def set_lpmode(self, lpmode):
        """
        Request LP_MODE change by writing a request file for the daemon.

        lpmode=True  → write "1" to sfp_N_lpmode_req (assert, force low-power)
        lpmode=False → write "0" to sfp_N_lpmode_req (deassert, allow high-power)

        The daemon reads and applies the request within one poll cycle (~3 s),
        then deletes the request file and updates the state file.

        Returns True immediately on successful file write (async: hardware state
        changes after the next daemon tick, ~3 s later).

        xcvrd contract: on this platform xcvrd calls set_lpmode() but does not
        re-read LP_MODE state to verify the result; it trusts get_lpmode() on the
        next poll cycle.  The ~3 s async window is acceptable because the daemon
        tick interval matches xcvrd's ~3 s poll period.
        """
        req_file = _LP_MODE_REQ.format(self._port)
        try:
            with open(req_file, 'w') as f:
                f.write('1' if lpmode else '0')
            return True
        except OSError:
            return False
```

- [ ] **Step 5.4: Verify smbus2 import and `_eeprom_bus_lock` are still used elsewhere before removing**

`_eeprom_bus_lock` is still used by `read_eeprom()`, `_hardware_read_eeprom()`, `write_eeprom()`. Leave all imports.

The `_MUX74_ADDR`, `_LP_MODE_CHIPS`, `_LP_MODE_CHANS` constants are now unused. Remove them to keep the file clean:

Lines to remove from sfp.py (around lines 92-94):
```python
_MUX74_ADDR    = 0x74
_LP_MODE_CHIPS = [0x20, 0x21]   # group 0 (ports 0-15), group 1 (ports 16-31)
_LP_MODE_CHANS = [0,    1   ]   # mux 0x74 channels for each group
```

Also remove the LP_MODE topology comment block that precedes them (lines ~77-95):
```python
# ---------------------------------------------------------------------------
# LP_MODE PCA9535 topology (discovered via I2C scan, 2026-03-20)
# ...
# ---------------------------------------------------------------------------
```

- [ ] **Step 5.5: Deploy updated sfp.py**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py \
    admin@192.168.88.12:/tmp/sfp.py
ssh admin@192.168.88.12 \
    "sudo cp /tmp/sfp.py /usr/lib/python3/dist-packages/sonic_platform/sfp.py"
```

---

## Task 6: Run full stage 21 tests — all 5 should pass

- [ ] **Step 6.1: Run all stage 21 tests**

```bash
cd tests && pytest stage_21_lpmode/ -v 2>&1 | tail -30
```

Expected output:
```
PASSED stage_21_lpmode/test_lpmode.py::TestLpmodeDaemon::test_state_files_exist_for_present_ports
PASSED stage_21_lpmode/test_lpmode.py::TestLpmodeDaemon::test_default_state_is_deasserted
PASSED stage_21_lpmode/test_lpmode.py::TestLpmodeDaemon::test_request_file_processed_within_one_tick
PASSED stage_21_lpmode/test_lpmode.py::TestLpmodeDaemon::test_get_lpmode_reads_state_file
PASSED stage_21_lpmode/test_lpmode.py::TestLpmodeDaemon::test_set_lpmode_writes_req_file
5 passed in ...
```

- [ ] **Step 6.2: Run existing stage 10 and 11 tests to confirm no regressions**

```bash
cd tests && pytest stage_10_daemon/ stage_11_transceiver/ -v 2>&1 | tail -20
```

Expected: All previously passing tests still pass.

- [ ] **Step 6.3: Run stage 07 (presence/EEPROM) to confirm daemon still works correctly**

```bash
cd tests && pytest stage_07_qsfp/ -v 2>&1 | tail -20
```

Expected: All passing.

---

## Task 7: Update STAGED_PHASES.md

**Files:**
- Modify: `tests/STAGED_PHASES.md`

- [ ] **Step 7.1: Append Phase 21 entry to STAGED_PHASES.md**

Add to the end of the phase list (before any posttest section):

```markdown
## Phase 21: LP_MODE Daemon Control
**Status: COMPLETE**
- Daemon exclusively owns LP_MODE PCA9535 pins (0x20, 0x21 on mux 0x74 ch0/ch1)
- All present ports deasserted (LP_MODE=0, TX enabled) on first daemon invocation
- sfp.py get_lpmode() reads /run/wedge100s/sfp_N_lpmode (file-only, no I2C)
- sfp.py set_lpmode() writes /run/wedge100s/sfp_N_lpmode_req (file-only, no I2C)
- Daemon processes req files within one poll tick (~3 s), deletes req file after apply
```

---

## Debugging Reference

**Verify LP_MODE hardware state directly (bypass daemon files):**
```bash
# On switch — check LP_MODE PCA9535 config register for ports 0-15
ssh admin@192.168.88.12 "python3 -c \"
from smbus2 import SMBus
with SMBus(1) as b:
    b.write_byte(0x74, 1<<0)       # select mux 0x74 ch0
    cfg0 = b.read_byte_data(0x20, 0x06)
    cfg1 = b.read_byte_data(0x20, 0x07)
    out0 = b.read_byte_data(0x20, 0x02)
    out1 = b.read_byte_data(0x20, 0x03)
    b.write_byte(0x74, 0x00)       # deselect
    print(f'cfg0={cfg0:#04x} cfg1={cfg1:#04x} out0={out0:#04x} out1={out1:#04x}')
\""
```
Expected after deassert: `cfg0=0x00 cfg1=0x00 out0=0x00 out1=0x00` (all outputs, all LOW).
Boot state (no daemon): `cfg0=0xff cfg1=0xff` (all inputs).

**Check req file handling manually:**
```bash
echo 1 > /run/wedge100s/sfp_0_lpmode_req
wedge100s-i2c-daemon poll-presence
cat /run/wedge100s/sfp_0_lpmode   # should be "1"
ls /run/wedge100s/sfp_0_lpmode_req 2>/dev/null && echo "BUG: req not deleted" || echo "OK: req deleted"
```

**If daemon fails to build:** check for missing `<sys/stat.h>` include (needed for `stat()` call in `poll_lpmode_hidraw`). The include is already present in the existing daemon.

**If LP_MODE deassert appears to work but modules still don't TX:**
- Check byte 93 (TX_DISABLE) in the QSFP EEPROM cache: `xxd /run/wedge100s/sfp_N_eeprom | grep "005[89]"`
- Byte 86 (offset 0x56) is TX_DISABLE in SFF-8636; byte 93 may be misidentified
- This is the secondary sff_mgr.py byte-93 issue — tracked separately
