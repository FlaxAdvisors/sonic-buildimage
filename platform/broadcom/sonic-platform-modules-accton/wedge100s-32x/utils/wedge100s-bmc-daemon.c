/**
 * @file wedge100s-bmc-daemon.c
 * @brief BMC sensor polling daemon for the Wedge 100S-32X (SSH-based).
 *
 * Establishes an SSH ControlMaster session to the BMC over the CDC-ECM
 * USB link (usb0, IPv6 link-local fe80::ff:fe00:1%usb0), reads all sensors
 * and GPIO state via multiplexed SSH commands, writes plain-integer files
 * to /run/wedge100s/, then exits.
 *
 * Invoked by wedge100s-bmc-poller.timer every 10 seconds.
 *
 * Design:
 *   - ControlMaster (-f -N) established once per invocation; all subsequent
 *     commands reuse the socket with ControlMaster=no.  Overhead is one
 *     SSH handshake per 10 s cycle instead of one per command.
 *   - popen(ssh … 'bmc-cmd') replaces the TTY send_cmd/read_until loop.
 *     Output is clean (no echo, no prompt noise): first line → strtol().
 *   - Write-requests: platform code writes /run/wedge100s/<file>.set;
 *     dispatch_write_requests() detects via inotify, runs the mapped BMC
 *     command via SSH, removes the .set file.  Sysfs attribute writes are
 *     used on the BMC side (not raw i2cset) to go through the syscpld
 *     kernel driver's i2c lock.  See write_requests[] dispatch table.
 *
 * Output files — all plain decimal integers in /run/wedge100s/:
 *   thermal_{1..7}                 TMP75 temperature in millidegrees C
 *   fan_present                    bitmask (0 = all present; bit set = absent)
 *   fan_{1..5}_front               front-rotor RPM
 *   fan_{1..5}_rear                rear-rotor RPM
 *   psu_{1,2}_{vin,iin,iout,pout}  raw PMBus LINEAR11 16-bit word (decimal)
 *   qsfp_int                       BMC gpio31 value (0 = interrupt asserted)
 *   qsfp_led_position              BMC gpio59 board strap (written once)
 *
 * Sensor sources (from ONL thermali.c / fani.c / psui.c):
 *   Thermal:  BMC i2c-3 (0x48-0x4c) and i2c-8 (0x48-0x49), sysfs hwmon
 *   Fan:      BMC i2c-8, fan-board controller at 0x33, sysfs
 *   PSU:      BMC i2c-7, PCA9546 mux at 0x70, PMBus 0x59/0x5a
 *
 * Build: gcc -O2 -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <errno.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <poll.h>
#include <syslog.h>
#include <sys/inotify.h>
#include <sys/timerfd.h>

/* ── constants ─────────────────────────────────────────────────────────── */

#define BMC_HOST    "root@fe80::ff:fe00:1%%usb0"   /* %% → literal % in cmd */
#define BMC_KEY     "/etc/sonic/wedge100s-bmc-key"
#define CTL_SOCK    "/run/wedge100s/.bmc-ctl"
#define RUN_DIR     "/run/wedge100s"

/*
 * SSH prefix for bmc_run / bmc_read_int.
 * Not used as a printf format string; appended literally via strncat.
 * The literal % in the BMC address (IPv6 zone-id) requires no escaping here.
 */
static const char SSH_CTL[] =
    "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
    "-o ConnectTimeout=5 -i " BMC_KEY " "
    "-o ControlMaster=no -o ControlPath=" CTL_SOCK " "
    "root@fe80::ff:fe00:1%usb0 ";

static const char SSH_MASTER[] =
    "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
    "-o ConnectTimeout=5 -i " BMC_KEY " "
    "-o ControlMaster=yes -o ControlPath=" CTL_SOCK " "
    "-f -N root@fe80::ff:fe00:1%usb0 2>/dev/null";

static const char SSH_EXIT[] =
    "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
    "-i " BMC_KEY " "
    "-o ControlPath=" CTL_SOCK " "
    "-O exit root@fe80::ff:fe00:1%usb0 2>/dev/null";

/* ── write_file ──────────────────────────────────────────────────────────── */
/**
 * @brief Write a decimal integer followed by a newline to a file.
 *
 * On failure the previous file content is preserved (the file is not
 * truncated before the write attempt).
 *
 * @param path  Absolute path of the file to write.
 * @param value Integer value to write.
 * @return 0 on success, -1 if fopen() failed.
 */
static int write_file(const char *path, int value)
{
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fprintf(f, "%d\n", value);
    fclose(f);
    return 0;
}

/* ── build_ssh_cmd ───────────────────────────────────────────────────────── */
/**
 * @brief Construct a complete SSH command string for a BMC remote command.
 *
 * Concatenates the static SSH_CTL prefix (ControlMaster=no socket reuse)
 * with the quoted BMC command and an optional suffix. Avoids using SSH_CTL
 * as a printf format string to prevent misinterpretation of the literal '%'
 * in the IPv6 zone-id.
 *
 * @param buf     Output buffer for the assembled shell command.
 * @param bufsz   Size of buf in bytes.
 * @param bmc_cmd BMC shell command to execute (will be single-quoted).
 * @param suffix  Optional string appended after the quoted command (e.g.
 *                " >/dev/null 2>&1"), or NULL for none.
 */
static void build_ssh_cmd(char *buf, size_t bufsz,
                           const char *bmc_cmd, const char *suffix)
{
    size_t pfx = strlen(SSH_CTL);
    if (pfx >= bufsz) { buf[0] = '\0'; return; }
    memcpy(buf, SSH_CTL, pfx);
    snprintf(buf + pfx, bufsz - pfx, "'%s'%s", bmc_cmd, suffix ? suffix : "");
}

static const char SSH_CHECK[] =
    "ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
    "-o ConnectTimeout=2 -i " BMC_KEY " "
    "-o ControlMaster=no -o ControlPath=" CTL_SOCK " "
    "-O check root@fe80::ff:fe00:1%usb0 2>/dev/null";

/* ── bmc_run ─────────────────────────────────────────────────────────────── */
/**
 * @brief Execute a BMC shell command via the SSH ControlMaster socket.
 *
 * Output (stdout and stderr) is discarded. Used for fire-and-forget
 * commands such as i2cset and LED diagnostic script invocations.
 *
 * @param bmc_cmd Shell command to run on the BMC.
 */
static void bmc_run(const char *bmc_cmd)
{
    char shell_cmd[512];
    build_ssh_cmd(shell_cmd, sizeof(shell_cmd), bmc_cmd, " >/dev/null 2>&1");
    (void)system(shell_cmd);
}

/* ── bmc_read_int ────────────────────────────────────────────────────────── */
/**
 * @brief Run a BMC command via SSH and parse its first output line as an integer.
 *
 * Uses popen() over the ControlMaster socket. Strips trailing newline before
 * parsing with strtol().
 *
 * @param bmc_cmd Shell command to run on the BMC.
 * @param base    Numeric base for strtol(): 10 for decimal sysfs values,
 *                0 for auto-detect (handles "0x..." i2cget output).
 * @param result  Out-parameter populated with the parsed integer on success.
 * @return 0 on success, -1 on SSH failure, empty output, or parse error.
 */
static int bmc_read_int(const char *bmc_cmd, int base, int *result)
{
    char shell_cmd[512];
    char line[128];
    char *end;
    long val;
    FILE *fp;

    build_ssh_cmd(shell_cmd, sizeof(shell_cmd), bmc_cmd, " 2>/dev/null");
    fp = popen(shell_cmd, "r");
    if (!fp) return -1;

    line[0] = '\0';
    fgets(line, sizeof(line), fp);
    pclose(fp);

    line[strcspn(line, "\r\n")] = '\0';   /* strip newline */
    if (!line[0]) return -1;

    errno = 0;
    val   = strtol(line, &end, base);
    if (end == line || errno != 0) return -1;

    *result = (int)val;
    return 0;
}

/* ── connection management ─────────────────────────────────────────────── */

/**
 * @brief Establish the SSH ControlMaster session to the BMC.
 *
 * Runs ssh with ControlMaster=yes and -f -N to background the master.
 * Sleeps 200 ms after the system() call to allow the socket to become ready.
 *
 * @return 0 on success, -1 if ssh exits non-zero.
 */
static int ssh_master_connect(void)
{
    if (system(SSH_MASTER) != 0) return -1;
    usleep(200000);  /* let master socket become ready */
    return 0;
}

/**
 * @brief Send an ssh -O exit command to terminate the ControlMaster socket.
 */
static void ssh_control_exit(void)
{
    (void)system(SSH_EXIT);
}

/**
 * @brief Check whether the SSH ControlMaster socket is still alive.
 *
 * Uses ssh -O check against the ControlPath socket.
 *
 * @return 0 if the socket is alive, non-zero otherwise.
 */
static int ssh_control_check(void)
{
    return system(SSH_CHECK);
}

/**
 * @brief Push the SSH public key to the BMC and establish the ControlMaster.
 *
 * Calls wedge100s-bmc-auth to push the key via /dev/ttyACM0, then starts the
 * SSH ControlMaster session. Always re-pushes the key because the BMC clears
 * authorized_keys on every BMC reboot. Also reads and caches qsfp_led_position
 * (gpio59 board strap) on every (re)connect.
 *
 * @return 0 on success, -1 if key push or ControlMaster setup fails.
 */
static int bmc_connect(void)
{
    if (system("/usr/bin/wedge100s-bmc-auth") != 0) {
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

/**
 * @brief Ensure the BMC SSH ControlMaster socket is alive, reconnecting if needed.
 *
 * Calls ssh_control_check(); if the socket is dead, closes it, removes the
 * stale socket file, and calls bmc_connect() to re-establish. On failure,
 * existing /run/wedge100s/ files retain their last-good values.
 *
 * @return 0 if connected (or reconnect succeeded), -1 on reconnect failure.
 */
static int bmc_ensure_connected(void)
{
    if (ssh_control_check() == 0) return 0;   /* still alive */
    syslog(LOG_WARNING, "wedge100s-bmc-daemon: ControlMaster dead — reconnecting");
    ssh_control_exit();
    unlink(CTL_SOCK);
    return bmc_connect();
}

/* Dispatch table: .set filename → BMC command */
static const struct {
    const char *setfile;
    const char *bmc_cmd;
} write_requests[] = {
    { "clear_led_diag.set", "/usr/local/bin/clear_led_diag.sh" },
};

/**
 * @brief Drain inotify events and dispatch pending write-request files to the BMC.
 *
 * Reads IN_CLOSE_WRITE events from inotify_fd. For each .set file detected,
 * handles named special cases (cpld_led_ctrl.set, led_ctrl_write.set,
 * led_color_read.set) then falls through to the write_requests[] dispatch
 * table. Request files are unlinked after processing.
 *
 * @param inotify_fd File descriptor returned by inotify_init1().
 */
static void dispatch_write_requests(int inotify_fd)
{
    char ibuf[sizeof(struct inotify_event) + NAME_MAX + 1];
    ssize_t n;

    while ((n = read(inotify_fd, ibuf, sizeof(ibuf))) > 0) {
        struct inotify_event *ev = (struct inotify_event *)ibuf;
        char path[256];
        size_t i, nlen;

        if (!(ev->mask & IN_CLOSE_WRITE) || ev->len == 0)
            continue;

        nlen = strlen(ev->name);
        if (nlen < 4 || strcmp(ev->name + nlen - 4, ".set") != 0)
            continue;

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

        /* Special case: cpld_led_ctrl.set → read register, write result file */
        if (strcmp(ev->name, "cpld_led_ctrl.set") == 0) {
            int val;
            syslog(LOG_INFO, "wedge100s-bmc-daemon: reading CPLD 0x3c");
            if (bmc_ensure_connected() == 0 &&
                bmc_read_int("i2cget -f -y 12 0x31 0x3c", 0, &val) == 0) {
                snprintf(path, sizeof(path), RUN_DIR "/cpld_led_ctrl");
                write_file(path, val);
            }
            continue;
        }

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

        for (i = 0; i < sizeof(write_requests) / sizeof(write_requests[0]); i++) {
            if (strcmp(ev->name, write_requests[i].setfile) == 0) {
                syslog(LOG_INFO, "wedge100s-bmc-daemon: dispatching %s", ev->name);
                if (bmc_ensure_connected() == 0)
                    bmc_run(write_requests[i].bmc_cmd);
                break;
            }
        }
    }
}

/* ── main ────────────────────────────────────────────────────────────────── */

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

        /* inotify: dispatch .set write-requests to BMC */
        if (pfds[1].revents & POLLIN)
            dispatch_write_requests(inotify_fd);

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
