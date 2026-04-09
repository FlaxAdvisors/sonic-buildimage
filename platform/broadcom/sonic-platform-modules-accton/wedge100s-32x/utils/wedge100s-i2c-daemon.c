/**
 * @file wedge100s-i2c-daemon.c
 * @brief Event-driven QSFP presence and EEPROM cache daemon for Wedge 100S-32X.
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
#include <time.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <syslog.h>
#include <sys/inotify.h>
#include <sys/timerfd.h>

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

/*
 * Per-port LP_MODE deassert timestamp (CLOCK_MONOTONIC, nanoseconds).
 * Set to clock_gettime() when set_lpmode_hidraw(port, 0) succeeds.
 * refresh_eeprom_lower_page() refuses to read the upper page from
 * hardware until LP_MODE_READY_NS has elapsed, preventing the race where
 * an EEPROM read fires while the module MCU is still resetting.
 * Initialised to 0 (always-expired) so absent/legacy ports are unaffected.
 */
#define LP_MODE_READY_NS  2500000000LL   /* 2.5 s: SFF-8636 module MCU init */
static long long g_lp_deassert_ns[NUM_PORTS];  /* 0 = no recent deassert */

/**
 * @brief Return the current CLOCK_MONOTONIC time in nanoseconds.
 *
 * @return Nanoseconds since an arbitrary epoch (monotonic clock).
 */
static long long now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* ── hidraw transport ────────────────────────────────────────────────────── */

/**
 * @brief Send a HID output report to the CP2112, padded to 64 bytes.
 *
 * @param report Pointer to report bytes (report ID is report[0]).
 * @param len    Number of meaningful bytes in report.
 * @return 0 on success, -1 if the write did not transfer all 64 bytes.
 */
static int hid_send(const uint8_t *report, int len)
{
    uint8_t buf[CP2112_REPORT_SIZE] = {0};
    if (len > CP2112_REPORT_SIZE) len = CP2112_REPORT_SIZE;
    memcpy(buf, report, len);
    ssize_t r = write(g_hidraw_fd, buf, CP2112_REPORT_SIZE);
    return (r == CP2112_REPORT_SIZE) ? 0 : -1;
}

/**
 * @brief Receive one HID input report from the CP2112 with a timeout.
 *
 * @param buf        Buffer of at least CP2112_REPORT_SIZE (64) bytes.
 * @param timeout_ms Maximum milliseconds to wait for a report.
 * @return Number of bytes read on success, -1 on timeout or read error.
 */
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

/**
 * @brief Cancel any outstanding CP2112 transfer and drain pending input reports.
 *
 * Sends a CANCEL_TRANSFER report then reads up to 8 stale input reports
 * (5 ms timeout each) to clear the USB input buffer.
 */
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

/**
 * @brief Poll TRANSFER_STATUS_REQUEST until the CP2112 reports Complete or Error.
 *
 * The CP2112 executes one I2C transfer at a time. Polls up to 100 times with
 * 2 ms sleeps between Busy responses. Cancels and returns -1 on Error or
 * if 100 iterations expire without completion.
 *
 * Status byte[1] values: 0x00=Idle (OK for write-only), 0x01=Busy,
 * 0x02=Complete, 0x03=Error.
 *
 * @return 0 on Complete or Idle, -1 on Error or timeout.
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

/**
 * @brief Write up to 61 bytes to a 7-bit I2C device via the CP2112.
 *
 * Issues a DATA_WRITE_REQUEST report [0x14][addr<<1][len][data...] and
 * waits for transfer completion.
 *
 * @param addr 7-bit I2C device address.
 * @param data Bytes to write.
 * @param len  Number of bytes (1–61).
 * @return 0 on success, -1 on invalid length, send failure, or transfer error.
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

/**
 * @brief Collect read data from the CP2112 after a completed read or write-read.
 *
 * Issues DATA_READ_FORCE_SEND reports in up to 61-byte chunks until
 * read_len bytes are accumulated in buf.
 *
 * Force-send report format: [0x12][len_hi][len_lo] (__be16, high byte first).
 * Read-response format:     [0x13][status][valid_len][data...].
 *
 * @param buf      Output buffer; must be at least read_len bytes.
 * @param read_len Total number of bytes to collect.
 * @return Total bytes collected on success, -1 on send/receive error or
 *         unexpected report ID.
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

/**
 * @brief Perform an I2C write-then-read (repeated-start) via the CP2112.
 *
 * Sends a DATA_WRITE_READ_REQUEST report to set the register/address pointer,
 * waits for completion, then collects read_len bytes via cp2112_collect().
 *
 * Report format: [0x11][addr<<1][read_len_hi][read_len_lo][write_len][write_data...].
 *
 * @param addr       7-bit I2C device address.
 * @param write_data Bytes to write (typically a register offset).
 * @param write_len  Number of bytes to write (1–16).
 * @param read_buf   Output buffer for read data.
 * @param read_len   Number of bytes to read (1–512).
 * @return Number of bytes read on success, -1 on error.
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
/**
 * @brief Return the PCA9548 mux I2C address that owns a given virtual bus number.
 *
 * The five PCA9548 muxes fan out from CP2112 i2c-1:
 *   0x70 → buses 2–9, 0x71 → 10–17, 0x72 → 18–25, 0x73 → 26–33, 0x74 → 34–41.
 *
 * @param bus Virtual bus number (2–41).
 * @return 7-bit I2C address of the owning PCA9548, or -1 for out-of-range bus.
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

/**
 * @brief Return the PCA9548 channel number for a given virtual bus number.
 *
 * Each PCA9548 controls 8 downstream buses; the channel is (bus % 8) within
 * each mux's base address range.
 *
 * @param bus Virtual bus number (2–41).
 * @return Channel number (0–7), or -1 for out-of-range bus.
 */
static int bus_to_mux_channel(int bus)
{
    if (bus >=  2 && bus <=  9) return bus -  2;
    if (bus >= 10 && bus <= 17) return bus - 10;
    if (bus >= 18 && bus <= 25) return bus - 18;
    if (bus >= 26 && bus <= 33) return bus - 26;
    if (bus >= 34 && bus <= 41) return bus - 34;
    return -1;
}

/**
 * @brief Select a single channel on a PCA9548 mux via CP2112.
 *
 * Writes a one-byte bitmask (1 << channel) to the mux to enable exactly
 * that downstream bus and disable all others.
 *
 * @param mux_addr 7-bit I2C address of the PCA9548.
 * @param channel  Channel number to select (0–7).
 * @return 0 on success, -1 on I2C write failure.
 */
static int mux_select(int mux_addr, int channel)
{
    uint8_t mask = (uint8_t)(1 << channel);
    return cp2112_write((uint8_t)mux_addr, &mask, 1);
}

/**
 * @brief Deselect all channels on a PCA9548 mux (write 0x00).
 *
 * @param mux_addr 7-bit I2C address of the PCA9548.
 * @return 0 on success, -1 on I2C write failure.
 */
static int mux_deselect(int mux_addr)
{
    uint8_t off = 0x00;
    return cp2112_write((uint8_t)mux_addr, &off, 1);
}

/**
 * @brief Deselect all channels on all five PCA9548 muxes.
 *
 * Called at daemon startup and after a crash recovery. A prior crash may have
 * left a mux channel selected, mis-routing subsequent I2C addresses. Writing
 * 0x00 to each mux brings the bus tree to a known-good idle state.
 *
 * @return 0 if all five deselects succeed, -1 if any fail.
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

/* ── i2c helpers (Phase 1 fallback) ─────────────────────────────────────── */

/**
 * @brief Read a single byte from an I2C device via SMBus ioctl (Phase 1 fallback).
 *
 * Opens /dev/i2c-N, sets the slave address with I2C_SLAVE_FORCE, then issues
 * an I2C_SMBUS_BYTE_DATA read to write the register pointer and read one byte.
 *
 * @param bus  Linux I2C bus number (N in /dev/i2c-N).
 * @param addr 7-bit I2C device address.
 * @param reg  Register offset to read.
 * @return Byte value [0..255] on success, -1 on open/ioctl failure.
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

/**
 * @brief Write a single byte to an I2C device via SMBus ioctl (Phase 1 fallback).
 *
 * Opens /dev/i2c-N, sets the slave address with I2C_SLAVE_FORCE, then issues
 * an I2C_SMBUS_BYTE_DATA write.
 *
 * @param bus  Linux I2C bus number (N in /dev/i2c-N).
 * @param addr 7-bit I2C device address.
 * @param reg  Register offset to write.
 * @param val  Byte value to write.
 * @return 0 on success, -1 on open/ioctl failure.
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

/**
 * @brief Write a null-terminated string to a file, creating or truncating it.
 *
 * @param path Absolute path of the file.
 * @param str  String to write (not newline-terminated by this function).
 * @return 0 on success, -1 if fopen() failed.
 */
static int write_str_file(const char *path, const char *str)
{
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fputs(str, f);
    fclose(f);
    return 0;
}

/**
 * @brief Write binary data to a file, creating or truncating it.
 *
 * @param path Absolute path of the file.
 * @param buf  Data to write.
 * @param len  Number of bytes to write.
 * @return 0 on success, -1 if fopen() failed or fewer than len bytes were written.
 */
static int write_binary_file(const char *path, const unsigned char *buf, int len)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    int written = (int)fwrite(buf, 1, (size_t)len, f);
    fclose(f);
    return (written == len) ? 0 : -1;
}

/* ── EEPROM refresh helper ───────────────────────────────────────────────── */

/**
 * @brief Refresh the lower-page EEPROM cache for a QSFP port via CP2112 hidraw.
 *
 * Reads bytes 0–127 (lower page, live DOM monitoring data) from the module
 * EEPROM at 0x50 on the port's mux channel. If a cache file already exists,
 * the upper-page bytes (128–255, static vendor/type info) are preserved from
 * the cache to avoid an extra I2C round-trip. If no cache exists, the upper
 * page is read from hardware; however if the LP_MODE deassert lock has not
 * expired (LP_MODE_READY_NS = 2.5 s), the function returns 0 immediately so
 * the caller retries on the next tick.
 *
 * @param port        Port index (0–31).
 * @param eeprom_path Absolute path of the EEPROM cache file in /run/wedge100s/.
 * @return 1 if the cache file was written successfully, 0 on any I2C error,
 *         invalid identifier byte, or LP_MODE lock not yet expired.
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
            /* Upper page must be read from hardware — but only after the module
             * MCU has had time to initialise following LP_MODE deassert.
             * If the lock has not expired, skip the write entirely and return 0
             * so the caller retries on the next tick. */
            long long elapsed = now_ns() - g_lp_deassert_ns[port];
            if (g_lp_deassert_ns[port] != 0 && elapsed < LP_MODE_READY_NS) {
                mux_deselect(mux_addr);
                return 0;   /* not ready; caller will retry next tick */
            }
            uint8_t upper_addr = 0x80;
            int ur = cp2112_write_read(0x50, &upper_addr, 1, ebuf + 128, 128);
            if (ur != 128) {
                /* Upper page read failed; do not write a cache with zero upper page.
                 * Return 0 so caller retries next tick. */
                mux_deselect(mux_addr);
                return 0;
            }
        }
        write_binary_file(eeprom_path, ebuf, EEPROM_SIZE);
        ok = 1;
    }
    mux_deselect(mux_addr);
    return ok;
}

/**
 * @brief Apply LP_MODE state for one QSFP port via CP2112 hidraw.
 *
 * Controls the LP_MODE pin via PCA9535 on mux 0x74 ch0 (ports 0–15) or
 * ch1 (ports 16–31). Uses the ONL XOR-1 interleave: line = (port % 16) ^ 1.
 *
 * lpmode=0 (deassert, allow high power): drives the PCA9535 pin LOW as an
 * output and records the deassert timestamp in g_lp_deassert_ns[] to gate
 * upper-page EEPROM reads until the module MCU has initialised (2.5 s).
 *
 * lpmode=1 (assert, force low power): releases the pin to INPUT so the
 * pull-up drives it HIGH.
 *
 * Verifies the config register readback after each write.
 *
 * @param port   Port index (0–31).
 * @param lpmode 0 = deassert LP_MODE, 1 = assert LP_MODE.
 * @return 0 on success, -1 on I2C error or readback mismatch.
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
    /* Record deassert time so refresh_eeprom_lower_page() can gate
     * upper-page reads until the module MCU has had time to initialise. */
    if (!lpmode)
        g_lp_deassert_ns[port] = now_ns();
    return 0;
}

/**
 * @brief Write bytes to a QSFP EEPROM via the CP2112 mux tree.
 *
 * Selects the port's mux channel, sends [offset][data...] as a single
 * DATA_WRITE_REQUEST (max 60 data bytes due to the 1-byte offset prefix),
 * then deselects the mux.
 *
 * @param mux_addr I2C address of the PCA9548 mux.
 * @param mux_chan Channel to select on the mux.
 * @param offset   Byte offset within the EEPROM to begin writing.
 * @param data     Data bytes to write.
 * @param len      Number of bytes to write (1–60).
 * @return 0 on success, -1 on mux select failure or I2C write error.
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

/**
 * @brief Process pending sfp_N_write_req files and write EEPROM bytes via hidraw.
 *
 * For each port, checks for a sfp_N_write_req JSON file containing offset,
 * length, and data_hex fields. Parses the hex payload, writes the bytes to
 * the QSFP EEPROM via cp2112_write_eeprom(), refreshes the EEPROM lower-page
 * cache, and writes an ack file (sfp_N_write_ack = "ok" or "err:...").
 * The request file is unlinked after processing.
 */
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

/**
 * @brief Process pending sfp_N_read_req files and return lower-page data via hidraw.
 *
 * For each port, checks for a sfp_N_read_req JSON file. Only offset=0,
 * length=128 (lower-page DOM read) is supported. Performs a cp2112_write_read()
 * to read 128 bytes from EEPROM 0x50, encodes the result as a 256-char hex
 * string, and writes it to sfp_N_read_resp. The request file is unlinked.
 */
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

/**
 * @brief Process LP_MODE state changes for all ports via CP2112 hidraw.
 *
 * Runs two passes in order:
 *
 * 1. Request files: for each sfp_N_lpmode_req file, reads the desired state
 *    ("0" = deassert, "1" = assert), calls set_lpmode_hidraw(), updates
 *    sfp_N_lpmode, and removes the request file.
 *
 * 2. Initial deassert: for each present port whose sfp_N_lpmode state file
 *    does not yet exist, drives LP_MODE LOW and writes sfp_N_lpmode="0",
 *    overriding the hardware power-on default of all-asserted (pull-up HIGH).
 *    EEPROM is not read here; the module MCU needs 2.5 s after LP_MODE exit.
 *
 * Presence state is read from sfp_N_present files written earlier in the
 * same tick by poll_presence_hidraw().
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
             * Do NOT read EEPROM here.  The QSFP module's MCU resets during
             * LP_MODE exit: attempting cp2112_write_read(0x50,...) immediately
             * after deassert hangs the I2C bus, leaving the CP2112 in a
             * permanently stuck BUSY state that survives cp2112_cancel().
             *
             * poll_presence_hidraw() already ran earlier this tick while
             * modules were in LP_MODE and wrote a valid cache (identifier byte
             * is stable between LP_MODE and full-power).  xcvrd will use that
             * cache to determine module type; DOM data is read on-demand via
             * sfp_N_read_req, not from the cache.
             */
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

/**
 * @brief Apply LP_MODE state for one port via i2c-dev SMBus ioctl (Phase 1 fallback).
 *
 * Uses the kernel I2C bus infrastructure (requires i2c_mux_pca954x loaded).
 * LP_MODE PCA9535 chips are at addresses 0x20/0x21 on buses 34/35.
 * Uses the same ONL XOR-1 interleave as set_lpmode_hidraw().
 *
 * @param port   Port index (0–31).
 * @param lpmode 0 = deassert LP_MODE, 1 = assert LP_MODE.
 * @return 0 on success, -1 on I2C read or write failure.
 */
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

/**
 * @brief Process LP_MODE state changes for all ports via i2c-dev SMBus (Phase 1 fallback).
 *
 * Same two-pass logic as poll_lpmode_hidraw() but using set_lpmode_sysfs()
 * instead of set_lpmode_hidraw(). Used when /dev/hidraw0 is unavailable.
 */
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

/**
 * @brief Mirror read-only wedge100s_cpld sysfs attributes to /run/wedge100s/.
 *
 * Provides a single canonical read path for Python consumers (psu.py,
 * component.py, chassis.py) so they never touch the kernel I2C sysfs tree
 * directly.
 *
 * Static attrs (cpld_version, board_rev, model_id, come_status) are hardware
 * constants — read once at first tick and never again.
 *
 * Dynamic attrs (PSU state, power rails, ROV, reset reason/sources) are read
 * on every tick.
 *
 * LED attributes (led_sys1, led_sys2) are NOT mirrored here; they are
 * managed exclusively by apply_led_writes().
 */
static void poll_cpld(void)
{
    /* Static attrs: hardware constants — read once, never again. */
    static const char *static_attrs[] = {
        "cpld_version", "board_rev", "model_id", "come_status",
        NULL
    };
    for (int i = 0; static_attrs[i]; i++) {
        char dst[128];
        struct stat st;
        snprintf(dst, sizeof(dst), RUN_DIR "/%s", static_attrs[i]);
        if (stat(dst, &st) != 0) {
            char src[128], val[64];
            snprintf(src, sizeof(src), CPLD_SYSFS "/%s", static_attrs[i]);
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

    /* Dynamic attrs: can change at runtime — read every tick. */
    static const char *dynamic_attrs[] = {
        "psu1_present", "psu1_pgood", "psu1_alarm", "psu1_input_ok",
        "psu2_present", "psu2_pgood", "psu2_alarm", "psu2_input_ok",
        "pwr_stby_ok",  "pwr_status2", "rov_status",
        "reset_reason", "reset_source1", "reset_source2",
        NULL
    };
    for (int i = 0; dynamic_attrs[i]; i++) {
        char src[128], dst[128], val[64];
        snprintf(src, sizeof(src), CPLD_SYSFS "/%s", dynamic_attrs[i]);
        snprintf(dst, sizeof(dst), RUN_DIR   "/%s", dynamic_attrs[i]);
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

/* ── apply_led_writes — write-through LED state to CPLD ─────────────────── */

/**
 * @brief Synchronise /run/wedge100s/led_sys{1,2} with wedge100s_cpld sysfs.
 *
 * Provides write-through LED control: chassis.py writes the desired color
 * string to RUN_DIR; this function pushes it to the CPLD sysfs attribute
 * on every poll tick so no Python code touches CPLD sysfs directly.
 *
 * Seed path (file absent on first tick): reads the current hardware value
 * from CPLD sysfs and seeds the RUN_DIR file so get_status_led() returns
 * the correct state before the first set_status_led() call.
 *
 * Write-through path (file present): reads the value chassis.py wrote and
 * writes it to CPLD sysfs. Idempotent — repeated writes of the same value
 * are harmless.
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

/**
 * @brief Read the 24c64 system EEPROM via CP2112 hidraw and cache it.
 *
 * Reads up to SYSEEPROM_SIZE (8192) bytes from the 24c64 at mux 0x74 ch6,
 * address 0x50, using 2-byte (16-bit) addressing in 512-byte chunks.
 * Validates the ONIE TlvInfo magic at offset 0 before writing the cache
 * to /run/wedge100s/syseeprom. Skips silently if the cache file already
 * exists (system EEPROM is static data; written once per boot).
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

/**
 * @brief Read the system EEPROM via at24 sysfs and cache it (Phase 1 fallback).
 *
 * Reads from /sys/bus/i2c/devices/40-0050/eeprom (requires at24 driver).
 * Validates ONIE TlvInfo magic and writes the cache to /run/wedge100s/syseeprom.
 * Skips if the cache file already exists.
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

/**
 * @brief Poll QSFP presence and refresh EEPROM caches via CP2112 hidraw.
 *
 * Reads INPUT0 and INPUT1 from PCA9535 0x22 (mux 0x74 ch2, ports 0–15) and
 * 0x23 (ch3, ports 16–31). Decodes active-low polarity with the ONL XOR-1
 * interleave (line ^ 1) to build a per-port presence array.
 *
 * For each port:
 *   - absent: unlinks sfp_N_eeprom, writes sfp_N_present="0".
 *   - present with valid cache (id byte in [0x01, 0x7f]): writes
 *     sfp_N_present="1", skips EEPROM I2C (stable path).
 *   - present with missing/invalid cache: calls refresh_eeprom_lower_page()
 *     to read lower + upper EEPROM pages, writes sfp_N_present="1".
 *
 * Mux channels are deselected immediately after each chip read.
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

/**
 * @brief Poll QSFP presence and refresh EEPROM caches via i2c-dev SMBus (Phase 1 fallback).
 *
 * Reads PCA9535 INPUT registers via i2c_read_byte_data() to build presence
 * bitmaps. Updates sfp_N_present files for all 32 ports.
 *
 * On insertion or first-boot with a module present: reads the 256-byte EEPROM
 * page 0 from the optoe1 sysfs node (/sys/bus/i2c/devices/N-0050/eeprom) and
 * writes the cache to /run/wedge100s/sfp_N_eeprom.
 *
 * On removal: unlinks sfp_N_eeprom immediately to prevent stale data from
 * being served to xcvrd.
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

/* ── persistent daemon helpers ──────────────────────────────────────────── */

/**
 * @brief Drain all pending inotify events from inotify_fd.
 *
 * Reads and discards events until the fd returns EAGAIN. Prevents
 * thundering-herd processing on burst writes to /run/wedge100s/.
 *
 * @param inotify_fd File descriptor returned by inotify_init1().
 */
static void drain_inotify(int inotify_fd)
{
    char ibuf[sizeof(struct inotify_event) + NAME_MAX + 1];
    while (read(inotify_fd, ibuf, sizeof(ibuf)) > 0)
        ;
}

/**
 * @brief Service all pending write/read/lpmode request files in /run/wedge100s/.
 *
 * Called on inotify IN_CLOSE_WRITE events (~50 ms latency). Scans the
 * directory for all pending request files rather than replaying inotify
 * filenames, because inotify coalesces duplicate events.
 *
 * Also drains stale CP2112 HID input reports before any hidraw operation to
 * prevent cp2112_wait_complete() from receiving a stale STATUS_ERROR from a
 * prior CPLD sysfs access.
 *
 * No-op if g_hidraw_fd < 0 (Phase 1 fallback active).
 */
static void service_write_requests(void)
{
    if (g_hidraw_fd < 0) return;
    /*
     * Drain stale HID input reports before any hidraw operation.
     * apply_led_writes() (at the end of this function and of each timer tick)
     * accesses CPLD sysfs via the kernel hid-cp2112 driver, which leaves 1-2
     * STATUS_RESPONSE reports in the USB input buffer.  Without draining them,
     * the next call's cp2112_wait_complete() receives a stale STATUS_ERROR or
     * STATUS_COMPLETE from the CPLD access instead of the response to its own
     * DATA_WRITE_REQUEST, causing spurious PCA9535/LP_MODE failures.
     */
    cp2112_cancel();
    poll_lpmode_hidraw();
    poll_write_requests_hidraw();
    poll_read_requests_hidraw();
    apply_led_writes();   /* respond to led_sys{1,2} writes ~50ms */
}

/**
 * @brief Initialize the daemon: open hidraw0, cancel stale transfers, deselect muxes.
 *
 * On crash recovery (systemd Restart=on-failure), a prior run may have left
 * the CP2112 in mid-transaction and a PCA9548 mux channel selected. This
 * function restores the bus to a known-good state.
 *
 * Tries up to two attempts. On the second attempt, escalates via SSH to the
 * BMC to run cp2112_i2c_flush.sh and reset_qsfp_mux.sh before retrying.
 *
 * On success, removes all stale sfp_N_lpmode state files so that
 * poll_lpmode_hidraw() re-deasserts LP_MODE for all ports on the first tick.
 * EEPROM cache files are intentionally preserved.
 *
 * @return 0 on success (hidraw0 open, all muxes deselected, LP_MODE deasserted
 *         for all ports), -1 if both attempts fail.
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
            /* Re-provision BMC SSH key via /dev/ttyACM0 before trying SSH.
             * The BMC clears authorized_keys on every reboot; without this
             * the SSH commands below fail silently and escalation does nothing. */
            (void)system("/usr/bin/wedge100s-bmc-auth >/dev/null 2>&1");
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
            /* Remove stale lpmode state files so that poll_lpmode_hidraw()
             * re-deasserts LP_MODE for all ports on the first tick.
             * EEPROM cache files are intentionally kept: modules need up to
             * 2 s after LP_MODE exit before their MCU is ready for reads;
             * poll_presence_hidraw() serves the cached data meanwhile, and
             * the 20 s DOM TTL timer triggers a fresh lower-page read once
             * the module is fully initialised. */
            {
                char path[80];
                for (int p = 0; p < NUM_PORTS; p++) {
                    snprintf(path, sizeof(path), RUN_DIR "/sfp_%d_lpmode", p);
                    unlink(path);
                }
            }
            /* Deassert LP_MODE for all ports so modules power up fully
             * before the first tick reads EEPROM. */
            {
                int failed = 0;
                for (int p = 0; p < NUM_PORTS; p++) {
                    if (set_lpmode_hidraw(p, 0) < 0)
                        failed++;
                }
                if (failed)
                    syslog(LOG_WARNING,
                           "wedge100s-i2c-daemon: daemon_init: %d port(s) failed LP_MODE deassert",
                           failed);
            }
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

/* ── main ───────────────────────────────────────────────────────────────── */

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
