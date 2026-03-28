/* bcmcmd_client.c — BCM diag shell Unix socket client.
 * Protocol (verified 2026-03-28):
 *   1. connect to /var/run/sswsyncd/sswsyncd.socket
 *   2. read until "drivshell>" prompt
 *   3. write "\n", read until "drivshell>"  (flush any pending output)
 *   4. write "ps\n", read until "drivshell>" → parse port table
 *   5. write "show counters\n", read until "drivshell>" → parse counters
 *
 * 'ps' output line format (one port per line):
 *   "       port_name( sdk_port)  link_state ..."
 *   e.g. "      xe86(118)  up     1   25G  FD   SW ..."
 *        "       ce0(  1)  up     4  100G  FD   SW ..."
 *
 * 'show counters' output line format (only non-zero entries printed):
 *   "COUNTER.port_name\t\t:\t\tvalue[,comma_sep]\t[+delta]"
 *   e.g. "RPKT.ce0\t\t:\t\t      3,255\t\t +3,255"
 *        "RBYT.xe86\t\t:\t\t    398,300\t     +389,116"
 */
#include "shim.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <syslog.h>

#define PROMPT        "drivshell>"
#define PROMPT_LEN    10
#define READ_BUF_SIZE 65536
#define SEND_TIMEOUT_MS 2000
#define RECV_TIMEOUT_MS 3000

/* ---- internal helpers ---- */

/* Accumulate socket reads into buf[0..n-1] until "drivshell>" appears or
 * timeout_ms elapses.  Returns total bytes read (NUL terminated), or -1. */
static int read_until_prompt(int fd, char *buf, int bufsz, int timeout_ms)
{
    int  total = 0;
    struct pollfd pfd = { .fd = fd, .events = POLLIN };

    while (total < bufsz - 1) {
        int rc = poll(&pfd, 1, timeout_ms);
        if (rc == 0) { errno = ETIMEDOUT; return -1; }
        if (rc < 0)  return -1;
        int n = (int)read(fd, buf + total, (size_t)(bufsz - 1 - total));
        if (n <= 0)  return -1;
        total += n;
        buf[total] = '\0';
        if (strstr(buf, PROMPT))
            return total;
    }
    errno = ENOBUFS;
    return -1;
}

/* Write all bytes; return 0 on success, -1 on error. */
static int write_all(int fd, const char *s)
{
    size_t len = strlen(s);
    while (len > 0) {
        ssize_t n = write(fd, s, len);
        if (n <= 0) return -1;
        s   += n;
        len -= (size_t)n;
    }
    return 0;
}

/* ---- public API ---- */

int bcmcmd_connect(const char *path, int timeout_ms)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    /* Set non-blocking for connect timeout. */
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    int rc = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    if (rc < 0 && errno != EINPROGRESS) { close(fd); return -1; }

    if (errno == EINPROGRESS) {
        struct pollfd pfd = { .fd = fd, .events = POLLOUT };
        if (poll(&pfd, 1, timeout_ms) <= 0) { close(fd); return -1; }
        int err = 0;
        socklen_t elen = sizeof(err);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen);
        if (err) { close(fd); errno = err; return -1; }
    }

    /* Restore blocking. */
    fcntl(fd, F_SETFL, flags);

    /* Read and discard the initial banner/prompt. */
    char buf[READ_BUF_SIZE];
    if (read_until_prompt(fd, buf, sizeof(buf), 2000) < 0) {
        close(fd); return -1;
    }
    /* Flush any pending output with a bare newline. */
    write_all(fd, "\n");
    read_until_prompt(fd, buf, sizeof(buf), 1000);  /* ignore errors here */

    return fd;
}

void bcmcmd_close(int fd)
{
    if (fd >= 0) close(fd);
}

/* Parse 'ps' output into sdk_ports[] and port_names[][].
 * Returns number of entries filled, or -1 on I/O error. */
int bcmcmd_ps(int fd, int *sdk_ports,
              char port_names[][SHIM_PORT_NAME_LEN], int max)
{
    static char buf[READ_BUF_SIZE];
    int n = 0;

    if (write_all(fd, "ps\n") < 0)                          return -1;
    if (read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS) < 0) return -1;

    /* Expected line format: "       xe86(118)  up   ..."
     * or                    "        ce0(  1)  up   ..."
     * The port_name starts after leading spaces; sdk_port is inside parens. */
    char *line = buf;
    while (n < max) {
        char *nl = strchr(line, '\n');
        if (!nl) break;
        *nl = '\0';

        /* Skip header lines (no opening paren after non-space chars). */
        char *paren = strchr(line, '(');
        if (!paren || paren == line) { line = nl + 1; continue; }

        /* Extract port name: scan backward from '(' for start of token. */
        char *name_end = paren;
        char *name_start = paren - 1;
        while (name_start > line && *name_start != ' ') name_start--;
        if (*name_start == ' ') name_start++;

        int namelen = (int)(name_end - name_start);
        if (namelen <= 0 || namelen >= SHIM_PORT_NAME_LEN) {
            line = nl + 1; continue;
        }

        /* Extract sdk_port number inside parens. */
        char *cparen = strchr(paren, ')');
        if (!cparen) { line = nl + 1; continue; }

        char numstr[16] = {0};
        int numlen = (int)(cparen - paren - 1);
        if (numlen <= 0 || numlen >= (int)sizeof(numstr)) {
            line = nl + 1; continue;
        }
        memcpy(numstr, paren + 1, (size_t)numlen);
        int sdk_port = atoi(numstr);
        if (sdk_port <= 0) { line = nl + 1; continue; }

        strncpy(port_names[n], name_start, (size_t)namelen);
        port_names[n][namelen] = '\0';
        sdk_ports[n] = sdk_port;
        n++;
        line = nl + 1;
    }
    return n;
}

/* Look up raw counter value by name in a port_row_t's raw[] table.
 * Returns 0 if not found. */
static uint64_t raw_lookup(const port_row_t *row, const char *name)
{
    for (int i = 0; i < row->n_raw; i++)
        if (strcmp(row->raw[i].name, name) == 0)
            return row->raw[i].value;
    return 0;
}

/* Find or create a port_row_t for port_name.  Returns NULL if cache is full. */
static port_row_t *cache_row(counter_cache_t *cache, const char *port_name)
{
    for (int i = 0; i < cache->n_rows; i++)
        if (strcmp(cache->rows[i].port_name, port_name) == 0)
            return &cache->rows[i];
    if (cache->n_rows >= SHIM_MAX_PORTS)
        return NULL;
    port_row_t *row = &cache->rows[cache->n_rows++];
    memset(row, 0, sizeof(*row));
    /* Use memcpy+NUL to avoid -Wstringop-truncation with strncpy. */
    size_t pnlen = strlen(port_name);
    if (pnlen >= SHIM_PORT_NAME_LEN) pnlen = SHIM_PORT_NAME_LEN - 1;
    memcpy(row->port_name, port_name, pnlen);
    row->port_name[pnlen] = '\0';
    return row;
}

/* Parse 'show counters' output and fill cache.
 * Line format: "COUNTER.port_name\t\t:\t\tvalue\t[+delta]"
 * where value has comma thousands-separators.
 * Only non-zero entries are emitted by bcmcmd. */
static int parse_counters(const char *buf, counter_cache_t *cache)
{
    cache->n_rows = 0;  /* reset rows; rebuild from output */

    const char *p = buf;
    while (*p) {
        const char *nl = strchr(p, '\n');
        if (!nl) break;

        /* Find the dot separating COUNTER.port */
        const char *dot = (const char *)memchr(p, '.', (size_t)(nl - p));
        if (!dot || dot <= p) { p = nl + 1; continue; }

        /* Find the colon */
        const char *colon = (const char *)memchr(dot, ':', (size_t)(nl - dot));
        if (!colon) { p = nl + 1; continue; }

        /* Extract counter name (before dot) */
        int cname_len = (int)(dot - p);
        if (cname_len <= 0 || cname_len >= 24) { p = nl + 1; continue; }
        char cname[24];
        memcpy(cname, p, (size_t)cname_len);
        cname[cname_len] = '\0';

        /* Extract port name (between dot and first whitespace) */
        const char *pname_start = dot + 1;
        const char *pname_end   = pname_start;
        while (pname_end < nl && *pname_end != ' ' && *pname_end != '\t')
            pname_end++;
        int pname_len = (int)(pname_end - pname_start);
        if (pname_len <= 0 || pname_len >= SHIM_PORT_NAME_LEN) {
            p = nl + 1; continue;
        }
        char pname[SHIM_PORT_NAME_LEN];
        memcpy(pname, pname_start, (size_t)pname_len);
        pname[pname_len] = '\0';

        /* Extract value (after colon, skip whitespace, read digits and commas) */
        const char *vp = colon + 1;
        while (vp < nl && (*vp == ' ' || *vp == '\t')) vp++;
        uint64_t value = 0;
        int got_digit = 0;
        while (vp < nl && (*vp == ',' || (*vp >= '0' && *vp <= '9'))) {
            if (*vp != ',') { value = value * 10 + (uint64_t)(*vp - '0'); got_digit = 1; }
            vp++;
        }
        if (!got_digit) { p = nl + 1; continue; }

        /* Store into cache. */
        port_row_t *row = cache_row(cache, pname);
        if (!row) { p = nl + 1; continue; }  /* cache full: skip */

        /* Store in raw[] for name2 lookups. */
        if (row->n_raw < (int)(sizeof(row->raw)/sizeof(row->raw[0]))) {
            /* Use memcpy+NUL to avoid -Wstringop-truncation with strncpy. */
            size_t cnlen = strlen(cname);
            if (cnlen > 23) cnlen = 23;
            memcpy(row->raw[row->n_raw].name, cname, cnlen);
            row->raw[row->n_raw].name[cnlen] = '\0';
            row->raw[row->n_raw].value = value;
            row->n_raw++;
        }

        /* Also resolve into indexed val[] for the stat_map. */
        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name1 && strcmp(g_stat_map[i].name1, cname) == 0)
                row->val[i] += value;
            /* name2 sums are resolved after all lines parsed (see below). */
        }

        p = nl + 1;
    }

    /* Second pass: resolve name2 sums for dual-counter stats. */
    for (int r = 0; r < cache->n_rows; r++) {
        port_row_t *row = &cache->rows[r];
        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name2)
                row->val[i] += raw_lookup(row, g_stat_map[i].name2);
        }
    }

    return 0;
}

int bcmcmd_fetch_counters(int fd, counter_cache_t *cache)
{
    static char buf[READ_BUF_SIZE];

    if (write_all(fd, "show counters\n") < 0)
        return -1;
    int n = read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS);
    if (n < 0) return -1;

    pthread_mutex_lock(&cache->lock);
    parse_counters(buf, cache);
    clock_gettime(CLOCK_MONOTONIC, &cache->fetched_at);
    pthread_mutex_unlock(&cache->lock);
    return 0;
}
