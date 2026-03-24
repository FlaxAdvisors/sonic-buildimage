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

/* PCA9535 LP_MODE chips (mux 0x74 ch0/ch1) */
static const int LP_PCA9535_ADDR[2] = { 0x20, 0x21 };
static const int LP_PCA9535_CHAN[2] = { 0,    1    };

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

/* ── CPLD sysfs path (wedge100s_cpld driver) ────────────────────────────── */

#define CPLD_SYSFS "/sys/bus/i2c/devices/1-0032"

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

/* ── EEPROM refresh helper ───────────────────────────────────────────────── */

/*
 * Refresh the lower page (bytes 0–127, live DOM data) of the EEPROM cache
 * for port via CP2112 hidraw.
 *
 * Lower page contains live DOM monitoring registers (temperature, voltage,
 * Tx/Rx power, bias current) that change continuously and must be re-read
 * on every daemon tick so xcvrd sees fresh values.
 *
 * Upper page 00h (bytes 128–255) contains static vendor/type info.  If the
 * cache file already exists its upper-page bytes are preserved; otherwise the
 * upper page is also read from hardware (first insertion or cache absent).
 *
 * Returns 1 if the cache was written, 0 on any I2C error or invalid id byte.
 */
static int refresh_eeprom_lower_page(int port, const char *eeprom_path)
{
    int bus      = SFP_BUS_MAP[port];
    int mux_addr = bus_to_mux_addr(bus);
    int mux_chan  = bus_to_mux_channel(bus);
    int ok = 0;

    if (mux_addr < 0 || mux_chan < 0) return 0;
    if (mux_select(mux_addr, mux_chan) < 0) return 0;

    unsigned char ebuf[EEPROM_SIZE] = {0};
    uint8_t lower_addr = 0x00;
    int r = cp2112_write_read(0x50, &lower_addr, 1, ebuf, 128);
    if (r == 128 && EEPROM_ID_VALID(ebuf[0])) {
        /* Upper page: read from existing cache (saves one I2C round-trip),
         * or from hardware if the cache is absent (first insertion). */
        FILE *cf = fopen(eeprom_path, "rb");
        if (cf) {
            if (fseek(cf, 128, SEEK_SET) == 0)
                (void)fread(ebuf + 128, 1, 128, cf);
            fclose(cf);
        } else {
            uint8_t upper_addr = 0x80;
            cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
        }
        write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
        ok = 1;
    }
    mux_deselect(mux_addr);
    return ok;
}

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
 * Returns 0 on success, -1 on error (including readback mismatch).
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
        /* Verify config register was written correctly */
        uint8_t verify_cfg = 0;
        if (cp2112_write_read((uint8_t)chip, &cfg_reg, 1, &verify_cfg, 1) < 0) {
            mux_deselect(0x74); return -1;
        }
        if (!(verify_cfg & (1u << bit))) {
            /* Bit not set as INPUT — write did not take effect */
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
        /* Verify config register was written correctly */
        uint8_t verify_cfg = 0;
        if (cp2112_write_read((uint8_t)chip, &cfg_reg, 1, &verify_cfg, 1) < 0) {
            mux_deselect(0x74); return -1;
        }
        if (verify_cfg & (1u << bit)) {
            /* Bit still set as INPUT — write did not take effect */
            mux_deselect(0x74); return -1;
        }
    }

    mux_deselect(0x74);
    return 0;
}

/*
 * cp2112_write_eeprom — write bytes to QSFP EEPROM via the mux tree.
 *
 * The CP2112 DATA_WRITE_REQUEST supports up to 61 bytes total.
 * reg_buf[0] = offset (1 byte), so max data bytes per call = 60.
 * Callers must split writes larger than 60 bytes.
 *
 * Returns 0 on success, -1 on error.
 */
static int cp2112_write_eeprom(int mux_addr, int mux_chan, int offset,
                                const uint8_t *data, int len)
{
    if (len < 1 || len > 60) return -1;

    uint8_t reg_buf[61];
    reg_buf[0] = (uint8_t)offset;
    memcpy(reg_buf + 1, data, len);

    if (mux_select(mux_addr, mux_chan) < 0) return -1;
    int rc = cp2112_write(0x50, reg_buf, len + 1);
    mux_deselect(mux_addr);
    return (rc < 0) ? -1 : 0;
}

/* ── poll_write_requests_hidraw — process pending sfp_N_write_req files ─── */

static void poll_write_requests_hidraw(void)
{
    char req_path[128], ack_path[128], eeprom_path[128];
    char read_buf[4096];
    FILE *fp;

    for (int port = 0; port < NUM_PORTS; port++) {
        snprintf(req_path,    sizeof(req_path),    RUN_DIR "/sfp_%d_write_req",  port);
        snprintf(ack_path,    sizeof(ack_path),    RUN_DIR "/sfp_%d_write_ack",  port);
        snprintf(eeprom_path, sizeof(eeprom_path), RUN_DIR "/sfp_%d_eeprom",     port);

        fp = fopen(req_path, "r");
        if (!fp) continue;

        /* Read JSON payload */
        size_t n = fread(read_buf, 1, sizeof(read_buf) - 1, fp);
        fclose(fp);
        read_buf[n] = '\0';

        /* Minimal JSON parse: extract offset, length, data_hex */
        int offset = -1, length = -1;
        char data_hex[512] = {0};
        {
            char *p;
            p = strstr(read_buf, "\"offset\"");
            if (p) sscanf(p + 8, " : %d", &offset);
            p = strstr(read_buf, "\"length\"");
            if (p) sscanf(p + 8, " : %d", &length);
            p = strstr(read_buf, "\"data_hex\"");
            if (p) sscanf(p + 10, " : \"%511[^\"]\"", data_hex);
        }

        if (offset < 0 || length <= 0 || length > 60 || data_hex[0] == '\0') {
            write_str_file(ack_path, "err:bad_request");
            unlink(req_path);
            continue;
        }

        /* Convert hex string to bytes */
        uint8_t write_buf[60];
        int hex_len = (int)strlen(data_hex);
        if (hex_len != length * 2) {
            write_str_file(ack_path, "err:hex_length_mismatch");
            unlink(req_path);
            continue;
        }
        for (int i = 0; i < length; i++) {
            unsigned int byte_val = 0;
            sscanf(data_hex + i * 2, "%02x", &byte_val);
            write_buf[i] = (uint8_t)byte_val;
        }

        /* Perform I2C write via hidraw */
        int bus = SFP_BUS_MAP[port];
        int mux_addr = bus_to_mux_addr(bus);
        int mux_chan  = bus_to_mux_channel(bus);
        int rc = cp2112_write_eeprom(mux_addr, mux_chan, offset, write_buf, length);

        if (rc < 0) {
            write_str_file(ack_path, "err:i2c_write_failed");
        } else {
            /* Refresh EEPROM cache */
            refresh_eeprom_lower_page(port, eeprom_path);
            write_str_file(ack_path, "ok");
        }
        unlink(req_path);
    }
}

/* ── poll_read_requests_hidraw — process pending sfp_N_read_req files ───── */

static void poll_read_requests_hidraw(void)
{
    char req_path[128], resp_path[128];
    char read_buf[256];
    FILE *fp;

    for (int port = 0; port < NUM_PORTS; port++) {
        snprintf(req_path,  sizeof(req_path),  RUN_DIR "/sfp_%d_read_req",  port);
        snprintf(resp_path, sizeof(resp_path), RUN_DIR "/sfp_%d_read_resp", port);

        fp = fopen(req_path, "r");
        if (!fp) continue;

        size_t n = fread(read_buf, 1, sizeof(read_buf) - 1, fp);
        fclose(fp);
        read_buf[n] = '\0';

        /* We only support offset=0, length=128 (lower page DOM read) */
        int offset = -1, length = -1;
        {
            char *p;
            p = strstr(read_buf, "\"offset\"");
            if (p) sscanf(p + 8, " : %d", &offset);
            p = strstr(read_buf, "\"length\"");
            if (p) sscanf(p + 8, " : %d", &length);
        }

        if (offset != 0 || length != 128) {
            write_str_file(resp_path, "err:unsupported_range");
            unlink(req_path);
            continue;
        }

        int bus      = SFP_BUS_MAP[port];
        int mux_addr = bus_to_mux_addr(bus);
        int mux_chan  = bus_to_mux_channel(bus);

        if (mux_addr < 0 || mux_chan < 0) {
            write_str_file(resp_path, "err:bad_bus");
            unlink(req_path);
            continue;
        }

        if (mux_select(mux_addr, mux_chan) < 0) {
            write_str_file(resp_path, "err:mux_select");
            unlink(req_path);
            continue;
        }

        unsigned char ebuf[128] = {0};
        uint8_t lower_addr = 0x00;
        int r = cp2112_write_read(0x50, &lower_addr, 1, ebuf, 128);
        mux_deselect(mux_addr);

        if (r != 128) {
            write_str_file(resp_path, "err:i2c_read_failed");
            unlink(req_path);
            continue;
        }

        /* Encode as hex string */
        char hex_resp[257];
        for (int i = 0; i < 128; i++)
            snprintf(hex_resp + i * 2, 3, "%02x", ebuf[i]);
        hex_resp[256] = '\0';
        write_str_file(resp_path, hex_resp);
        unlink(req_path);
    }
}

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
        if (set_lpmode_hidraw(port, 0) == 0) {
            write_str_file(state_path, "0");
            /*
             * Immediately refresh the lower page now that LP_MODE is deasserted.
             * poll_presence_hidraw() ran earlier this tick and wrote a LP-mode
             * snapshot (DOM bytes = 0 → -inf dBm).  Re-reading here in the same
             * invocation overwrites it with post-deassert values so xcvrd never
             * sees the stale snapshot.  On failure the stale cache is deleted so
             * xcvrd gets a cache-miss (None) rather than wrong data; the next
             * tick's poll_presence_hidraw() will retry the lower-page read.
             */
            char eeprom_path[80];
            snprintf(eeprom_path, sizeof(eeprom_path), RUN_DIR "/sfp_%d_eeprom", port);
            if (!refresh_eeprom_lower_page(port, eeprom_path))
                unlink(eeprom_path);
        } else {
            fprintf(stderr,
                    "wedge100s-i2c-daemon: initial deassert port %d failed\n",
                    port);
        }
    }
}

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

/* ── poll_cpld — mirror read-only CPLD sysfs attrs to /run/wedge100s/ ────── */

/*
 * Copy read-only wedge100s_cpld sysfs attributes to RUN_DIR so that Python
 * consumers (psu.py, component.py) have a single canonical read path that
 * does not touch the kernel i2c sysfs tree directly.
 *
 * LED attributes (led_sys1, led_sys2) are NOT mirrored here; they are
 * managed exclusively by apply_led_writes() below.
 */
static void poll_cpld(void)
{
    static const char *attrs[] = {
        "cpld_version",
        "psu1_present", "psu1_pgood",
        "psu2_present", "psu2_pgood",
        NULL
    };

    for (int i = 0; attrs[i]; i++) {
        char src[128], dst[128], val[64];
        snprintf(src, sizeof(src), CPLD_SYSFS "/%s", attrs[i]);
        snprintf(dst, sizeof(dst), RUN_DIR   "/%s", attrs[i]);

        FILE *f = fopen(src, "r");
        if (!f) continue;

        if (fgets(val, (int)sizeof(val), f)) {
            /* strip trailing whitespace (newline from sysfs) */
            int n = (int)strlen(val);
            while (n > 0 && (val[n-1] == '\n' || val[n-1] == '\r' ||
                             val[n-1] == ' '))
                val[--n] = '\0';
            write_str_file(dst, val);
        }
        fclose(f);
    }
}

/* ── apply_led_writes — write-through LED state to CPLD ─────────────────── */

/*
 * Manages /run/wedge100s/led_sys{1,2} as the sole path for LED state.
 * chassis.py writes the desired value to RUN_DIR; this function pushes
 * it through to the wedge100s_cpld sysfs attribute (and hence the CPLD
 * hardware) on every poll tick — no Python code touches CPLD sysfs.
 *
 * Seed path (file absent): on the first tick after boot, the run-dir file
 * does not yet exist.  Read the hardware's current value from CPLD sysfs
 * and write it to RUN_DIR so get_status_led() returns the correct state
 * before the first set_status_led() call.
 *
 * Write-through path (file present): read the value chassis.py wrote to
 * RUN_DIR and write it to CPLD sysfs.  Idempotent — writing the same
 * value repeatedly is harmless.
 */
static void apply_led_writes(void)
{
    static const char *leds[] = { "led_sys1", "led_sys2", NULL };

    for (int i = 0; leds[i]; i++) {
        char run_path[128], cpld_path[128], val[64];
        snprintf(run_path,  sizeof(run_path),  RUN_DIR    "/%s", leds[i]);
        snprintf(cpld_path, sizeof(cpld_path), CPLD_SYSFS "/%s", leds[i]);

        FILE *rf = fopen(run_path, "r");
        if (!rf) {
            /* Seed: /run file absent — copy hardware state into RUN_DIR. */
            FILE *cf = fopen(cpld_path, "r");
            if (!cf) continue;
            if (fgets(val, (int)sizeof(val), cf)) {
                int n = (int)strlen(val);
                while (n > 0 && (val[n-1] == '\n' || val[n-1] == '\r' ||
                                 val[n-1] == ' '))
                    val[--n] = '\0';
                write_str_file(run_path, val);
            }
            fclose(cf);
            continue;
        }

        /* Write-through: push RUN_DIR value to CPLD sysfs. */
        if (!fgets(val, (int)sizeof(val), rf)) { fclose(rf); continue; }
        fclose(rf);

        int n = (int)strlen(val);
        while (n > 0 && (val[n-1] == '\n' || val[n-1] == '\r' ||
                         val[n-1] == ' '))
            val[--n] = '\0';

        FILE *cf = fopen(cpld_path, "w");
        if (!cf) continue;
        fprintf(cf, "%s", val);
        fclose(cf);
    }
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

    /* Process each port: update presence file and refresh EEPROM lower page */
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

        /*
         * Stable port: if the cache exists and has a valid SFF identifier byte,
         * skip the EEPROM I2C round-trip entirely.  DOM data (lower page) is
         * refreshed on-demand by sfp.py via smbus2 when xcvrd asks for it,
         * so there is no need to hammer the CP2112 every tick.
         *
         * New insertion or invalid/absent cache: read lower + upper pages from
         * hardware now (this is the one time we need to do it — upper page is
         * static vendor info; lower page is the initial snapshot).
         */
        {
            int id_byte = -1;
            FILE *ef = fopen(eeprom_path, "rb");
            if (ef) { id_byte = fgetc(ef); fclose(ef); }
            if (EEPROM_ID_VALID(id_byte)) {
                write_str_file(present_path, "1");
                continue;
            }
        }

        refresh_eeprom_lower_page(port, eeprom_path);

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
        poll_lpmode_hidraw();
        poll_write_requests_hidraw();
        poll_read_requests_hidraw();
        close(g_hidraw_fd);
        g_hidraw_fd = -1;
    } else {
        poll_syseeprom();
        poll_presence();
        poll_lpmode_sysfs();
    }

    /*
     * LED write-through: apply_led_writes() runs first.  chassis.py writes
     * the desired LED value to RUN_DIR; apply_led_writes() pushes it to the
     * wedge100s_cpld sysfs attribute.  On the very first tick, when the RUN_DIR
     * file does not yet exist, it seeds RUN_DIR from the hardware state instead.
     *
     * CPLD read-only mirror: poll_cpld() then mirrors cpld_version and
     * psuN_present/pgood to RUN_DIR so Python consumers never read sysfs.
     *
     * Both must run AFTER the hidraw block: each CPLD sysfs access causes
     * hid_cp2112 to conduct an I2C transaction, leaving two stale HID input
     * reports (0x16 TRANSFER_STATUS_RESPONSE + 0x13 DATA_READ_RESPONSE) in
     * the hidraw buffer per attribute.  cp2112_cancel() only drains 8 —
     * leaving stale reports that cp2112_wait_complete() would consume as
     * false completions for subsequent mux writes, corrupting PCA9535 reads.
     */
    apply_led_writes();
    poll_cpld();

    return 0;
}
