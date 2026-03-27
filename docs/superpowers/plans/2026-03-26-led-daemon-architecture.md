# LED Daemon Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace two timer+oneshot BMC/I2C polling pairs with persistent daemons, and permanently clear the ONIE LED rainbow via a BMC-side init script deployed by platform-init.

**Architecture:** D1 deploys a BMC shell utility (`clear_led_diag.sh`) that writes four syscpld sysfs attributes to kill LED test mode; a new C binary (`wedge100s-bmc-auth`) pushes the SSH key via TTY so platform-init can reach the BMC over SSH every boot. D2 and D3 replace the timer+oneshot pattern with a persistent timerfd+inotify loop, cutting write-request latency from ≤3 s / ≤10 s to ≤50 ms. D4 is a hardware verification that requires no code change unless LED colours are wrong.

**Tech Stack:** C (gcc), POSIX timerfd/inotify/poll, systemd unit files, Debian packaging (debian/rules + postinst), Python 3 (accton_wedge100s_util.py), pytest (tests/stage_10_daemon)

**Spec:** `docs/superpowers/specs/2026-03-24-led-daemon-architecture-design.md`

---

## File Map

| Action | Path (relative to `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/`) |
|--------|--------------------------------------------------------------------------------------|
| **NEW** | `utils/clear_led_diag.sh` |
| **NEW** | `utils/wedge100s-bmc-auth.c` |
| **NEW** | `service/wedge100s-i2c-daemon.service` |
| **NEW** | `service/wedge100s-bmc-daemon.service` |
| **REMOVE** | `service/wedge100s-i2c-poller.timer` |
| **REMOVE** | `service/wedge100s-i2c-poller.service` |
| **REMOVE** | `service/wedge100s-bmc-poller.timer` |
| **REMOVE** | `service/wedge100s-bmc-poller.service` |
| **MODIFY** | `utils/wedge100s-i2c-daemon.c` |
| **MODIFY** | `utils/wedge100s-bmc-daemon.c` |
| **MODIFY** | `utils/accton_wedge100s_util.py` |
| **MODIFY** | `debian/rules` (one level up from the wedge100s-32x dir) |
| **MODIFY** | `debian/sonic-platform-accton-wedge100s-32x.postinst` (same level) |
| **MODIFY** | `tests/stage_10_daemon/test_daemon.py` |
| **MODIFY** | `notes/SUBSYSTEMS_LED.md` (D4 only) |

Absolute `debian/` path: `platform/broadcom/sonic-platform-modules-accton/debian/`

---

## Task 1: D1 — BMC LED Init (bmc-auth + clear_led_diag.sh + platform-init)

**Files:**
- Create: `utils/clear_led_diag.sh`
- Create: `utils/wedge100s-bmc-auth.c`
- Modify: `debian/rules` (add bmc-auth build/clean steps)
- Modify: `utils/accton_wedge100s_util.py` (remove `_request_led_init`, add `_bmc_led_init`)

**Dependency:** None. Start here.

---

- [ ] **Step 1.0: Write failing D1 test**

Add to `tests/stage_10_daemon/test_daemon.py` — this will FAIL until D1 is deployed:

```python
def test_bmc_led_init_deployed(ssh):
    """D1: clear_led_diag.sh is on BMC and th_led_en=1 (platform-init ran)."""
    # clear_led_diag.sh must exist on BMC
    _, _, rc = ssh.run(
        "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%usb0 test -x /usr/local/bin/clear_led_diag.sh",
        timeout=15
    )
    assert rc == 0, "clear_led_diag.sh missing from BMC /usr/local/bin/"

    # th_led_en must be 1
    out, _, rc2 = ssh.run(
        "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%usb0 "
        "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en",
        timeout=15
    )
    assert rc2 == 0 and out.strip() == "1", (
        f"th_led_en={out.strip()!r} (expected 1) — D1 LED init not yet applied"
    )
```

Run to confirm FAIL (expected before D1 deploy):
```bash
cd tests && pytest stage_10_daemon/test_daemon.py::test_bmc_led_init_deployed -v
```

---

- [ ] **Step 1.1: Verify BMC syscpld sysfs attributes exist**

Confirm all five attributes that `clear_led_diag.sh` will write are present:

```bash
ssh root@192.168.88.13 "ls /sys/class/i2c-adapter/i2c-12/12-0031/ | grep -E 'led|walk|th_led'"
```

Expected output must include: `led_test_mode_en`, `led_test_blink_en`, `walk_test_en`, `th_led_en`

Also confirm `board-utils.sh` defines `SYSCPLD_SYSFS_DIR`:
```bash
ssh root@192.168.88.13 "grep SYSCPLD_SYSFS_DIR /usr/local/bin/board-utils.sh"
```
Expected: `SYSCPLD_SYSFS_DIR="/sys/class/i2c-adapter/i2c-12/12-0031"`

---

- [ ] **Step 1.2: Create `utils/clear_led_diag.sh`**

`clear_led_diag.sh` lives in `utils/` and is installed to `/usr/bin/clear_led_diag.sh` on the host by the existing `debian/rules` copy rule (all non-.c files from utils/ → usr/bin/). This host path is used only as the SCP source in `_bmc_led_init` — the script is a BMC utility that sources BMC-private `board-utils.sh` and would fail if run directly on the host (no `board-utils.sh` present). This placement is intentional and follows the pattern used by the existing host-side daemon binaries in utils/.

```bash
cat > platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/clear_led_diag.sh << 'EOF'
#!/bin/sh
# clear_led_diag.sh — disable syscpld LED test pattern, enable TH LEDUP output.
# BMC-side utility. Installed to BMC /usr/local/bin/ by SONiC platform-init.
# Safe to run at any time; idempotent.
# DO NOT run on the SONiC host — this script requires BMC sysfs paths.
. /usr/local/bin/board-utils.sh
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_mode_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_blink_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/walk_test_en
echo 1 > ${SYSCPLD_SYSFS_DIR}/th_led_en
EOF
chmod +x platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/clear_led_diag.sh
```

Verify: `cat platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/clear_led_diag.sh`

---

- [ ] **Step 1.3: Smoke-test `clear_led_diag.sh` on BMC directly**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/clear_led_diag.sh \
    root@192.168.88.13:/tmp/clear_led_diag_test.sh
ssh root@192.168.88.13 "chmod +x /tmp/clear_led_diag_test.sh && /tmp/clear_led_diag_test.sh && echo OK"
```

Expected: `OK` with no errors.

Verify the register was changed:
```bash
ssh root@192.168.88.13 "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en"
```
Expected: `1`

---

- [ ] **Step 1.4: Create `utils/wedge100s-bmc-auth.c`**

Write the following to `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-auth.c`:

```c
/*
 * wedge100s-bmc-auth.c — Push SSH public key to BMC via /dev/ttyACM0.
 *
 * Opens the BMC serial console (57600 8N1), logs in as root/0penBmc,
 * appends /etc/sonic/wedge100s-bmc-key.pub to /root/.ssh/authorized_keys
 * idempotently, then exits cleanly.
 *
 * Called from platform-init (do_install) on every SONiC boot.
 * Also called by wedge100s-bmc-daemon on every BMC reconnect, since the
 * BMC clears authorized_keys on every BMC reboot.
 *
 * Exits 0 on success, 1 on any failure.
 *
 * Build: gcc -O2 -o wedge100s-bmc-auth wedge100s-bmc-auth.c
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <sys/select.h>
#include <sys/time.h>

#define TTY_DEV     "/dev/ttyACM0"
#define BMC_LOGIN   "root"
#define BMC_PASS    "0penBmc"
#define PUBKEY_PATH "/etc/sonic/wedge100s-bmc-key.pub"
#define TIMEOUT_SEC 10

static int g_tty_fd = -1;

static int tty_open(void)
{
    struct termios tio;

    g_tty_fd = open(TTY_DEV, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (g_tty_fd < 0) {
        fprintf(stderr, "wedge100s-bmc-auth: open %s: %s\n",
                TTY_DEV, strerror(errno));
        return -1;
    }

    if (tcgetattr(g_tty_fd, &tio) < 0) {
        close(g_tty_fd); g_tty_fd = -1; return -1;
    }
    cfmakeraw(&tio);
    cfsetispeed(&tio, B57600);
    cfsetospeed(&tio, B57600);
    tio.c_cflag |= (CLOCAL | CREAD);
    tio.c_cc[VMIN]  = 0;
    tio.c_cc[VTIME] = 0;
    tcsetattr(g_tty_fd, TCSANOW, &tio);
    tcflush(g_tty_fd, TCIOFLUSH);
    return 0;
}

/*
 * Read from TTY until needle found or timeout_sec elapses.
 * Returns 1 if needle found, 0 on timeout.
 * Accumulates up to bufsz-1 bytes; rolls the tail to avoid missing
 * needles that span two reads.
 */
static int tty_wait_for(const char *needle, int timeout_sec,
                        char *buf, int bufsz)
{
    int pos = 0;
    time_t deadline = time(NULL) + timeout_sec;
    int nlen = (int)strlen(needle);

    buf[0] = '\0';
    while (time(NULL) < deadline) {
        fd_set rset;
        struct timeval tv = {0, 200000};   /* 200 ms poll */
        FD_ZERO(&rset);
        FD_SET(g_tty_fd, &rset);
        if (select(g_tty_fd + 1, &rset, NULL, NULL, &tv) <= 0) continue;

        ssize_t n = read(g_tty_fd, buf + pos, bufsz - pos - 1);
        if (n <= 0) continue;
        pos += (int)n;
        buf[pos] = '\0';

        if (strstr(buf, needle)) return 1;

        /* Keep a tail window to avoid missing needle spanning reads */
        if (pos > nlen * 2) {
            memmove(buf, buf + pos - nlen, (size_t)nlen);
            pos = nlen;
            buf[pos] = '\0';
        }
    }
    return 0;
}

static void tty_send(const char *s)
{
    write(g_tty_fd, s, strlen(s));
}

int main(void)
{
    char pubkey[512];
    char cmd[768];
    char buf[1024];
    FILE *fp;

    /* Read public key */
    fp = fopen(PUBKEY_PATH, "r");
    if (!fp) {
        fprintf(stderr, "wedge100s-bmc-auth: %s: %s\n",
                PUBKEY_PATH, strerror(errno));
        return 1;
    }
    if (!fgets(pubkey, (int)sizeof(pubkey), fp)) {
        fclose(fp);
        fprintf(stderr, "wedge100s-bmc-auth: empty pubkey %s\n", PUBKEY_PATH);
        return 1;
    }
    fclose(fp);
    pubkey[strcspn(pubkey, "\r\n")] = '\0';

    if (tty_open() < 0) return 1;

    /* Send CR to prod any existing session */
    tty_send("\r\n");
    usleep(300000);

    /* Check for shell prompt first (already logged in) */
    tty_send("\r\n");
    if (tty_wait_for("# ", 2, buf, sizeof(buf))) goto logged_in;

    /* Not logged in: wait for login prompt */
    tty_send("\r\n");
    if (!tty_wait_for("login:", TIMEOUT_SEC, buf, sizeof(buf))) {
        fprintf(stderr, "wedge100s-bmc-auth: no login prompt on %s\n", TTY_DEV);
        close(g_tty_fd);
        return 1;
    }

    tty_send(BMC_LOGIN "\r\n");
    if (!tty_wait_for("Password:", TIMEOUT_SEC, buf, sizeof(buf))) {
        fprintf(stderr, "wedge100s-bmc-auth: no password prompt\n");
        close(g_tty_fd);
        return 1;
    }

    tty_send(BMC_PASS "\r\n");
    if (!tty_wait_for("# ", TIMEOUT_SEC, buf, sizeof(buf))) {
        fprintf(stderr, "wedge100s-bmc-auth: login failed\n");
        close(g_tty_fd);
        return 1;
    }

logged_in:
    /* Append key idempotently; use long form to avoid shell quoting issues */
    snprintf(cmd, sizeof(cmd),
             "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
             "grep -qxF '%s' /root/.ssh/authorized_keys 2>/dev/null || "
             "echo '%s' >> /root/.ssh/authorized_keys\r\n",
             pubkey, pubkey);
    tty_send(cmd);

    if (!tty_wait_for("# ", TIMEOUT_SEC, buf, sizeof(buf))) {
        fprintf(stderr, "wedge100s-bmc-auth: command timed out\n");
        close(g_tty_fd);
        return 1;
    }

    tty_send("exit\r\n");
    usleep(100000);
    close(g_tty_fd);
    return 0;
}
```

---

- [ ] **Step 1.5: Add `wedge100s-bmc-auth` build/clean to `debian/rules`**

In `platform/broadcom/sonic-platform-modules-accton/debian/rules`:

Inside `override_dh_auto_clean`, after the two existing `rm -f` lines, add:
```makefile
		rm -f $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth; \
```

Inside `override_dh_auto_build`, after the `wedge100s-i2c-daemon` build block (line ~75), add:
```makefile
		if [ -f $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth.c ]; then \
			gcc -O2 -o $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth \
				$(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth.c; \
			echo "Built wedge100s-bmc-auth for $$mod"; \
		fi; \
```

The install step (`override_dh_auto_install`) already copies all non-.c files from `utils/` to `usr/bin/` — the `wedge100s-bmc-auth` binary gets installed automatically.

---

- [ ] **Step 1.6: Add `_bmc_led_init()` to `utils/accton_wedge100s_util.py`**

> **Note:** Complete Steps 1.6 and 1.7 together before building — Step 1.7 updates the `do_install()` call site to reference `_bmc_led_init()`, and Step 1.6 provides its definition. Building after only one of these steps will cause a `NameError` or leave the old `_request_led_init()` call in place.

Add the following function after `_configure_usb0()` and before `do_install()`:

```python
def _bmc_led_init():
    """Push SSH key to BMC, deploy clear_led_diag.sh, and run it.

    Platform-init sequence per spec D1:
      1. wedge100s-bmc-auth  — push key via /dev/ttyACM0 (10s TTY automation)
      2. SSH probe           — gate; non-fatal if BMC unreachable
      3. Deploy clear_led_diag.sh to BMC /usr/local/bin/ if absent/changed
      4. Patch setup_board.sh inside board_rev>=2 block (once; idempotent)
      5. Run clear_led_diag.sh immediately (every boot)
    """
    import syslog

    BMC_HOST  = "root@fe80::ff:fe00:1%usb0"
    BMC_KEY   = "/etc/sonic/wedge100s-bmc-key"
    SSH_OPTS  = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-i", BMC_KEY,
    ]
    LOCAL_SCRIPT = "/usr/bin/clear_led_diag.sh"
    BMC_SCRIPT   = "/usr/local/bin/clear_led_diag.sh"
    SETUP_BOARD  = "/etc/init.d/setup_board.sh"

    # Step 1: push SSH key via TTY
    ret = subprocess.run(["/usr/bin/wedge100s-bmc-auth"],
                         timeout=30).returncode
    if ret != 0:
        syslog.syslog(syslog.LOG_WARNING,
                      "platform-init: BMC key push failed (exit %d)" % ret)
        return

    # Step 2: SSH probe (gate for steps 3-5)
    probe = subprocess.run(
        ["ssh"] + SSH_OPTS + [BMC_HOST, "echo ok"],
        capture_output=True, text=True, timeout=10
    )
    if probe.returncode != 0:
        syslog.syslog(syslog.LOG_WARNING,
                      "platform-init: BMC SSH probe failed — skipping LED init")
        return

    # Step 3: deploy clear_led_diag.sh if absent or changed.
    # Use SSH stdin pipe (not scp): scp mishandles IPv6 link-local addresses
    # with % scope-id (e.g. fe80::ff:fe00:1%usb0) on some OpenSSH versions.
    try:
        with open(LOCAL_SCRIPT, 'rb') as f:
            deploy = subprocess.run(
                ["ssh"] + SSH_OPTS + [BMC_HOST,
                 "cat > %s && chmod +x %s" % (BMC_SCRIPT, BMC_SCRIPT)],
                stdin=f, capture_output=True, timeout=15
            )
        if deploy.returncode != 0:
            syslog.syslog(syslog.LOG_WARNING,
                          "platform-init: clear_led_diag.sh deploy failed")
            # Continue: script may be present from a prior boot
    except OSError as e:
        syslog.syslog(syslog.LOG_WARNING,
                      "platform-init: clear_led_diag.sh open failed: " + str(e))

    # Step 4: patch setup_board.sh once (idempotent grep guard)
    check = subprocess.run(
        ["ssh"] + SSH_OPTS + [BMC_HOST,
         "grep -q 'clear_led_diag.sh' %s" % SETUP_BOARD],
        capture_output=True, timeout=10
    )
    if check.returncode != 0:
        # Insert "    clear_led_diag.sh" before the closing "fi" of the
        # board_rev >= 2 block.  The block ends at the first bare "fi".
        patch_cmd = (
            r"sed -i '/if \[ \$board_rev -ge 2 \]/,/^fi/{/^fi/i\\"
            r"    # SONiC LED init\n    clear_led_diag.sh"
            r"}' " + SETUP_BOARD
        )
        subprocess.run(
            ["ssh"] + SSH_OPTS + [BMC_HOST, patch_cmd],
            capture_output=True, timeout=10
        )

    # Step 5: run clear_led_diag.sh immediately (every boot)
    result = subprocess.run(
        ["ssh"] + SSH_OPTS + [BMC_HOST,
         "chmod +x %s && %s" % (BMC_SCRIPT, BMC_SCRIPT)],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        syslog.syslog(syslog.LOG_INFO,
                      "platform-init: clear_led_diag.sh OK (th_led_en=1)")
    else:
        syslog.syslog(syslog.LOG_WARNING,
                      "platform-init: clear_led_diag.sh failed: " + result.stderr.strip())
```

---

- [ ] **Step 1.7: Modify `utils/accton_wedge100s_util.py` — remove `_request_led_init`, update `do_install()`**

Delete the `_request_led_init` function body (currently lines 360–377, starting with `def _request_led_init():` through the closing `except` block).

In `do_install()`, the last three lines currently read:
```python
    _configure_usb0()
    _request_led_init()
    print("Platform init complete.")
```

Change to:
```python
    _configure_usb0()
    _bmc_led_init()
    print("Platform init complete.")
```

---

- [ ] **Step 1.8: Build the .deb**

```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Expected: build completes successfully. Check that `wedge100s-bmc-auth` is compiled:

```bash
ls -la platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-auth
```

---

- [ ] **Step 1.9: Deploy and verify on hardware**

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 "sudo systemctl stop pmon && sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
```

Restart platform-init to trigger `_bmc_led_init()`:
```bash
ssh admin@192.168.88.12 "sudo systemctl restart wedge100s-platform-init"
```

Verify `th_led_en=1` on BMC:
```bash
ssh root@192.168.88.13 "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en"
```
Expected: `1`

Verify `clear_led_diag.sh` is deployed to BMC:
```bash
ssh root@192.168.88.13 "ls -la /usr/local/bin/clear_led_diag.sh && grep clear_led_diag /etc/init.d/setup_board.sh"
```
Expected: file exists, grep finds the line.

Verify syslog entries:
```bash
ssh admin@192.168.88.12 "sudo journalctl -u wedge100s-platform-init --no-pager | grep -E 'bmc-auth|clear_led|LED'"
```

Restart pmon:
```bash
ssh admin@192.168.88.12 "sudo systemctl start pmon"
```

---

- [ ] **Step 1.10: Commit D1**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/clear_led_diag.sh
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-auth.c
git add platform/broadcom/sonic-platform-modules-accton/debian/rules
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py
git commit -m "feat(D1): BMC LED init — clear_led_diag.sh + wedge100s-bmc-auth

- Add clear_led_diag.sh (BMC-side utility): writes four syscpld sysfs
  attributes to clear LED test mode and enable BCM LEDUP output
- Add wedge100s-bmc-auth.c: TTY-based SSH key push to BMC; replaces the
  Python bmc.provision_ssh_key() path for platform-init
- platform-init do_install(): call bmc-auth, SSH probe, deploy and run
  clear_led_diag.sh on every boot
- Remove _request_led_init() from accton_wedge100s_util.py; syscpld
  LED control is now BMC-owned via clear_led_diag.sh"
```

---

## Task 2: D2 — i2c-daemon: Timer+oneshot → Persistent Daemon

**Files:**
- Create: `service/wedge100s-i2c-daemon.service`
- Modify: `utils/wedge100s-i2c-daemon.c` (new daemon_init + main loop; preserve all existing poll functions)
- Remove: `service/wedge100s-i2c-poller.timer`, `service/wedge100s-i2c-poller.service`

**Dependency:** Task 1 complete (removes syscpld_led_ctrl from bmc-daemon; i2c-daemon itself has no syscpld_led_ctrl references, so D2 can begin as soon as D1 is committed).

**Parallel with Task 3** — D2 and D3 touch different files. Both feed into Task 4 (Integration).

---

- [ ] **Step 2.1: Write failing integration test for persistent i2c-daemon**

Update `tests/stage_10_daemon/test_daemon.py` — add these tests near the top of the timer/service section (they will FAIL until D2 is deployed):

```python
# New: persistent daemon tests (will pass after D2 ships)
I2C_DAEMON  = "wedge100s-i2c-daemon.service"
BMC_DAEMON  = "wedge100s-bmc-daemon.service"

def test_i2c_daemon_running(ssh):
    """wedge100s-i2c-daemon.service is active (persistent daemon, D2)."""
    active = _systemctl_is_active(ssh, I2C_DAEMON)
    print(f"\n{I2C_DAEMON}: {'active' if active else 'INACTIVE'}")
    assert active, f"{I2C_DAEMON} not active — D2 not yet deployed"
```

Run to confirm FAIL:
```bash
cd tests && pytest stage_10_daemon/test_daemon.py::test_i2c_daemon_running -v
```
Expected: `FAILED` (unit not found)

---

- [ ] **Step 2.2: Create `service/wedge100s-i2c-daemon.service`**

Write to `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-i2c-daemon.service`:

```ini
[Unit]
Description=Wedge100S QSFP I2C persistent daemon (presence + EEPROM cache)
Documentation=file:///usr/bin/wedge100s-i2c-daemon.c
After=wedge100s-platform-init.service
Requires=wedge100s-platform-init.service

[Service]
Type=simple
ExecStart=/usr/bin/wedge100s-i2c-daemon
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
```

---

- [ ] **Step 2.3: Add new includes and global flag to `wedge100s-i2c-daemon.c`**

At the top of the `#include` block (after the existing includes), add:

```c
#include <poll.h>
#include <signal.h>
#include <syslog.h>
#include <sys/inotify.h>
#include <sys/timerfd.h>
```

---

- [ ] **Step 2.4: Add `mux_deselect_all()` after the existing `mux_deselect()`**

After line 326 (after the closing `}` of `mux_deselect()`), insert:

```c
/* Deselect all channels on all five PCA9548 muxes.
 * Called at startup/restart for crash-recovery: a prior crash may have
 * left a mux channel selected, mis-routing all subsequent I2C addresses.
 * Returns 0 if all deselects succeed, -1 if any fail.
 */
static int mux_deselect_all(void)
{
    static const uint8_t mux_addrs[] = {0x70, 0x71, 0x72, 0x73, 0x74};
    int ok = 0;
    for (int i = 0; i < 5; i++) {
        if (mux_deselect(mux_addrs[i]) < 0) ok = -1;
    }
    return ok;
}
```

---

- [ ] **Step 2.5: Add `daemon_init()` before `main()`**

Insert before `main()`:

```c
/*
 * daemon_init — open hidraw0, drain stale reports, deselect all muxes.
 *
 * On crash recovery (Restart=on-failure), a prior run may have left
 * the CP2112 in mid-transaction and a PCA9548 mux channel selected.
 * Draining stale HID reports and writing 0x00 to all five mux addresses
 * resets the bus to a known-good state before the main loop starts.
 *
 * If the first attempt fails, try BMC escalation (i2c_flush + mux reset
 * via SSH), then retry once.  If still failing, return -1 so systemd
 * Restart=on-failure handles the backoff.
 */
static int daemon_init(void)
{
    static const char SSH_FLUSH[] =
        "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%%usb0 "
        "/usr/local/bin/cp2112_i2c_flush.sh >/dev/null 2>&1";
    static const char SSH_RESET_MUX[] =
        "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%%usb0 "
        "/usr/local/bin/reset_qsfp_mux.sh >/dev/null 2>&1";

    for (int attempt = 0; attempt < 2; attempt++) {
        if (attempt == 1) {
            syslog(LOG_WARNING, "wedge100s-i2c-daemon: attempting BMC escalation");
            (void)system(SSH_FLUSH);
            (void)system(SSH_RESET_MUX);
            usleep(500000);
        }

        if (g_hidraw_fd >= 0) { close(g_hidraw_fd); g_hidraw_fd = -1; }
        g_hidraw_fd = open("/dev/hidraw0", O_RDWR);
        if (g_hidraw_fd < 0) {
            syslog(LOG_ERR, "wedge100s-i2c-daemon: open /dev/hidraw0: %s",
                   strerror(errno));
            continue;
        }

        cp2112_cancel();

        if (mux_deselect_all() == 0) {
            syslog(LOG_INFO, "wedge100s-i2c-daemon: daemon_init OK (hidraw0 open)");
            return 0;
        }
        syslog(LOG_WARNING,
               "wedge100s-i2c-daemon: mux_deselect_all failed (attempt %d)",
               attempt + 1);
    }

    syslog(LOG_ERR, "wedge100s-i2c-daemon: daemon_init failed after BMC escalation");
    return -1;
}
```

---

- [ ] **Step 2.6: Add `drain_inotify()` and `service_write_requests()` before `main()`**

```c
/* Drain all pending inotify events (prevents thundering-herd on burst writes). */
static void drain_inotify(int inotify_fd)
{
    char ibuf[sizeof(struct inotify_event) + NAME_MAX + 1];
    while (read(inotify_fd, ibuf, sizeof(ibuf)) > 0)
        ;
}

/*
 * service_write_requests — scan /run/wedge100s/ for pending request files.
 *
 * Called on inotify IN_CLOSE_WRITE events (~50 ms latency).
 * Scans the directory rather than replaying inotify filenames:
 * inotify coalesces duplicate events, so directory scan is the only
 * reliable way to find all pending work on burst writes.
 */
static void service_write_requests(void)
{
    if (g_hidraw_fd < 0) return;
    poll_lpmode_hidraw();
    poll_write_requests_hidraw();
    poll_read_requests_hidraw();
    apply_led_writes();   /* respond to led_sys{1,2} writes ~50ms */
}
```

---

- [ ] **Step 2.7: Verify `CPLD_SYSFS` macro name, then modify `poll_cpld()` for `cpld_version` boot-once**

First, confirm the macro name used in `wedge100s-i2c-daemon.c`:
```bash
grep -n 'CPLD_SYSFS\|CPLD_PATH\|1-0032' platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c | head -10
```
Expected: `#define CPLD_SYSFS "/sys/bus/i2c/devices/1-0032"` at line ~402.
If the macro name differs from `CPLD_SYSFS`, substitute the correct name in the replacement code below.

Replace the current `poll_cpld()` body with a version that reads `cpld_version` only once at first tick (boot-once, like syseeprom), while reading PSU attributes every tick.

Find the current `poll_cpld()` (line ~903):

```c
static void poll_cpld(void)
{
    static const char *attrs[] = {
        "cpld_version",
        "psu1_present", "psu1_pgood",
        "psu2_present", "psu2_pgood",
        NULL
    };

    for (int i = 0; attrs[i]; i++) {
        ...
    }
}
```

Replace with:

```c
static void poll_cpld(void)
{
    /* cpld_version: static hardware info — read once at first tick only. */
    {
        char dst[128];
        struct stat st;
        snprintf(dst, sizeof(dst), RUN_DIR "/cpld_version");
        if (stat(dst, &st) != 0) {
            char src[128], val[64];
            snprintf(src, sizeof(src), CPLD_SYSFS "/cpld_version");
            FILE *f = fopen(src, "r");
            if (f) {
                if (fgets(val, (int)sizeof(val), f)) {
                    int n = (int)strlen(val);
                    while (n > 0 && (val[n-1]=='\n'||val[n-1]=='\r'||val[n-1]==' '))
                        val[--n] = '\0';
                    write_str_file(dst, val);
                }
                fclose(f);
            }
        }
    }

    /* PSU state: dynamic — read every tick. */
    static const char *psu_attrs[] = {
        "psu1_present", "psu1_pgood",
        "psu2_present", "psu2_pgood",
        NULL
    };
    for (int i = 0; psu_attrs[i]; i++) {
        char src[128], dst[128], val[64];
        snprintf(src, sizeof(src), CPLD_SYSFS "/%s", psu_attrs[i]);
        snprintf(dst, sizeof(dst), RUN_DIR   "/%s", psu_attrs[i]);
        FILE *f = fopen(src, "r");
        if (!f) continue;
        if (fgets(val, (int)sizeof(val), f)) {
            int n = (int)strlen(val);
            while (n > 0 && (val[n-1]=='\n'||val[n-1]=='\r'||val[n-1]==' '))
                val[--n] = '\0';
            write_str_file(dst, val);
        }
        fclose(f);
    }
}
```

---

- [ ] **Step 2.8: Replace `main()` in `wedge100s-i2c-daemon.c`**

Replace the entire `main()` function (starting at line 1281) with:

```c
int main(void)
{
    int timer_fd, inotify_fd;
    struct itimerspec its = {
        .it_interval = {1, 0},
        .it_value    = {1, 0},
    };

    openlog("wedge100s-i2c-daemon", LOG_PID | LOG_NDELAY, LOG_DAEMON);
    mkdir(RUN_DIR, 0755);

    if (daemon_init() < 0) {
        syslog(LOG_ERR, "daemon_init failed — exiting for systemd restart");
        return 1;
    }

    timer_fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK);
    if (timer_fd < 0) {
        syslog(LOG_ERR, "timerfd_create: %s", strerror(errno));
        return 1;
    }
    timerfd_settime(timer_fd, 0, &its, NULL);

    inotify_fd = inotify_init1(IN_NONBLOCK);
    if (inotify_fd < 0) {
        syslog(LOG_ERR, "inotify_init1: %s", strerror(errno));
        return 1;
    }
    inotify_add_watch(inotify_fd, RUN_DIR, IN_CLOSE_WRITE);

    struct pollfd pfds[2] = {
        {.fd = timer_fd,   .events = POLLIN},
        {.fd = inotify_fd, .events = POLLIN},
    };

    syslog(LOG_INFO, "wedge100s-i2c-daemon: entering main loop (1s tick + inotify)");

    while (1) {
        int r = poll(pfds, 2, -1);
        if (r < 0) {
            if (errno == EINTR) continue;
            syslog(LOG_ERR, "poll: %s", strerror(errno));
            return 1;
        }

        /* inotify: write-request response (~50 ms latency) */
        if (pfds[1].revents & POLLIN) {
            drain_inotify(inotify_fd);
            service_write_requests();
        }

        /* timer: 1s tick — full poll cycle */
        if (pfds[0].revents & POLLIN) {
            uint64_t exp;
            (void)read(timer_fd, &exp, sizeof(exp));

            /*
             * cp2112_cancel() at tick-start drains the two stale HID input
             * reports left by each prior CPLD sysfs access.  Must run before
             * any hidraw operation this tick.
             */
            cp2112_cancel();

            /* hidraw poll functions (order matters — see comment in spec) */
            poll_syseeprom_hidraw();
            poll_presence_hidraw();
            poll_lpmode_hidraw();
            poll_write_requests_hidraw();
            poll_read_requests_hidraw();

            /*
             * apply_led_writes() and poll_cpld() run LAST: each CPLD sysfs
             * access leaves two stale HID reports; cp2112_cancel() at the
             * NEXT tick-start drains them.
             */
            apply_led_writes();
            poll_cpld();
        }
    }
    return 0; /* unreachable — suppresses gcc -O2 end-of-function warning */
}
```

---

- [ ] **Step 2.9: Remove old poller files**

```bash
git rm platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-i2c-poller.timer
git rm platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-i2c-poller.service
```

---

- [ ] **Step 2.10: Build to confirm no compile errors**

```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

If compile error: read the error, fix the C code, repeat. Do not deploy until clean build.

---

- [ ] **Step 2.11: Commit D2 (code only)**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-i2c-daemon.service
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c
git commit -m "feat(D2): i2c-daemon — timer+oneshot → persistent timerfd+inotify

- Replace one-shot ExecStart with persistent main loop:
  timerfd 1s + inotify IN_CLOSE_WRITE on /run/wedge100s/
- Add daemon_init(): opens hidraw0, cp2112_cancel(), mux_deselect_all()
  on startup/restart for crash recovery; BMC escalation on failure
- Add service_write_requests(): directory scan for pending *_req/*.set
  files, called on inotify events (~50 ms write-request latency)
- poll_cpld(): cpld_version now boot-once (was every 3s tick)
- Remove wedge100s-i2c-poller.timer and .service (superseded)
- Add wedge100s-i2c-daemon.service (Type=simple, Restart=on-failure)"
```

---

## Task 3: D3 — bmc-daemon: Timer+oneshot → Persistent Daemon

**Files:**
- Create: `service/wedge100s-bmc-daemon.service`
- Modify: `utils/wedge100s-bmc-daemon.c` (add bmc_connect/bmc_ensure_connected, new main loop, remove syscpld_led_ctrl)
- Remove: `service/wedge100s-bmc-poller.timer`, `service/wedge100s-bmc-poller.service`

**Dependency:** Task 1 complete (`wedge100s-bmc-auth` binary available at `/usr/bin/`).
**Parallel with Task 2** — D3 touches only bmc-daemon files.

---

- [ ] **Step 3.1: Write failing integration test for persistent bmc-daemon**

Add to `tests/stage_10_daemon/test_daemon.py` (uses `BMC_DAEMON` constant from Step 2.1):

```python
def test_bmc_daemon_running(ssh):
    """wedge100s-bmc-daemon.service is active (persistent daemon, D3)."""
    active = _systemctl_is_active(ssh, BMC_DAEMON)
    print(f"\n{BMC_DAEMON}: {'active' if active else 'INACTIVE'}")
    assert active, f"{BMC_DAEMON} not active — D3 not yet deployed"
```

Run to confirm FAIL:
```bash
cd tests && pytest stage_10_daemon/test_daemon.py::test_bmc_daemon_running -v
```
Expected: `FAILED`

---

- [ ] **Step 3.2: Create `service/wedge100s-bmc-daemon.service`**

Write to `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-bmc-daemon.service`:

```ini
[Unit]
Description=Wedge100S BMC sensor persistent daemon (SSH-based)
Documentation=file:///usr/bin/wedge100s-bmc-daemon.c
After=wedge100s-platform-init.service
Requires=wedge100s-platform-init.service

[Service]
Type=simple
ExecStart=/usr/bin/wedge100s-bmc-daemon
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

---

- [ ] **Step 3.3: Add new includes to `wedge100s-bmc-daemon.c`**

After the existing `#include <sys/types.h>`, add:

```c
#include <poll.h>
#include <syslog.h>
#include <sys/inotify.h>
#include <sys/timerfd.h>
```

---

- [ ] **Step 3.4: Replace SSH constants section in `wedge100s-bmc-daemon.c`**

The current `SSH_CTL`, `SSH_MASTER`, `SSH_EXIT` static strings and the `build_ssh_cmd()` helper are preserved. Add `SSH_CHECK` for connection liveness probe:

After the closing `}` of `build_ssh_cmd()` (around line 106), add:

```c
static const char SSH_CHECK[] =
    "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
    "-o ConnectTimeout=2 -i " BMC_KEY " "
    "-o ControlMaster=no -o ControlPath=" CTL_SOCK " "
    "-O check root@fe80::ff:fe00:1%usb0 2>/dev/null";
```

---

- [ ] **Step 3.5: Add `ssh_master_connect()`, `ssh_control_exit()`, `ssh_control_check()`, `bmc_connect()`, `bmc_ensure_connected()` to `wedge100s-bmc-daemon.c`**

Insert before the existing `main()`:

```c
/* ── connection management ─────────────────────────────────────────────── */

static int ssh_master_connect(void)
{
    if (system(SSH_MASTER) != 0) return -1;
    usleep(200000);  /* let master socket become ready */
    return 0;
}

static void ssh_control_exit(void)
{
    (void)system(SSH_EXIT);
}

/* Returns 0 if the ControlMaster socket is alive, non-zero otherwise. */
static int ssh_control_check(void)
{
    return system(SSH_CHECK);
}

/*
 * bmc_connect — push SSH key via TTY, establish ControlMaster, read
 * one-time values.
 *
 * Called on startup and on every reconnect.  Always re-pushes the key
 * because the BMC clears authorized_keys on every BMC reboot.
 */
static int bmc_connect(void)
{
    if (system("wedge100s-bmc-auth") != 0) {
        syslog(LOG_ERR, "wedge100s-bmc-daemon: key push via TTY failed");
        return -1;
    }
    if (ssh_master_connect() < 0) {
        syslog(LOG_ERR, "wedge100s-bmc-daemon: SSH ControlMaster failed");
        return -1;
    }

    /*
     * Read qsfp_led_position on every (re)connect — spec requires this.
     * No stat() guard: the value must be refreshed on every reconnect so
     * a prior stale file (from a crash or systemd restart) doesn't persist.
     * gpio59 is a board strap that is physically fixed, so re-reading it
     * unconditionally is safe and cheap (one SSH command per reconnect).
     */
    {
        char path[256];
        int val;
        snprintf(path, sizeof(path), RUN_DIR "/qsfp_led_position");
        if (bmc_read_int("cat /sys/class/gpio/gpio59/value", 10, &val) == 0)
            write_file(path, val);
    }

    syslog(LOG_INFO, "wedge100s-bmc-daemon: BMC connected");
    return 0;
}

/*
 * bmc_ensure_connected — check socket liveness; reconnect if dead.
 * Returns 0 if connected (or reconnect succeeded), -1 on failure.
 * On failure, existing /run/wedge100s/ files retain their last-good values.
 */
static int bmc_ensure_connected(void)
{
    if (ssh_control_check() == 0) return 0;   /* still alive */
    syslog(LOG_WARNING, "wedge100s-bmc-daemon: ControlMaster dead — reconnecting");
    ssh_control_exit();
    unlink(CTL_SOCK);
    return bmc_connect();
}
```

---

- [ ] **Step 3.6: Add `drain_inotify_bmc()` helper before `main()`**

(Identical pattern to the i2c-daemon version; named differently to avoid future linking issues if the two are ever compiled together.)

```c
static void drain_inotify_bmc(int inotify_fd)
{
    char ibuf[sizeof(struct inotify_event) + NAME_MAX + 1];
    while (read(inotify_fd, ibuf, sizeof(ibuf)) > 0)
        ;
}
```

---

- [ ] **Step 3.7: Replace `main()` in `wedge100s-bmc-daemon.c`**

The existing `main()` (line 152) reads all sensors once and exits. Replace it entirely with:

```c
int main(void)
{
    int timer_fd, inotify_fd;
    struct itimerspec its = {
        .it_interval = {10, 0},
        .it_value    = {10, 0},
    };
    char path[256];
    char cmd[512];
    int  val, i;

    /* Thermal sensor BMC sysfs paths (from thermali.c directory[]) */
    static const char *const thermal_paths[7] = {
        "/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/3-0049/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/3-004a/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/3-004b/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/3-004c/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/8-0048/hwmon/*/temp1_input",
        "/sys/bus/i2c/devices/8-0049/hwmon/*/temp1_input",
    };
    static const struct { int mux_ch; int pmbus_addr; } psu_cfg[2] = {
        { 0x02, 0x59 },
        { 0x01, 0x5a },
    };
    static const struct { int reg; const char *name; } pmbus_regs[4] = {
        { 0x88, "vin"  },
        { 0x89, "iin"  },
        { 0x8c, "iout" },
        { 0x96, "pout" },
    };

    openlog("wedge100s-bmc-daemon", LOG_PID | LOG_NDELAY, LOG_DAEMON);
    mkdir(RUN_DIR, 0755);

    if (bmc_connect() < 0) {
        syslog(LOG_ERR, "wedge100s-bmc-daemon: initial connect failed — exiting");
        return 1;
    }

    timer_fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK);
    if (timer_fd < 0) {
        syslog(LOG_ERR, "timerfd_create: %s", strerror(errno));
        return 1;
    }
    timerfd_settime(timer_fd, 0, &its, NULL);

    inotify_fd = inotify_init1(IN_NONBLOCK);
    if (inotify_fd < 0) {
        syslog(LOG_ERR, "inotify_init1: %s", strerror(errno));
        return 1;
    }
    inotify_add_watch(inotify_fd, RUN_DIR, IN_CLOSE_WRITE);

    struct pollfd pfds[2] = {
        {.fd = timer_fd,   .events = POLLIN},
        {.fd = inotify_fd, .events = POLLIN},
    };

    syslog(LOG_INFO, "wedge100s-bmc-daemon: entering main loop (10s tick + inotify)");

    while (1) {
        int r = poll(pfds, 2, -1);
        if (r < 0) {
            if (errno == EINTR) continue;
            syslog(LOG_ERR, "poll: %s", strerror(errno));
            return 1;
        }

        /* inotify: placeholder for future BMC write-request consumers */
        if (pfds[1].revents & POLLIN)
            drain_inotify_bmc(inotify_fd);

        /* timer: 10s tick — full BMC sensor poll */
        if (pfds[0].revents & POLLIN) {
            uint64_t exp;
            (void)read(timer_fd, &exp, sizeof(exp));

            if (bmc_ensure_connected() < 0) {
                syslog(LOG_WARNING,
                       "wedge100s-bmc-daemon: BMC unavailable — skipping tick");
                continue;
            }

            /* qsfp_int — diagnostic presence interrupt */
            {
                snprintf(path, sizeof(path), RUN_DIR "/qsfp_int");
                if (bmc_read_int("cat /sys/class/gpio/gpio31/value",
                                 10, &val) == 0)
                    write_file(path, val);
            }

            /* thermal sensors */
            for (i = 0; i < 7; i++) {
                snprintf(cmd,  sizeof(cmd),  "cat %s", thermal_paths[i]);
                snprintf(path, sizeof(path), RUN_DIR "/thermal_%d", i + 1);
                if (bmc_read_int(cmd, 10, &val) == 0)
                    write_file(path, val);
            }

            /* fan-tray presence */
            {
                snprintf(path, sizeof(path), RUN_DIR "/fan_present");
                if (bmc_read_int(
                        "cat /sys/bus/i2c/devices/8-0033/fantray_present",
                        0, &val) == 0)
                    write_file(path, val);
            }

            /* fan RPM */
            for (i = 1; i <= 5; i++) {
                snprintf(cmd, sizeof(cmd),
                         "cat /sys/bus/i2c/devices/8-0033/fan%d_input",
                         i * 2 - 1);
                snprintf(path, sizeof(path), RUN_DIR "/fan_%d_front", i);
                if (bmc_read_int(cmd, 10, &val) == 0)
                    write_file(path, val);

                snprintf(cmd, sizeof(cmd),
                         "cat /sys/bus/i2c/devices/8-0033/fan%d_input",
                         i * 2);
                snprintf(path, sizeof(path), RUN_DIR "/fan_%d_rear", i);
                if (bmc_read_int(cmd, 10, &val) == 0)
                    write_file(path, val);
            }

            /* PSU PMBus */
            for (i = 0; i < 2; i++) {
                int r2;
                snprintf(cmd, sizeof(cmd), "i2cset -f -y 7 0x70 0x%02x",
                         psu_cfg[i].mux_ch);
                bmc_run(cmd);

                for (r2 = 0; r2 < 4; r2++) {
                    snprintf(cmd, sizeof(cmd),
                             "i2cget -f -y 7 0x%02x 0x%02x w",
                             psu_cfg[i].pmbus_addr, pmbus_regs[r2].reg);
                    snprintf(path, sizeof(path), RUN_DIR "/psu_%d_%s",
                             i + 1, pmbus_regs[r2].name);
                    if (bmc_read_int(cmd, 0, &val) == 0)
                        write_file(path, val);
                }
            }
        }
    }
    return 0; /* unreachable — suppresses gcc -O2 end-of-function warning */
}
```

**Note:** The original `main()` had a write-request block for `syscpld_led_ctrl.set`. That block is removed entirely — D1 supersedes it. The `qsfp_led_position` read is now in `bmc_connect()` (once per reconnect), not in the main loop.

---

- [ ] **Step 3.8: Remove old bmc poller files**

```bash
git rm platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-bmc-poller.timer
git rm platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-bmc-poller.service
```

---

- [ ] **Step 3.9: Build to confirm no compile errors**

```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

---

- [ ] **Step 3.10: Commit D3 (code only)**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-bmc-daemon.service
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c
git commit -m "feat(D3): bmc-daemon — timer+oneshot → persistent timerfd+inotify

- Replace one-shot ExecStart with persistent main loop:
  timerfd 10s + inotify IN_CLOSE_WRITE on /run/wedge100s/
- Add bmc_connect(): wedge100s-bmc-auth key push + SSH ControlMaster;
  reads qsfp_led_position once per (re)connect
- Add bmc_ensure_connected(): dead socket detection → full reconnect
- Remove syscpld_led_ctrl read/write (superseded by D1)
- Remove wedge100s-bmc-poller.timer and .service (superseded)
- Add wedge100s-bmc-daemon.service (Type=simple, Restart=on-failure 5s)"
```

---

## Task 4: Integration — postinst + tests + deploy + verify

**Files:**
- Modify: `debian/sonic-platform-accton-wedge100s-32x.postinst`
- Modify: `tests/stage_10_daemon/test_daemon.py`

**Dependency:** Tasks 2 and 3 both committed.

---

- [ ] **Step 4.1: Update `postinst` — swap poller units for daemon units**

In `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`:

Find the Phase R28 / Phase EOS block (lines 32–47):
```sh
# Phase R28: enable BMC sensor polling daemon timer.
...
systemctl enable wedge100s-bmc-poller.timer
systemctl start  wedge100s-bmc-poller.timer || true

# Phase EOS: enable QSFP presence + EEPROM cache daemon timer.
...
systemctl enable wedge100s-i2c-poller.timer
systemctl start  wedge100s-i2c-poller.timer || true
```

Replace with:
```sh
# D3: enable BMC sensor persistent daemon.
# Replaced timer+oneshot with timerfd-driven persistent process.
mkdir -p /run/wedge100s
systemctl enable wedge100s-bmc-daemon.service
systemctl start  wedge100s-bmc-daemon.service || true

# D2: enable QSFP I2C persistent daemon.
# Replaced timer+oneshot with timerfd+inotify driven persistent process.
systemctl enable wedge100s-i2c-daemon.service
systemctl start  wedge100s-i2c-daemon.service || true
```

Also add a block to stop and disable the old poller units if they are still present (handles upgrades from the pre-D2/D3 deb):
```sh
# Migration: stop and disable old poller timer units if still present.
# Use unconditional disable (not guarded by is-enabled): systemd reports
# "not-found" for units whose files have been removed but which still have
# enabled symlinks, causing is-enabled to return non-zero incorrectly.
for unit in wedge100s-bmc-poller.timer wedge100s-bmc-poller.service \
            wedge100s-i2c-poller.timer wedge100s-i2c-poller.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
    echo "wedge100s postinst: disabled legacy unit $unit (if present)"
done
```

Add the migration block immediately before the new enable/start lines.

---

- [ ] **Step 4.2: Update `tests/stage_10_daemon/test_daemon.py` — full overhaul**

Replace the timer/service constants and tests section. The new constants at the top of the test file:

```python
I2C_SERVICE = "wedge100s-i2c-daemon.service"
BMC_SERVICE = "wedge100s-bmc-daemon.service"
```

Remove `I2C_TIMER`, `BMC_TIMER` constants. Update `test_i2c_timer_active` and `test_bmc_timer_active` to instead assert that the **old** timer units are NOT active and the new daemon units ARE active:

```python
def test_i2c_daemon_active(ssh):
    """wedge100s-i2c-daemon.service is active (persistent daemon)."""
    active = _systemctl_is_active(ssh, I2C_SERVICE)
    print(f"\n{I2C_SERVICE}: {'active' if active else 'INACTIVE'}")
    assert active, (
        f"{I2C_SERVICE} is not active.\n"
        f"Fix: sudo systemctl start {I2C_SERVICE}"
    )


def test_bmc_daemon_active(ssh):
    """wedge100s-bmc-daemon.service is active (persistent daemon)."""
    active = _systemctl_is_active(ssh, BMC_SERVICE)
    print(f"\n{BMC_SERVICE}: {'active' if active else 'INACTIVE'}")
    assert active, (
        f"{BMC_SERVICE} is not active.\n"
        f"Fix: sudo systemctl start {BMC_SERVICE}"
    )


def test_old_i2c_poller_timer_absent(ssh):
    """wedge100s-i2c-poller.timer is NOT active (migration regression guard)."""
    out, _, _ = ssh.run("systemctl is-active wedge100s-i2c-poller.timer 2>&1",
                        timeout=10)
    state = out.strip()
    print(f"\nwedge100s-i2c-poller.timer state: {state!r}")
    assert state != "active", (
        "Old wedge100s-i2c-poller.timer is still active — migration incomplete.\n"
        "Fix: sudo systemctl disable --now wedge100s-i2c-poller.timer"
    )


def test_old_bmc_poller_timer_absent(ssh):
    """wedge100s-bmc-poller.timer is NOT active (migration regression guard)."""
    out, _, _ = ssh.run("systemctl is-active wedge100s-bmc-poller.timer 2>&1",
                        timeout=10)
    state = out.strip()
    print(f"\nwedge100s-bmc-poller.timer state: {state!r}")
    assert state != "active", (
        "Old wedge100s-bmc-poller.timer is still active — migration incomplete.\n"
        "Fix: sudo systemctl disable --now wedge100s-bmc-poller.timer"
    )
```

Update the fix-hint messages in the existing cache file tests to reference the new service names:
- Any `{I2C_SERVICE}` → `wedge100s-i2c-daemon.service`
- Any `{I2C_TIMER}` → `wedge100s-i2c-daemon.service`
- Any `{BMC_SERVICE}` → `wedge100s-bmc-daemon.service`
- Any `{BMC_TIMER}` → `wedge100s-bmc-daemon.service`

Update the module-level docstring to reflect the new service names and 1s/10s intervals.

Also update `test_qsfp_presence_cache_fresh` staleness comment to reflect the 1s tick:
```python
assert age < STALE_THRESHOLD_S, (
    f"sfp_0_present is {age}s old (>{STALE_THRESHOLD_S}s threshold).\n"
    f"I2C daemon may not be running: sudo systemctl start {I2C_SERVICE}"
)
```

---

- [ ] **Step 4.3: Build the final .deb**

```bash
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Verify the new service files are in the package and old timer files are absent:
```bash
dpkg -c target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb | grep -E "service|timer"
```

Expected output contains:
```
./lib/systemd/system/wedge100s-i2c-daemon.service
./lib/systemd/system/wedge100s-bmc-daemon.service
./lib/systemd/system/wedge100s-platform-init.service
```

Must NOT contain `wedge100s-i2c-poller.timer`, `wedge100s-bmc-poller.timer`, etc.

---

- [ ] **Step 4.4: Deploy to target**

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 "sudo systemctl stop pmon && \
    sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && \
    sudo systemctl start pmon"
```

---

- [ ] **Step 4.5: Verify new daemons running on target**

```bash
ssh admin@192.168.88.12 "systemctl is-active wedge100s-i2c-daemon.service wedge100s-bmc-daemon.service"
```

Expected: both return `active`

```bash
ssh admin@192.168.88.12 "systemctl is-active wedge100s-i2c-poller.timer wedge100s-bmc-poller.timer 2>&1"
```

Expected: `inactive` or `Unit not found` for both

---

- [ ] **Step 4.6: Run stage 10 daemon tests**

```bash
cd tests && pytest stage_10_daemon/ -v --tb=short
```

Expected: all tests pass. If any cache file tests fail, wait 30 s and retry (daemon needs time to populate files after cold start).

---

- [ ] **Step 4.7: Run full test suite**

```bash
cd tests && python3 run_tests.py
```

Watch for regressions in stage_08_led, stage_09_cpld, and stage_03_platform.

---

- [ ] **Step 4.8: Commit integration**

```bash
git add platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
git add tests/stage_10_daemon/test_daemon.py
git commit -m "feat(D2+D3 integration): postinst + tests for persistent daemons

- postinst: enable wedge100s-{i2c,bmc}-daemon.service (replaces poller timers)
- postinst: migration block disables old poller units on upgrade
- test_daemon.py: test new daemon service units, regression-guard old timers
- test_daemon.py: update service name references and staleness comments"
```

---

## Task 5: D4 — Link + Speed LED Hardware Verification

**Files:**
- Modify: `notes/SUBSYSTEMS_LED.md` (append hardware findings)

**Dependency:** Task 1 deployed on hardware (`th_led_en=1` confirmed).

---

- [ ] **Step 5.1: Confirm `th_led_en=1` on hardware**

```bash
ssh root@192.168.88.13 "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en"
```

Expected: `1`. If `0`, run Task 1 hardware steps first.

---

- [ ] **Step 5.2: Bring up a test port with known-speed QSFP28**

```bash
ssh admin@192.168.88.12 "sudo config interface startup Ethernet0"
ssh admin@192.168.88.12 "sudo ip link set Ethernet0 up"
```

Wait 5 seconds, then check link state:
```bash
ssh admin@192.168.88.12 "sudo show interfaces status Ethernet0"
```

---

- [ ] **Step 5.3: Observe front-panel LED color and document**

With the port link-up, physically observe the front-panel LED for port 0 (Ethernet0). Note:
- LED color (green or amber)
- Link speed reported by `show interfaces status`

For a second data point, if a lower-speed QSFP or DAC is available, insert it and check the other color.

**Decision tree:**
- Colors correct (green=100G or green=higher-speed, amber=lower-speed): document and close.
- Colors inverted: update `led_proc_init.soc` with corrected bytecode (create a follow-up plan).
- All LEDs off despite `th_led_en=1`: investigate `qsfp_led_position` strap (gpio59).

---

- [ ] **Step 5.4: Update `notes/SUBSYSTEMS_LED.md`**

Append a new section to `notes/SUBSYSTEMS_LED.md`:

```markdown
## Port LEDs (BCM LEDUP / Front Panel)

### Hardware path

syscpld register `0x3c` (BMC i2c-12 / addr `0x31`) must have:
- `th_led_en=1` (bit 1): enables BCM LEDUP output to front-panel connectors
- `led_test_mode_en=0`, `led_test_blink_en=0`, `walk_test_en=0`: all test modes off

Factory default at hardware power-on: `0xe0` (all test bits set, LEDUP gated).
After D1: platform-init deploys `clear_led_diag.sh` to BMC, patches
`setup_board.sh`, and runs it every boot, permanently setting `th_led_en=1`.

### BCM LED program

`led_proc_init.soc` (identical to AS7712-32X) drives two LEDUP channels:
- LEDUP0: green channel
- LEDUP1: amber channel

### Hardware test results (verified YYYY-MM-DD)

| Condition | LEDUP0 (green) | LEDUP1 (amber) | Notes |
|---|---|---|---|
| 100G link-up | ... | ... | Ethernet0, <module type> |
| Lower-speed link-up | ... | ... | If tested |
| Link-down | off | off | |

**Color mapping:** [Fill in after D4 hardware test]

### `qsfp_led_position` strap (gpio59)

BMC gpio59 determines LED chain scan direction. Value read at bmc-daemon startup:
```bash
ssh root@192.168.88.13 "cat /sys/class/gpio/gpio59/value"
```
Value: [Fill in] — [chain direction based on value]
```

Replace the `YYYY-MM-DD` and `[Fill in]` placeholders with actual observed values.

---

- [ ] **Step 5.5: Commit D4**

```bash
git add notes/SUBSYSTEMS_LED.md
git commit -m "docs(D4): document port LED hardware verification results

- Add port LED section: syscpld 0x3c path, BCM LEDUP capabilities
- Record hardware-observed green/amber speed mapping
- Document qsfp_led_position strap value"
```

---

## Verification Summary

After all five tasks, confirm:

```bash
# Hardware: th_led_en=1 and setup_board.sh patched
ssh root@192.168.88.13 "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en && grep clear_led_diag /etc/init.d/setup_board.sh"

# Daemons: both persistent daemons running, no poller timers
ssh admin@192.168.88.12 "systemctl is-active wedge100s-i2c-daemon wedge100s-bmc-daemon"
ssh admin@192.168.88.12 "systemctl list-units 'wedge100s-*' --all"

# Cache files fresh
ssh admin@192.168.88.12 "ls -la /run/wedge100s/ | head -20"

# Full test suite
cd tests && python3 run_tests.py
```
