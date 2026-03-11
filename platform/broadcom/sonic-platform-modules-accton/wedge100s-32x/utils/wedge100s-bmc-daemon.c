/*
 * wedge100s-bmc-daemon.c — Single-session BMC sensor polling daemon.
 *
 * Opens /dev/ttyACM0 once, logs into OpenBMC, reads all sensors in one
 * continuous TTY session (no open/close per command), writes results to
 * plain-integer files in /run/wedge100s/, then exits.
 *
 * Invoked by wedge100s-bmc-poller.timer every 10 seconds.
 *
 * Problem solved:
 *   bmc.py re-opens the TTY and re-logs in for every command.  With
 *   ~28 commands needed for a full poll cycle that costs ~65 seconds per
 *   thermalctld cycle.  This daemon keeps the session alive for all reads
 *   and completes the full poll in ~3-5 seconds.
 *
 * Output files — all plain decimal integers in /run/wedge100s/:
 *   thermal_{1..7}              TMP75 temperature in millidegrees C
 *   fan_present                 bitmask (0 = all trays present; bit set = absent)
 *   fan_{1..5}_front            front-rotor RPM
 *   fan_{1..5}_rear             rear-rotor RPM
 *   psu_{1,2}_{vin,iin,iout,pout}  raw PMBus LINEAR11 16-bit word (decimal)
 *
 * Sensor sources (from ONL thermali.c / fani.c / psui.c):
 *   Thermal:  BMC i2c-3 (0x48-0x4c) and i2c-8 (0x48-0x49), sysfs hwmon
 *   Fan:      BMC i2c-8, fan-board controller at 0x33, sysfs
 *   PSU:      BMC i2c-7, PCA9546 mux at 0x70, PMBus 0x59/0x5a
 *
 * TTY design (see bmc.py design notes for full rationale):
 *   - Blocking I/O with VMIN=1 (ttyACM/USB-CDC does not signal select()
 *     correctly under O_NONBLOCK on this kernel)
 *   - select() provides per-read timeouts
 *   - Prompt pattern ":~# " matches any OpenBMC root shell hostname
 *   - Null byte appended to each write (mirrors ONL write(fd,buf,strlen+1))
 *
 * Build: gcc -O2 -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <errno.h>
#include <time.h>
#include <termios.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <sys/types.h>

/* ── constants ─────────────────────────────────────────────────────────── */

#define TTY_DEVICE      "/dev/ttyACM0"
#define TTY_PROMPT      ":~# "          /* root@HOSTNAME:~#  — any hostname */
#define RUN_DIR         "/run/wedge100s"
#define BUF_SIZE        8192            /* per-command response buffer      */

#define CMD_TIMEOUT     8.0             /* seconds: wait for prompt         */
#define LOGIN_TIMEOUT   5.0             /* seconds: wait for login steps    */
#define TTY_OPEN_RETRY  20              /* attempts to open /dev/ttyACM0    */
#define LOGIN_RETRY     10              /* attempts to reach shell prompt   */

/* ── globals ────────────────────────────────────────────────────────────── */

static int g_fd = -1;                   /* TTY file descriptor              */

/* ── time helper ─────────────────────────────────────────────────────────── */

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* ── TTY open / close ────────────────────────────────────────────────────── */

static int tty_open(void)
{
    struct termios attr;
    int flags, i;

    for (i = 0; i < TTY_OPEN_RETRY; i++) {
        g_fd = open(TTY_DEVICE, O_RDWR | O_NOCTTY | O_NONBLOCK);
        if (g_fd >= 0) {
            tcgetattr(g_fd, &attr);
            /* 57600 8N1 raw — mirrors bmc.py and ONL platform_lib.c */
            attr.c_cflag  = B57600 | CS8 | CLOCAL | CREAD;
            attr.c_iflag  = IGNPAR;
            attr.c_oflag  = 0;
            attr.c_lflag  = 0;
            attr.c_cc[VMIN]  = 1;
            attr.c_cc[VTIME] = 0;
            cfsetispeed(&attr, B57600);
            cfsetospeed(&attr, B57600);
            tcsetattr(g_fd, TCSANOW, &attr);
            /* Switch to blocking I/O with VMIN=1 so select() works.
             * ttyACM (USB CDC) does not signal select() correctly when
             * opened with O_NONBLOCK on this kernel.                   */
            flags = fcntl(g_fd, F_GETFL);
            fcntl(g_fd, F_SETFL, flags & ~O_NONBLOCK);
            return 0;
        }
        usleep(100000);
    }
    return -1;
}

static void tty_close(void)
{
    if (g_fd >= 0) {
        close(g_fd);
        g_fd = -1;
    }
}

/* ── read_until ──────────────────────────────────────────────────────────── */
/*
 * Accumulate bytes into buf (at most bufsz-1) until needle is found or
 * timeout expires.  buf is always NUL-terminated.  Returns bytes read.
 */
static int read_until(char *buf, int bufsz, const char *needle, double timeout)
{
    double deadline = now_sec() + timeout;
    int len = 0;

    buf[0] = '\0';
    while (now_sec() < deadline && len < bufsz - 1) {
        double rem = deadline - now_sec();
        if (rem <= 0.0) break;
        struct timeval tv;
        tv.tv_sec  = (long)rem;
        tv.tv_usec = (long)((rem - tv.tv_sec) * 1e6);
        fd_set rds;
        FD_ZERO(&rds);
        FD_SET(g_fd, &rds);
        int r = select(g_fd + 1, &rds, NULL, NULL, &tv);
        if (r <= 0) break;
        int n = (int)read(g_fd, buf + len, bufsz - 1 - len);
        if (n <= 0) break;
        len += n;
        buf[len] = '\0';
        if (strstr(buf, needle) != NULL) break;
    }
    return len;
}

/* ── drain ───────────────────────────────────────────────────────────────── */
/*
 * Discard pending input; wait settle ms after last byte.
 * Mirrors bmc.py _drain(); clears echo/prompt leftovers before each send.
 */
static void drain(void)
{
    char tmp[256];
    double deadline = now_sec() + 0.1;  /* 100 ms settle */

    while (now_sec() < deadline) {
        struct timeval tv = {0, 50000};
        fd_set rds;
        FD_ZERO(&rds);
        FD_SET(g_fd, &rds);
        if (select(g_fd + 1, &rds, NULL, NULL, &tv) <= 0) break;
        if (read(g_fd, tmp, sizeof(tmp)) <= 0) break;
    }
}

/* ── tty_login ───────────────────────────────────────────────────────────── */
/*
 * Bring the TTY to the shell prompt.  Mirrors bmc.py _tty_login().
 * Handles: already-logged-in, login: prompt, and Password: prompt.
 */
static int tty_login(void)
{
    char buf[BUF_SIZE];
    int i;

    for (i = 0; i < LOGIN_RETRY; i++) {
        /* One CR refreshes the prompt without double-prompt race */
        write(g_fd, "\r\x00", 2);
        read_until(buf, sizeof(buf), TTY_PROMPT, 1.0);
        if (strstr(buf, TTY_PROMPT)) return 0;

        if (strstr(buf, " login:")) {
            write(g_fd, "root\r\x00", 6);
            read_until(buf, sizeof(buf), "Password:", LOGIN_TIMEOUT);
            if (strstr(buf, "Password:")) {
                write(g_fd, "0penBmc\r\x00", 9);
                read_until(buf, sizeof(buf), TTY_PROMPT, LOGIN_TIMEOUT);
                if (strstr(buf, TTY_PROMPT)) return 0;
            }
        }
        usleep(50000);
    }
    return -1;
}

/* ── send_cmd ────────────────────────────────────────────────────────────── */
/*
 * Write cmd to the TTY, read until prompt, store full response in out.
 * Returns 0 when prompt received, -1 on timeout.
 *
 * The null byte after \r\n mirrors ONL's write(fd, buf, strlen(buf)+1).
 */
static int send_cmd(const char *cmd, char *out, int outsz)
{
    char line[512];
    int  n;

    n = snprintf(line, sizeof(line) - 1, "%s\r\n", cmd);
    line[n]     = '\x00';   /* null terminator after \r\n */
    line[n + 1] = '\0';

    drain();
    write(g_fd, line, n + 1);   /* includes the null byte */

    read_until(out, outsz, TTY_PROMPT, CMD_TIMEOUT);
    return strstr(out, TTY_PROMPT) ? 0 : -1;
}

/* ── parse_last_int ──────────────────────────────────────────────────────── */
/*
 * Find the LAST occurrence of cmd in resp (cmd echo), then parse the first
 * whitespace-delimited numeric token that follows it.
 *
 * base: 10 for decimal sysfs files, 0 for auto (handles "0x..." i2cget output).
 *
 * Returns INT_MIN on failure (no match or no parseable token).
 * Mirrors bmc.py _parse_int() which uses rfind() + int(token, base).
 */
static int parse_last_int(const char *resp, const char *cmd, int base)
{
    const char *p    = resp;
    const char *last = NULL;
    int         clen = (int)strlen(cmd);
    char        tmp[256];
    char       *tok, *end;
    long        val;

    /* Locate the last echo of cmd in the response */
    while ((p = strstr(p, cmd)) != NULL) {
        last = p;
        p   += clen;
    }
    if (!last) return INT_MIN;

    /* Tokenise the text after the echo; parse the first numeric token */
    strncpy(tmp, last + clen, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = '\0';

    tok = strtok(tmp, " \t\r\n");
    while (tok) {
        errno = 0;
        val   = strtol(tok, &end, base);
        if (end != tok && errno == 0)
            return (int)val;
        tok = strtok(NULL, " \t\r\n");
    }
    return INT_MIN;
}

/* ── write_file ──────────────────────────────────────────────────────────── */
/*
 * Write a decimal integer (and newline) to path.
 * On failure, the previous file value is preserved (harmless for stale data).
 */
static int write_file(const char *path, int value)
{
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fprintf(f, "%d\n", value);
    fclose(f);
    return 0;
}

/* ── bmc_read_int ────────────────────────────────────────────────────────── */
/*
 * Send cmd, parse the integer result, store in *result.
 * Returns 0 on success, -1 on TTY timeout or parse failure.
 */
static int bmc_read_int(const char *cmd, int base, int *result)
{
    char resp[BUF_SIZE];
    int  v;

    if (send_cmd(cmd, resp, sizeof(resp)) < 0) return -1;
    v = parse_last_int(resp, cmd, base);
    if (v == INT_MIN) return -1;
    *result = v;
    return 0;
}

/* ── bmc_send_only ───────────────────────────────────────────────────────── */
/* Send cmd, ignore output (used for i2cset mux select). */
static void bmc_send_only(const char *cmd)
{
    char resp[BUF_SIZE];
    send_cmd(cmd, resp, sizeof(resp));
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(void)
{
    char path[256];
    char cmd[256];
    char resp[BUF_SIZE];
    int  val;
    int  i;

    /* Thermal sensor BMC sysfs paths (from thermali.c directory[]).
     * The hwmon wildcard (*) is expanded by the BMC shell.            */
    static const char *const thermal_paths[7] = {
        "/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input",  /* TMP75-1 */
        "/sys/bus/i2c/devices/3-0049/hwmon/*/temp1_input",  /* TMP75-2 */
        "/sys/bus/i2c/devices/3-004a/hwmon/*/temp1_input",  /* TMP75-3 */
        "/sys/bus/i2c/devices/3-004b/hwmon/*/temp1_input",  /* TMP75-4 */
        "/sys/bus/i2c/devices/3-004c/hwmon/*/temp1_input",  /* TMP75-5 */
        "/sys/bus/i2c/devices/8-0048/hwmon/*/temp1_input",  /* TMP75-6 */
        "/sys/bus/i2c/devices/8-0049/hwmon/*/temp1_input",  /* TMP75-7 */
    };

    /* PSU mux channels and PMBus addresses (from psui.c) */
    static const struct {
        int mux_ch;
        int pmbus_addr;
    } psu_cfg[2] = {
        { 0x02, 0x59 },  /* PSU1: mux channel 0x02, PMBus 0x59 */
        { 0x01, 0x5a },  /* PSU2: mux channel 0x01, PMBus 0x5a */
    };

    /* PMBus registers to read (LINEAR11 format) */
    static const struct {
        int         reg;
        const char *name;
    } pmbus_regs[4] = {
        { 0x88, "vin"  },   /* READ_VIN  — AC input voltage  */
        { 0x89, "iin"  },   /* READ_IIN  — AC input current  */
        { 0x8c, "iout" },   /* READ_IOUT — DC output current */
        { 0x96, "pout" },   /* READ_POUT — DC output power   */
    };

    /* ── ensure output directory exists ─────────────────────────────────── */
    mkdir(RUN_DIR, 0755);

    /* ── open TTY ────────────────────────────────────────────────────────── */
    if (tty_open() < 0) {
        fprintf(stderr, "wedge100s-bmc-daemon: cannot open %s\n", TTY_DEVICE);
        return 1;
    }

    /* ── login ───────────────────────────────────────────────────────────── */
    if (tty_login() < 0) {
        fprintf(stderr, "wedge100s-bmc-daemon: BMC login failed\n");
        tty_close();
        return 1;
    }

    /* ── thermal sensors (7 × cat) ───────────────────────────────────────── */
    for (i = 0; i < 7; i++) {
        snprintf(cmd,  sizeof(cmd),  "cat %s", thermal_paths[i]);
        snprintf(path, sizeof(path), RUN_DIR "/thermal_%d", i + 1);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);
        /* On failure: silently skip; previous file value remains. */
    }

    /* ── fan-tray presence (1 × cat, hex bitmask) ───────────────────────── */
    {
        const char *fc = "cat /sys/bus/i2c/devices/8-0033/fantray_present";
        snprintf(path, sizeof(path), RUN_DIR "/fan_present");
        if (send_cmd(fc, resp, sizeof(resp)) == 0) {
            val = parse_last_int(resp, fc, 0);   /* base=0: handles "0x0" */
            if (val != INT_MIN)
                write_file(path, val);
        }
    }

    /* ── fan RPM (5 trays × 2 rotors = 10 × cat) ───────────────────────── */
    for (i = 1; i <= 5; i++) {
        /* front rotor: fan(tray*2 - 1)_input  per fani.c fid*2-1 */
        snprintf(cmd,  sizeof(cmd),  "cat /sys/bus/i2c/devices/8-0033/fan%d_input",
                 i * 2 - 1);
        snprintf(path, sizeof(path), RUN_DIR "/fan_%d_front", i);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);

        /* rear rotor: fan(tray*2)_input  per fani.c fid*2 */
        snprintf(cmd,  sizeof(cmd),  "cat /sys/bus/i2c/devices/8-0033/fan%d_input",
                 i * 2);
        snprintf(path, sizeof(path), RUN_DIR "/fan_%d_rear", i);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);
    }

    /* ── PSU PMBus (2 PSUs × (1 mux-set + 4 word-reads)) ─────────────────── */
    for (i = 0; i < 2; i++) {
        int r;

        /* Select PCA9546 channel: single-byte write (no register prefix) */
        snprintf(cmd, sizeof(cmd), "i2cset -f -y 7 0x70 0x%02x",
                 psu_cfg[i].mux_ch);
        bmc_send_only(cmd);

        /* Read PMBus registers; i2cget -w returns "0xNNNN" */
        for (r = 0; r < 4; r++) {
            snprintf(cmd, sizeof(cmd), "i2cget -f -y 7 0x%02x 0x%02x w",
                     psu_cfg[i].pmbus_addr, pmbus_regs[r].reg);
            snprintf(path, sizeof(path), RUN_DIR "/psu_%d_%s",
                     i + 1, pmbus_regs[r].name);
            if (send_cmd(cmd, resp, sizeof(resp)) == 0) {
                val = parse_last_int(resp, cmd, 0);  /* 0: auto-detect 0x... */
                if (val != INT_MIN)
                    write_file(path, val);
            }
        }
    }

    /* ── done ────────────────────────────────────────────────────────────── */
    tty_close();
    return 0;
}
