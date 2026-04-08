/**
 * @file wedge100s-bmc-auth.c
 * @brief Push SSH public key to OpenBMC via the /dev/ttyACM0 serial console.
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

/**
 * @brief Open /dev/ttyACM0 and configure it for 57600 8N1 raw mode.
 *
 * Sets the global g_tty_fd on success. Configures the port with cfmakeraw()
 * and 57600 baud. Non-blocking I/O is enabled; VMIN=0/VTIME=0.
 *
 * @return 0 on success, -1 on open or tcgetattr/tcsetattr failure.
 */
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

/**
 * @brief Read from the TTY until a needle string is found or a timeout elapses.
 *
 * Polls g_tty_fd in 200 ms increments. Accumulates received bytes in buf,
 * rolling the tail window to avoid missing needle strings that span two reads.
 *
 * @param needle      String to search for in received data.
 * @param timeout_sec Maximum seconds to wait before giving up.
 * @param buf         Caller-supplied buffer for accumulated TTY output.
 * @param bufsz       Size of buf in bytes.
 * @return 1 if needle was found within the timeout, 0 on timeout.
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

/**
 * @brief Write a string to the BMC TTY without waiting for acknowledgement.
 *
 * @param s Null-terminated string to transmit.
 */
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
