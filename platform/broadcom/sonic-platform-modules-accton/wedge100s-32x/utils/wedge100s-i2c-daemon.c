/*
 * wedge100s-i2c-daemon.c — Event-driven QSFP presence + EEPROM cache daemon.
 *
 * Usage: wedge100s-i2c-daemon poll-presence
 *
 * Invoked every 3 s by wedge100s-i2c-poller.timer (one-shot systemd service).
 *
 * On each invocation:
 *   1. Reads system EEPROM once at first boot (if /run/wedge100s/syseeprom absent).
 *   2. Reads PCA9535 presence via i2c-dev ioctl (4 reads: buses 36/37, 0x22/0x23).
 *   3. For each port:
 *        - absent:              deletes sfp_N_eeprom, writes sfp_N_present="0"
 *        - insertion/retry:     reads 256 bytes from optoe1 sysfs, writes both files
 *        - stable (present+cached): rewrites sfp_N_present="1", skips EEPROM I2C
 *        - present, no cache:   writes sfp_N_present="1", retries EEPROM next tick
 *
 * Presence decoding: ONL sfpi.c XOR-1 interleave (line ^ 1).
 * PCA9535 INPUT registers are active-low (0 = module present).
 *
 * Key property: this binary is the sole entity that initiates kernel I2C
 * transactions for QSFP EEPROMs and PCA9535 presence chips.  pmon Python
 * consumers read /run/wedge100s/ files only — no direct mux-tree I2C.
 *
 * Build: gcc -O2 -o wedge100s-i2c-daemon wedge100s-i2c-daemon.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>

/* ── constants ─────────────────────────────────────────────────────────── */

#define RUN_DIR         "/run/wedge100s"
#define NUM_PORTS       32
#define EEPROM_SIZE     256
#define SYSEEPROM_SIZE  8192

/* PCA9535 presence chips (mux 0x74 ch2/ch3) */
static const int PCA9535_BUS[2]  = { 36, 37 };
static const int PCA9535_ADDR[2] = { 0x22, 0x23 };

/* Port-to-bus map (sfp_bus_index[] from ONL sfpi.c, 0-indexed) */
static const int SFP_BUS_MAP[NUM_PORTS] = {
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    19, 18, 21, 20, 23, 22, 25, 24,
    27, 26, 29, 28, 31, 30, 33, 32,
};

/* ONIE TlvInfo magic: "TlvInfo\0" */
static const unsigned char ONIE_MAGIC[8] = {
    0x54, 0x6c, 0x76, 0x49, 0x6e, 0x66, 0x6f, 0x00
};

/* ── i2c helpers ────────────────────────────────────────────────────────── */

/*
 * Read a single byte from I2C device at (bus, addr, reg) via SMBus ioctl.
 * Uses I2C_SMBUS_BYTE_DATA (write register pointer, then read one byte).
 * Returns byte value [0..255] on success, -1 on error.
 */
static int i2c_read_byte_data(int bus, int addr, int reg)
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

    args.read_write = I2C_SMBUS_READ;
    args.command    = (unsigned char)reg;
    args.size       = I2C_SMBUS_BYTE_DATA;
    args.data       = &data;

    if (ioctl(fd, I2C_SMBUS, &args) < 0) {
        close(fd);
        return -1;
    }

    close(fd);
    return (int)(data.byte & 0xFF);
}

/* ── file helpers ───────────────────────────────────────────────────────── */

static int write_str_file(const char *path, const char *str)
{
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fputs(str, f);
    fclose(f);
    return 0;
}

static int write_binary_file(const char *path, const unsigned char *buf, int len)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    int written = (int)fwrite(buf, 1, (size_t)len, f);
    fclose(f);
    return (written == len) ? 0 : -1;
}

/* ── syseeprom — read once at first boot ─────────────────────────────────── */

/*
 * Read the 24c64 system EEPROM (mux 0x74 ch6, i2c-40/0x50) once.
 * /run/wedge100s/syseeprom is on tmpfs — absent after every boot.
 * Validates ONIE TlvInfo magic before writing to prevent storing corrupt data.
 */
static void poll_syseeprom(void)
{
    char cache_path[64];
    char sysfs_path[64];
    struct stat st;
    unsigned char buf[SYSEEPROM_SIZE];
    FILE *f;
    int nread;

    snprintf(cache_path, sizeof(cache_path), RUN_DIR "/syseeprom");

    /* If already cached: skip (static hardware data never changes) */
    if (stat(cache_path, &st) == 0) return;

    snprintf(sysfs_path, sizeof(sysfs_path),
             "/sys/bus/i2c/devices/40-0050/eeprom");

    f = fopen(sysfs_path, "rb");
    if (!f) {
        fprintf(stderr, "wedge100s-i2c-daemon: syseeprom: cannot open %s: %s\n",
                sysfs_path, strerror(errno));
        return;
    }

    nread = (int)fread(buf, 1, sizeof(buf), f);
    fclose(f);

    if (nread < 8) {
        fprintf(stderr,
                "wedge100s-i2c-daemon: syseeprom: short read (%d bytes)\n",
                nread);
        return;
    }

    if (memcmp(buf, ONIE_MAGIC, 8) != 0) {
        fprintf(stderr,
                "wedge100s-i2c-daemon: syseeprom: invalid magic "
                "(got %02x %02x %02x %02x...); hardware may be corrupted\n",
                buf[0], buf[1], buf[2], buf[3]);
        return;
    }

    if (write_binary_file(cache_path, buf, nread) < 0) {
        fprintf(stderr,
                "wedge100s-i2c-daemon: syseeprom: write failed: %s\n",
                strerror(errno));
    }
}

/* ── poll_presence — presence + event-driven EEPROM ─────────────────────── */

/*
 * Read PCA9535 presence bitmaps; update /run/wedge100s/sfp_N_present files.
 * On insertion or first-boot with module present: read 256-byte EEPROM page 0
 * from optoe1 sysfs and write /run/wedge100s/sfp_N_eeprom.
 * On removal: delete sfp_N_eeprom immediately (stale data must not be served).
 */
static void poll_presence(void)
{
    int curr_present[NUM_PORTS];
    int port;

    /* Initialise to absent; filled in by PCA9535 reads below */
    memset(curr_present, 0, sizeof(curr_present));

    /* Read all 4 PCA9535 INPUT registers (2 chips × 2 registers) */
    for (int g = 0; g < 2; g++) {
        for (int r = 0; r < 2; r++) {
            int byte = i2c_read_byte_data(PCA9535_BUS[g], PCA9535_ADDR[g], r);
            if (byte < 0) {
                fprintf(stderr,
                        "wedge100s-i2c-daemon: PCA9535 read failed "
                        "(bus %d addr 0x%02x reg %d): %s\n",
                        PCA9535_BUS[g], PCA9535_ADDR[g], r, strerror(errno));
                /* Affected ports remain absent — safe default */
                continue;
            }
            for (int bit = 0; bit < 8; bit++) {
                int line = r * 8 + bit;
                int p    = g * 16 + (line ^ 1);  /* XOR-1 interleave */
                curr_present[p] = !((byte >> bit) & 1); /* active-low */
            }
        }
    }

    for (port = 0; port < NUM_PORTS; port++) {
        char present_path[64];
        char eeprom_path[64];

        snprintf(present_path, sizeof(present_path),
                 RUN_DIR "/sfp_%d_present", port);
        snprintf(eeprom_path,  sizeof(eeprom_path),
                 RUN_DIR "/sfp_%d_eeprom",  port);

        if (!curr_present[port]) {
            /*
             * Module absent: delete cached EEPROM (stale data must not be
             * served to sfp.py) and write present=0.
             */
            unlink(eeprom_path);
            write_str_file(present_path, "0");
            continue;
        }

        /*
         * Module present — determine whether EEPROM needs to be read.
         * Triggers: insertion (prev != "1") or previous read failed (no file).
         */
        struct stat est;
        int eeprom_exists = (stat(eeprom_path, &est) == 0);

        char prev_val[8] = {0};
        FILE *pf = fopen(present_path, "r");
        if (pf) {
            if (fgets(prev_val, (int)sizeof(prev_val), pf))
                prev_val[strcspn(prev_val, "\r\n")] = '\0';
            fclose(pf);
        }
        int prev_present = (strcmp(prev_val, "1") == 0);

        if (prev_present && eeprom_exists) {
            /*
             * Stable: module present, EEPROM already cached.
             * Rewrite present file (updates mtime for sfp.py staleness check).
             * Skip EEPROM I2C entirely — this is the common steady-state path.
             */
            write_str_file(present_path, "1");
            continue;
        }

        /*
         * Insertion event (or retry after failed EEPROM read):
         * Read 256 bytes from the optoe1 sysfs file.
         *
         * If the read fails (module still warming up, I2C glitch):
         *   - do NOT write sfp_N_eeprom
         *   - sfp.py falls back to direct sysfs
         *   - on the next 3 s tick: prev=1, no file → retries automatically
         *
         * If the read succeeds: write sfp_N_eeprom.
         * In either case: write sfp_N_present="1".
         */
        {
            char sysfs_eeprom[80];
            int bus = SFP_BUS_MAP[port];
            snprintf(sysfs_eeprom, sizeof(sysfs_eeprom),
                     "/sys/bus/i2c/devices/%d-0050/eeprom", bus);

            FILE *ef = fopen(sysfs_eeprom, "rb");
            if (ef) {
                unsigned char ebuf[EEPROM_SIZE];
                int nread = (int)fread(ebuf, 1, EEPROM_SIZE, ef);
                fclose(ef);
                if (nread == EEPROM_SIZE)
                    write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
                /* else: partial read — do not write partial data */
            }
            /* ef == NULL: sysfs not yet ready; retry next tick */
        }

        write_str_file(present_path, "1");
    }
}

/* ── main ───────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    /* Ensure output directory exists (tmpfs, recreated each boot) */
    mkdir(RUN_DIR, 0755);

    if (argc < 2 || strcmp(argv[1], "poll-presence") != 0) {
        fprintf(stderr, "Usage: %s poll-presence\n", argv[0]);
        return 1;
    }

    poll_syseeprom();
    poll_presence();

    return 0;
}
