/*
 * wedge100s-i2c-daemon.c — Event-driven QSFP presence + EEPROM cache daemon.
 *
 * Usage: wedge100s-i2c-daemon poll-presence
 *
 * Invoked every 3 s by wedge100s-i2c-poller.timer (one-shot systemd service).
 *
 * On each invocation:
 *   1. Reads system EEPROM once at first boot (if /run/wedge100s/syseeprom absent).
 *   2. Reads PCA9535 presence for all 32 ports.
 *   3. For each port:
 *        - absent:                deletes sfp_N_eeprom, writes sfp_N_present="0"
 *        - insertion/retry:       reads 256 bytes of EEPROM page 0, writes both files
 *        - stable (present+file): rewrites sfp_N_present="1", skips EEPROM I2C
 *        - present, no cache:     writes sfp_N_present="1", retries EEPROM next tick
 *
 * Two runtime paths, selected at startup:
 *
 *   Phase 2 — hidraw direct (preferred):
 *     Opens /dev/hidraw0 and communicates with the CP2112 USB-HID bridge via
 *     raw AN495 HID reports, bypassing the kernel I2C stack entirely.  This
 *     eliminates i2c_mux_pca954x and optoe1 from the mux-tree read path, removing
 *     the probe-write attack surface that corrupted the existing QSFP EEPROMs.
 *     PCA9535 presence reads, QSFP EEPROM reads, and system EEPROM reads all go
 *     through this path.
 *
 *   Phase 1 — sysfs/i2c-dev fallback (when /dev/hidraw0 is unavailable):
 *     PCA9535 via i2c-dev ioctl on buses 36/37; QSFP EEPROM via optoe1 sysfs;
 *     system EEPROM via at24 sysfs.  This path requires hid_cp2112,
 *     i2c_mux_pca954x, optoe1, and at24 to be loaded.
 *
 * CP2112 HID report IDs (from Linux kernel hid-cp2112.c, confirmed 6.12.41):
 *   0x10  DATA_READ_REQUEST
 *   0x11  DATA_WRITE_READ_REQUEST
 *   0x12  DATA_READ_FORCE_SEND     [len_hi][len_lo] (__be16)
 *   0x13  DATA_READ_RESPONSE       [ignored][valid_len][data...]
 *   0x14  DATA_WRITE_REQUEST       [addr<<1][len][data...]
 *   0x15  TRANSFER_STATUS_REQUEST  [0x01]
 *   0x16  TRANSFER_STATUS_RESPONSE [status0][status1]...
 *   0x17  CANCEL_TRANSFER          [0x01]
 *
 * Presence decoding: ONL sfpi.c XOR-1 interleave (line ^ 1).
 * PCA9535 INPUT registers are active-low (0 = module present).
 *
 * Key property: this binary is the sole entity that initiates CP2112 mux-tree
 * I2C transactions.  pmon Python consumers read /run/wedge100s/ files only.
 *
 * Build: gcc -O2 -o wedge100s-i2c-daemon wedge100s-i2c-daemon.c
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/select.h>
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

/*
 * SFF-8024 Table 4-1 identifier validity check.
 * Valid identifiers are non-zero and assigned sequentially by the SFF
 * committee; the current ceiling is well below 0x30 (QSFP-DD variants,
 * OSFP-XD, etc.).  Anything in [0x01, 0x7f] is a plausible present or
 * future module type and should be cached.  0x00 means unspecified/blank;
 * 0x80-0xff is the garbage/bit-corruption range (e.g. 0xb3, 0xff).
 */
#define EEPROM_ID_VALID(id)  ((id) >= 0x01 && (id) <= 0x7f)

/* PCA9535 presence chips (mux 0x74 ch2/ch3) */
static const int PCA9535_BUS[2]  = { 36, 37 };
static const int PCA9535_ADDR[2] = { 0x22, 0x23 };

/* PCA9535 mux channels (mux 0x74: ch2 → bus 36, ch3 → bus 37) */
static const int PCA9535_MUX_CHAN[2] = { 2, 3 };

/* Port-to-bus map (sfp_bus_index[] from ONL sfpi.c, 0-indexed, confirmed SONiC 6.1.0) */
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

/* ── CP2112 HID protocol constants ──────────────────────────────────────── */
/*
 * Report IDs from Linux kernel hid-cp2112.c (drivers/hid/hid-cp2112.c).
 * Confirmed against kernel 6.12.41.
 */
#define CP2112_DATA_READ_REQUEST        0x10
#define CP2112_DATA_WRITE_READ_REQUEST  0x11
#define CP2112_DATA_READ_FORCE_SEND     0x12
#define CP2112_DATA_READ_RESPONSE       0x13
#define CP2112_DATA_WRITE_REQUEST       0x14
#define CP2112_TRANSFER_STATUS_REQUEST  0x15
#define CP2112_TRANSFER_STATUS_RESPONSE 0x16
#define CP2112_CANCEL_TRANSFER          0x17

/* Transfer status byte (byte[1] of TRANSFER_STATUS_RESPONSE) */
#define CP2112_STATUS_IDLE     0x00
#define CP2112_STATUS_BUSY     0x01
#define CP2112_STATUS_COMPLETE 0x02
#define CP2112_STATUS_ERROR    0x03

#define CP2112_REPORT_SIZE   64   /* all HID reports padded to 64 bytes */
#define CP2112_MAX_READ_DATA 61   /* max data bytes in one read-response report */

/* hidraw fd; -1 = not open (Phase 1 fallback active) */
static int g_hidraw_fd = -1;

/* ── hidraw transport ────────────────────────────────────────────────────── */

/* Send a HID output report (padded to 64 bytes). */
static int hid_send(const uint8_t *report, int len)
{
    uint8_t buf[CP2112_REPORT_SIZE] = {0};
    if (len > CP2112_REPORT_SIZE) len = CP2112_REPORT_SIZE;
    memcpy(buf, report, len);
    ssize_t r = write(g_hidraw_fd, buf, CP2112_REPORT_SIZE);
    return (r == CP2112_REPORT_SIZE) ? 0 : -1;
}

/* Receive a HID input report with timeout in milliseconds. Returns bytes read, -1 on timeout/error. */
static int hid_recv(uint8_t *buf, int timeout_ms)
{
    fd_set rfds;
    struct timeval tv;
    FD_ZERO(&rfds);
    FD_SET(g_hidraw_fd, &rfds);
    tv.tv_sec  = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    int r = select(g_hidraw_fd + 1, &rfds, NULL, NULL, &tv);
    if (r <= 0) return -1;
    return (int)read(g_hidraw_fd, buf, CP2112_REPORT_SIZE);
}

/* Cancel any outstanding CP2112 transfer and drain pending input reports. */
static void cp2112_cancel(void)
{
    uint8_t req[2] = {CP2112_CANCEL_TRANSFER, 0x01};
    hid_send(req, 2);
    /* drain up to 8 stale reports (50 ms each max) */
    uint8_t drain[CP2112_REPORT_SIZE];
    for (int i = 0; i < 8; i++) {
        if (hid_recv(drain, 5) < 0) break;
    }
}

/*
 * Poll TRANSFER_STATUS_REQUEST until status is Complete or Error.
 * Returns 0 on Complete/Idle, -1 on Error or timeout.
 *
 * The CP2112 can only execute one I2C transfer at a time.  We poll with
 * TRANSFER_STATUS_REQUEST and inspect status byte[1]:
 *   0x00 = Idle   (no transfer — treat as OK for write-only ops)
 *   0x01 = Busy   (in progress — retry)
 *   0x02 = Complete
 *   0x03 = Error
 */
static int cp2112_wait_complete(void)
{
    uint8_t req[2] = {CP2112_TRANSFER_STATUS_REQUEST, 0x01};
    uint8_t resp[CP2112_REPORT_SIZE];

    for (int i = 0; i < 100; i++) {
        if (hid_send(req, 2) < 0) return -1;
        int r = hid_recv(resp, 20);
        if (r < 2) { usleep(1000); continue; }
        if (resp[0] != CP2112_TRANSFER_STATUS_RESPONSE) { usleep(1000); continue; }
        switch (resp[1]) {
        case CP2112_STATUS_COMPLETE: return 0;
        case CP2112_STATUS_IDLE:     return 0;
        case CP2112_STATUS_ERROR:    cp2112_cancel(); return -1;
        case CP2112_STATUS_BUSY:
        default:                     usleep(2000); break;
        }
    }
    cp2112_cancel();
    return -1;  /* timeout */
}

/*
 * Write len bytes of data to 7-bit I2C addr via CP2112.
 * Report: [0x14][addr<<1][len][data[0..len-1]]
 * Returns 0 on success, -1 on error.
 */
static int cp2112_write(uint8_t addr, const uint8_t *data, int len)
{
    if (len < 1 || len > 61) return -1;
    uint8_t report[CP2112_REPORT_SIZE] = {0};
    report[0] = CP2112_DATA_WRITE_REQUEST;
    report[1] = addr << 1;
    report[2] = (uint8_t)len;
    memcpy(&report[3], data, len);
    if (hid_send(report, 3 + len) < 0) return -1;
    return cp2112_wait_complete();
}

/*
 * Collect read data after a completed read or write-read request.
 * Issues DATA_READ_FORCE_SEND reports (up to 61 bytes each) until
 * read_len bytes are accumulated in buf.
 *
 * Force-send report: [0x12][len_hi][len_lo]  (__be16 — high byte first)
 * Read-response:     [0x13][status][valid_len][data...]
 *
 * Returns total bytes collected, or -1 on error.
 */
static int cp2112_collect(uint8_t *buf, int read_len)
{
    int received = 0;
    while (received < read_len) {
        int chunk = read_len - received;
        if (chunk > CP2112_MAX_READ_DATA) chunk = CP2112_MAX_READ_DATA;

        uint8_t force[CP2112_REPORT_SIZE] = {0};
        force[0] = CP2112_DATA_READ_FORCE_SEND;
        force[1] = (uint8_t)(chunk >> 8);    /* __be16 high byte */
        force[2] = (uint8_t)(chunk & 0xff);  /* __be16 low byte  */
        if (hid_send(force, 3) < 0) return -1;

        uint8_t resp[CP2112_REPORT_SIZE] = {0};
        int r = hid_recv(resp, 100);
        if (r < 3 || resp[0] != CP2112_DATA_READ_RESPONSE) return -1;

        int valid = (int)resp[2];  /* byte[1]=status ignored; byte[2]=valid byte count */
        if (valid <= 0 || valid > CP2112_MAX_READ_DATA) return -1;
        if (valid > chunk) valid = chunk;
        memcpy(buf + received, &resp[3], valid);
        received += valid;
    }
    return received;
}

/*
 * I2C write-then-read via CP2112 (repeated start).
 * Writes write_data (sets register/address pointer), then reads read_len bytes.
 *
 * Report: [0x11][addr<<1][read_len_hi][read_len_lo][write_len][write_data...]
 *
 * Returns bytes read on success, -1 on error.
 */
static int cp2112_write_read(uint8_t addr,
                              const uint8_t *write_data, int write_len,
                              uint8_t *read_buf, int read_len)
{
    if (write_len < 1 || write_len > 16 || read_len < 1 || read_len > 512)
        return -1;

    uint8_t report[CP2112_REPORT_SIZE] = {0};
    report[0] = CP2112_DATA_WRITE_READ_REQUEST;
    report[1] = addr << 1;
    report[2] = (uint8_t)(read_len >> 8);
    report[3] = (uint8_t)(read_len & 0xff);
    report[4] = (uint8_t)write_len;
    memcpy(&report[5], write_data, write_len);
    if (hid_send(report, 5 + write_len) < 0) return -1;
    if (cp2112_wait_complete() < 0) return -1;
    return cp2112_collect(read_buf, read_len);
}

/* ── mux topology helpers ────────────────────────────────────────────────── */
/*
 * PCA9548 mux tree behind CP2112 (i2c-1):
 *   0x70 ch0-7 → buses  2-9   (QSFP ports)
 *   0x71 ch0-7 → buses 10-17  (QSFP ports)
 *   0x72 ch0-7 → buses 18-25  (QSFP ports)
 *   0x73 ch0-7 → buses 26-33  (QSFP ports)
 *   0x74 ch0-7 → buses 34-41  (ch2→36 PCA9535 0x22, ch3→37 PCA9535 0x23,
 *                               ch6→40 syseeprom 24c64 0x50)
 */
static int bus_to_mux_addr(int bus)
{
    if (bus >=  2 && bus <=  9) return 0x70;
    if (bus >= 10 && bus <= 17) return 0x71;
    if (bus >= 18 && bus <= 25) return 0x72;
    if (bus >= 26 && bus <= 33) return 0x73;
    if (bus >= 34 && bus <= 41) return 0x74;
    return -1;
}

static int bus_to_mux_channel(int bus)
{
    if (bus >=  2 && bus <=  9) return bus -  2;
    if (bus >= 10 && bus <= 17) return bus - 10;
    if (bus >= 18 && bus <= 25) return bus - 18;
    if (bus >= 26 && bus <= 33) return bus - 26;
    if (bus >= 34 && bus <= 41) return bus - 34;
    return -1;
}

/* Select a single PCA9548 channel (bitmask = 1 << channel). */
static int mux_select(int mux_addr, int channel)
{
    uint8_t mask = (uint8_t)(1 << channel);
    return cp2112_write((uint8_t)mux_addr, &mask, 1);
}

/* Deselect all channels on a mux. */
static int mux_deselect(int mux_addr)
{
    uint8_t off = 0x00;
    return cp2112_write((uint8_t)mux_addr, &off, 1);
}

/* ── i2c helpers (Phase 1 fallback) ─────────────────────────────────────── */

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

/* ── syseeprom — hidraw path (Phase 2) ──────────────────────────────────── */

/*
 * Read the 24c64 system EEPROM (mux 0x74 ch6, 0x50) via hidraw.
 *
 * 24c64 uses 2-byte (16-bit) addressing: write [addr_hi][addr_lo] then read.
 * CP2112 max per write-read transfer = 512 bytes; read in 512-byte chunks.
 * Validates ONIE TlvInfo magic before writing cache.
 */
static void poll_syseeprom_hidraw(void)
{
    char cache_path[64];
    struct stat st;

    snprintf(cache_path, sizeof(cache_path), RUN_DIR "/syseeprom");
    if (stat(cache_path, &st) == 0) return;  /* already cached — static data */

    if (mux_select(0x74, 6) < 0) {
        fprintf(stderr, "wedge100s-i2c-daemon: syseeprom: mux 0x74 ch6 select failed\n");
        return;
    }

    unsigned char buf[SYSEEPROM_SIZE] = {0};
    int total = 0;

    while (total < SYSEEPROM_SIZE) {
        int chunk = SYSEEPROM_SIZE - total;
        if (chunk > 512) chunk = 512;
        /* 24c64 2-byte address: high byte first */
        uint8_t addr_bytes[2] = {(uint8_t)(total >> 8), (uint8_t)(total & 0xff)};
        int r = cp2112_write_read(0x50, addr_bytes, 2, buf + total, chunk);
        if (r < 0) {
            fprintf(stderr,
                    "wedge100s-i2c-daemon: syseeprom: read at offset %d failed\n",
                    total);
            mux_deselect(0x74);
            return;
        }
        total += r;
    }

    mux_deselect(0x74);

    if (total < 8 || memcmp(buf, ONIE_MAGIC, 8) != 0) {
        fprintf(stderr,
                "wedge100s-i2c-daemon: syseeprom: invalid magic "
                "(got %02x %02x %02x %02x...) — not writing cache\n",
                buf[0], buf[1], buf[2], buf[3]);
        return;
    }

    if (write_binary_file(cache_path, buf, total) < 0)
        fprintf(stderr, "wedge100s-i2c-daemon: syseeprom: write failed: %s\n",
                strerror(errno));
}

/* ── syseeprom — sysfs path (Phase 1 fallback) ──────────────────────────── */

static void poll_syseeprom(void)
{
    char cache_path[64];
    char sysfs_path[64];
    struct stat st;
    unsigned char buf[SYSEEPROM_SIZE];
    FILE *f;
    int nread;

    snprintf(cache_path, sizeof(cache_path), RUN_DIR "/syseeprom");
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

/* ── poll_presence — hidraw path (Phase 2) ──────────────────────────────── */

/*
 * Read PCA9535 presence and QSFP EEPROMs via CP2112 hidraw.
 *
 * PCA9535 presence reads:
 *   mux 0x74 ch2 → PCA9535 0x22 (ports 0-15)
 *   mux 0x74 ch3 → PCA9535 0x23 (ports 16-31)
 *   Read INPUT0 (reg 0) and INPUT1 (reg 1) from each chip.
 *   Decode XOR-1 interleave and active-low polarity per ONL sfpi.c.
 *
 * QSFP EEPROM reads (on insertion or retry):
 *   Select port's mux channel, read 128 bytes lower page (addr 0x00)
 *   and 128 bytes upper page 0 (addr 0x80) from EEPROM at 0x50.
 *   QSFP28 EEPROMs use 1-byte addressing within a 256-byte memory map.
 *   Default page is 00h; no explicit page-select write is needed on insertion.
 *
 * Deselects each mux immediately after use.
 */
static void poll_presence_hidraw(void)
{
    int curr_present[NUM_PORTS] = {0};

    /* Read PCA9535 presence (mux 0x74 ch2 and ch3) */
    for (int g = 0; g < 2; g++) {
        if (mux_select(0x74, PCA9535_MUX_CHAN[g]) < 0) {
            fprintf(stderr,
                    "wedge100s-i2c-daemon: PCA9535[%d] mux select failed\n", g);
            continue;
        }
        for (int r = 0; r < 2; r++) {
            uint8_t reg_byte = (uint8_t)r;
            uint8_t val = 0;
            int ret = cp2112_write_read((uint8_t)PCA9535_ADDR[g],
                                        &reg_byte, 1, &val, 1);
            if (ret < 0) {
                fprintf(stderr,
                        "wedge100s-i2c-daemon: PCA9535[%d] reg %d read failed\n",
                        g, r);
                continue;
            }
            for (int bit = 0; bit < 8; bit++) {
                int line = r * 8 + bit;
                int p    = g * 16 + (line ^ 1);  /* XOR-1 interleave (ONL sfpi.c) */
                curr_present[p] = !((val >> bit) & 1);  /* active-low */
            }
        }
        mux_deselect(0x74);
    }

    /* Process each port: update presence file and conditionally read EEPROM */
    for (int port = 0; port < NUM_PORTS; port++) {
        char present_path[64];
        char eeprom_path[64];

        snprintf(present_path, sizeof(present_path),
                 RUN_DIR "/sfp_%d_present", port);
        snprintf(eeprom_path,  sizeof(eeprom_path),
                 RUN_DIR "/sfp_%d_eeprom",  port);

        if (!curr_present[port]) {
            unlink(eeprom_path);
            write_str_file(present_path, "0");
            continue;
        }

        /* Determine if EEPROM read is needed */
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
             * Stable only if the cached identifier is valid.
             * An invalid byte (0x00, 0x80-0xff) means the prior read caught
             * a corrupt or uninitialized EEPROM — discard the cache and
             * re-read every cycle until presence clears or a valid type lands.
             */
            int id_byte = -1;
            FILE *ef = fopen(eeprom_path, "rb");
            if (ef) { id_byte = fgetc(ef); fclose(ef); }
            if (EEPROM_ID_VALID(id_byte)) {
                write_str_file(present_path, "1");
                continue;
            }
            unlink(eeprom_path);
        }

        /*
         * Insertion event or retry after a failed EEPROM read.
         * Read 256 bytes from the QSFP EEPROM via hidraw:
         *   - Lower page (bytes 0-127):  write-read addr 0x00, read 128
         *   - Upper page 0 (bytes 128-255): write-read addr 0x80, read 128
         */
        int bus      = SFP_BUS_MAP[port];
        int mux_addr = bus_to_mux_addr(bus);
        int mux_chan  = bus_to_mux_channel(bus);

        if (mux_addr >= 0 && mux_chan >= 0 && mux_select(mux_addr, mux_chan) == 0) {
            unsigned char ebuf[EEPROM_SIZE] = {0};
            uint8_t lower_addr = 0x00;
            int r = cp2112_write_read(0x50, &lower_addr, 1, ebuf, 128);
            if (r == 128) {
                uint8_t upper_addr = 0x80;
                r = cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
                if (r == 128 && EEPROM_ID_VALID(ebuf[0]))
                    write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
                /* r < 128 or invalid id: do not cache; retry next tick */
            }
            /* r < 128 on lower page: module warming up; retry next tick */
            mux_deselect(mux_addr);
        }

        write_str_file(present_path, "1");
    }
}

/* ── poll_presence — sysfs/i2c-dev path (Phase 1 fallback) ─────────────── */

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
                continue;
            }
            for (int bit = 0; bit < 8; bit++) {
                int line = r * 8 + bit;
                int p    = g * 16 + (line ^ 1);
                curr_present[p] = !((byte >> bit) & 1);
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
            unlink(eeprom_path);
            write_str_file(present_path, "0");
            continue;
        }

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
            int id_byte = -1;
            FILE *ef = fopen(eeprom_path, "rb");
            if (ef) { id_byte = fgetc(ef); fclose(ef); }
            if (EEPROM_ID_VALID(id_byte)) {
                write_str_file(present_path, "1");
                continue;
            }
            unlink(eeprom_path);
        }

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
                if (nread == EEPROM_SIZE && EEPROM_ID_VALID(ebuf[0]))
                    write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
            }
        }

        write_str_file(present_path, "1");
    }
}

/* ── main ───────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    mkdir(RUN_DIR, 0755);

    if (argc < 2 || strcmp(argv[1], "poll-presence") != 0) {
        fprintf(stderr, "Usage: %s poll-presence\n", argv[0]);
        return 1;
    }

    /*
     * Phase 2: try to open /dev/hidraw0 for direct CP2112 access.
     * If successful, all mux-tree I2C goes through raw HID reports —
     * no kernel i2c_mux_pca954x or optoe1 involvement.
     *
     * Phase 1 fallback: hidraw0 unavailable (permission, missing device, or
     * hid_cp2112 has exclusive ownership). Use i2c-dev ioctl for PCA9535
     * and optoe1 sysfs for EEPROM.
     *
     * Note: when hid_cp2112 is loaded, both hid_cp2112 and our hidraw fd
     * receive all CP2112 interrupt reports.  CPLD accesses (address 0x32,
     * no mux) are safe to interleave because they do not change mux state.
     * For full isolation, Step P2-4 removes i2c_mux_pca954x and optoe1,
     * ensuring no kernel driver touches the mux tree concurrently.
     */
    g_hidraw_fd = open("/dev/hidraw0", O_RDWR);

    if (g_hidraw_fd >= 0) {
        cp2112_cancel();  /* drain any stale CP2112 state from prior users */
        poll_syseeprom_hidraw();
        poll_presence_hidraw();
        close(g_hidraw_fd);
        g_hidraw_fd = -1;
    } else {
        poll_syseeprom();
        poll_presence();
    }

    return 0;
}
