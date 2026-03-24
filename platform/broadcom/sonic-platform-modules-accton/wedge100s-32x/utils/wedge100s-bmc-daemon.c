/*
 * wedge100s-bmc-daemon.c — BMC sensor polling daemon (SSH-based).
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
 *     daemon reads, dispatches via SSH, removes file.  Sysfs attribute
 *     writes are used (not raw i2cset) to go through the syscpld kernel
 *     driver's i2c lock.
 *
 * Output files — all plain decimal integers in /run/wedge100s/:
 *   thermal_{1..7}                 TMP75 temperature in millidegrees C
 *   fan_present                    bitmask (0 = all present; bit set = absent)
 *   fan_{1..5}_front               front-rotor RPM
 *   fan_{1..5}_rear                rear-rotor RPM
 *   psu_{1,2}_{vin,iin,iout,pout}  raw PMBus LINEAR11 16-bit word (decimal)
 *   syscpld_led_ctrl               syscpld register 0x3c (LED control byte)
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
/*
 * Write a decimal integer (and newline) to path.
 * On failure the previous file value is preserved.
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
/*
 * Construct a full SSH command by concatenating the static SSH_CTL prefix
 * with the quoted BMC command.  Avoids using SSH_CTL as a printf format
 * string (which would misinterpret the literal '%' in the IPv6 zone-id).
 */
static void build_ssh_cmd(char *buf, size_t bufsz,
                           const char *bmc_cmd, const char *suffix)
{
    size_t pfx = strlen(SSH_CTL);
    if (pfx >= bufsz) { buf[0] = '\0'; return; }
    memcpy(buf, SSH_CTL, pfx);
    snprintf(buf + pfx, bufsz - pfx, "'%s'%s", bmc_cmd, suffix ? suffix : "");
}

/* ── bmc_run ─────────────────────────────────────────────────────────────── */
/* Run a BMC shell command via the ControlMaster socket; ignore output. */
static void bmc_run(const char *bmc_cmd)
{
    char shell_cmd[512];
    build_ssh_cmd(shell_cmd, sizeof(shell_cmd), bmc_cmd, " >/dev/null 2>&1");
    (void)system(shell_cmd);
}

/* ── bmc_read_int ────────────────────────────────────────────────────────── */
/*
 * Run bmc_cmd via SSH, parse the first output line as an integer.
 * base: 10 for decimal sysfs, 0 for auto (handles "0x…" i2cget output).
 * Returns 0 on success, -1 on SSH failure or parse failure.
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

/* ── main ────────────────────────────────────────────────────────────────── */

int main(void)
{
    char path[256];
    char cmd[512];
    int  val;
    int  i;

    /* Thermal sensor BMC sysfs paths (from thermali.c directory[]) */
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
    static const struct { int mux_ch; int pmbus_addr; } psu_cfg[2] = {
        { 0x02, 0x59 },  /* PSU1: mux channel 0x02, PMBus 0x59 */
        { 0x01, 0x5a },  /* PSU2: mux channel 0x01, PMBus 0x5a */
    };

    /* PMBus registers to read (LINEAR11 format) */
    static const struct { int reg; const char *name; } pmbus_regs[4] = {
        { 0x88, "vin"  },   /* READ_VIN  — AC input voltage  */
        { 0x89, "iin"  },   /* READ_IIN  — AC input current  */
        { 0x8c, "iout" },   /* READ_IOUT — DC output current */
        { 0x96, "pout" },   /* READ_POUT — DC output power   */
    };

    /* ── ensure output directory exists ─────────────────────────────────── */
    mkdir(RUN_DIR, 0755);

    /* ── establish SSH ControlMaster ─────────────────────────────────────── */
    /*
     * Opens a background non-interactive SSH session.  All subsequent
     * bmc_run/bmc_read_int calls reuse this socket (ControlMaster=no),
     * paying only one handshake per 10 s polling cycle.
     *
     * -f:  fork to background after authentication
     * -N:  no remote command (keeps the master alive)
     */
    if (system(SSH_MASTER) != 0) {
        fprintf(stderr, "wedge100s-bmc-daemon: SSH ControlMaster failed\n");
        return 1;
    }
    /* Brief pause so the master socket is ready before we send commands. */
    usleep(200000);

    /* ── process write-requests ──────────────────────────────────────────── */
    /*
     * Platform code writes /run/wedge100s/syscpld_led_ctrl.set with the
     * desired register 0x3c value (decimal or hex).  We decode it into
     * individual sysfs attribute writes — going through the syscpld kernel
     * driver (not raw i2cset) to respect the driver's i2c bus lock.
     *
     * Bit map for syscpld register 0x3c:
     *   bit 7: led_test_mode_en   bit 6: led_test_blink_en
     *   [5:4]: th_led_steam       bit 3: walk_test_en
     *   bit 1: th_led_en          bit 0: th_led_clr
     */
    {
        const char *set_path = RUN_DIR "/syscpld_led_ctrl.set";
        FILE *sf = fopen(set_path, "r");
        if (sf) {
            int set_val = 0;
            if (fscanf(sf, "%i", &set_val) == 1) {
                static const char SD[] = "/sys/bus/i2c/devices/12-0031";
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/led_test_mode_en",
                         (set_val >> 7) & 1, SD);
                bmc_run(cmd);
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/led_test_blink_en",
                         (set_val >> 6) & 1, SD);
                bmc_run(cmd);
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/th_led_steam",
                         (set_val >> 4) & 3, SD);
                bmc_run(cmd);
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/walk_test_en",
                         (set_val >> 3) & 1, SD);
                bmc_run(cmd);
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/th_led_en",
                         (set_val >> 1) & 1, SD);
                bmc_run(cmd);
                snprintf(cmd, sizeof(cmd),
                         "echo %d > %s/th_led_clr",
                          set_val       & 1, SD);
                bmc_run(cmd);
            }
            fclose(sf);
            unlink(set_path);
        }
    }

    /* ── syscpld LED control register (0x3c) — read every cycle ─────────── */
    {
        snprintf(path, sizeof(path), RUN_DIR "/syscpld_led_ctrl");
        if (bmc_read_int("i2cget -f -y 12 0x31 0x3c", 0, &val) == 0)
            write_file(path, val);
    }

    /* ── QSFP presence interrupt (BMC AST GPIOD7 / gpio31, active-low) ───── */
    /*
     * 0 = interrupt asserted; lets i2c-daemon trigger an immediate presence
     * scan instead of waiting up to 3 s for the next scheduled poll.
     */
    {
        snprintf(path, sizeof(path), RUN_DIR "/qsfp_int");
        if (bmc_read_int("cat /sys/class/gpio/gpio31/value", 10, &val) == 0)
            write_file(path, val);
    }

    /* ── LED chain orientation strap (GPIOH3 / gpio59) — written once ────── */
    {
        struct stat st;
        snprintf(path, sizeof(path), RUN_DIR "/qsfp_led_position");
        if (stat(path, &st) != 0) {
            if (bmc_read_int("cat /sys/class/gpio/gpio59/value", 10, &val) == 0)
                write_file(path, val);
        }
    }

    /* ── thermal sensors (7 × cat) ───────────────────────────────────────── */
    for (i = 0; i < 7; i++) {
        snprintf(cmd,  sizeof(cmd),  "cat %s", thermal_paths[i]);
        snprintf(path, sizeof(path), RUN_DIR "/thermal_%d", i + 1);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);
    }

    /* ── fan-tray presence (hex bitmask) ─────────────────────────────────── */
    {
        snprintf(path, sizeof(path), RUN_DIR "/fan_present");
        if (bmc_read_int("cat /sys/bus/i2c/devices/8-0033/fantray_present",
                         0, &val) == 0)
            write_file(path, val);
    }

    /* ── fan RPM (5 trays × 2 rotors) ───────────────────────────────────── */
    for (i = 1; i <= 5; i++) {
        /* front rotor: fan(tray*2 - 1)_input  per fani.c fid*2-1 */
        snprintf(cmd,  sizeof(cmd),
                 "cat /sys/bus/i2c/devices/8-0033/fan%d_input", i * 2 - 1);
        snprintf(path, sizeof(path), RUN_DIR "/fan_%d_front", i);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);

        /* rear rotor: fan(tray*2)_input  per fani.c fid*2 */
        snprintf(cmd,  sizeof(cmd),
                 "cat /sys/bus/i2c/devices/8-0033/fan%d_input", i * 2);
        snprintf(path, sizeof(path), RUN_DIR "/fan_%d_rear", i);
        if (bmc_read_int(cmd, 10, &val) == 0)
            write_file(path, val);
    }

    /* ── PSU PMBus (2 PSUs × (mux-select + 4 word-reads)) ─────────────────── */
    for (i = 0; i < 2; i++) {
        int r;

        /* Select PCA9546 channel */
        snprintf(cmd, sizeof(cmd), "i2cset -f -y 7 0x70 0x%02x",
                 psu_cfg[i].mux_ch);
        bmc_run(cmd);

        /* Read PMBus registers; i2cget -w returns "0xNNNN" */
        for (r = 0; r < 4; r++) {
            snprintf(cmd, sizeof(cmd), "i2cget -f -y 7 0x%02x 0x%02x w",
                     psu_cfg[i].pmbus_addr, pmbus_regs[r].reg);
            snprintf(path, sizeof(path), RUN_DIR "/psu_%d_%s",
                     i + 1, pmbus_regs[r].name);
            if (bmc_read_int(cmd, 0, &val) == 0)
                write_file(path, val);
        }
    }

    /* ── close ControlMaster ─────────────────────────────────────────────── */
    (void)system(SSH_EXIT);

    return 0;
}
