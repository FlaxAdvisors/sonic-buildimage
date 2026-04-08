/**
 * @file bcmcmd_client.c
 * @brief BCM diagnostic shell Unix socket client implementation.
 *
 * Implements connect/close/ps/fetch_counters functions that communicate
 * with the BCM diagnostic shell (sswsyncd) via a Unix domain socket.
 * Moved from sai-stat-shim to flex-counter-daemon.
 */
#include "bcmcmd_client.h"
#include "stat_map.h"

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
#define READ_BUF_SIZE 262144
#define SEND_TIMEOUT_MS 2000
#define RECV_TIMEOUT_MS 3000
#define COUNTER_RECV_TIMEOUT_MS 8000

/**
 * @brief Read from fd until the drivshell> prompt appears or timeout expires.
 *
 * @param fd         Socket file descriptor.
 * @param buf        Output buffer for received data.
 * @param bufsz      Size of buf.
 * @param timeout_ms Receive timeout per poll call in milliseconds.
 * @return Total bytes read on success, -1 on timeout or error.
 */
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

/**
 * @brief Write all bytes of string s to fd, retrying on short writes.
 *
 * @param fd Socket file descriptor.
 * @param s  Null-terminated string to write.
 * @return 0 on success, -1 on write error.
 */
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

int bcmcmd_connect(const char *path, int timeout_ms)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    int rc = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    if (rc < 0 && errno != EINPROGRESS) { close(fd); return -1; }

    if (errno == EINPROGRESS) {
        struct pollfd pfd = { .fd = fd, .events = POLLOUT };
        int pr = poll(&pfd, 1, timeout_ms);
        if (pr <= 0) {
            close(fd); return -1;
        }
        int err = 0;
        socklen_t elen = sizeof(err);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen);
        if (err) { close(fd); errno = err; return -1; }
    }

    fcntl(fd, F_SETFL, flags);

    char buf[READ_BUF_SIZE];
    buf[0] = '\0';
    write_all(fd, "\n");
    int rc_banner = read_until_prompt(fd, buf, sizeof(buf), 3000);
    if (rc_banner < 0) {
        syslog(LOG_WARNING, "flex-counter-daemon: bcmcmd banner timeout");
        close(fd); return -1;
    }

    return fd;
}

void bcmcmd_close(int fd)
{
    if (fd >= 0) close(fd);
}

int bcmcmd_ps(int fd, int *sdk_ports,
              char port_names[][BCMCMD_PORT_NAME_LEN], int max)
{
    static char buf[READ_BUF_SIZE];
    int n = 0;

    if (write_all(fd, "ps\n") < 0)                          return -1;
    if (read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS) < 0) return -1;

    char *line = buf;
    while (n < max) {
        char *nl = strchr(line, '\n');
        if (!nl) break;
        *nl = '\0';

        char *paren = strchr(line, '(');
        if (!paren || paren == line) { line = nl + 1; continue; }

        char *name_end = paren;
        char *name_start = paren - 1;
        while (name_start > line && *name_start != ' ') name_start--;
        if (*name_start == ' ') name_start++;

        int namelen = (int)(name_end - name_start);
        if (namelen <= 0 || namelen >= BCMCMD_PORT_NAME_LEN) {
            line = nl + 1; continue;
        }

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

static uint64_t raw_lookup(const port_row_t *row, const char *name)
{
    for (int i = 0; i < row->n_raw; i++)
        if (strcmp(row->raw[i].name, name) == 0)
            return row->raw[i].value;
    return 0;
}

static port_row_t *cache_row(counter_cache_t *cache, const char *port_name)
{
    for (int i = 0; i < cache->n_rows; i++)
        if (strcmp(cache->rows[i].port_name, port_name) == 0)
            return &cache->rows[i];
    if (cache->n_rows >= BCMCMD_MAX_PORTS)
        return NULL;
    port_row_t *row = &cache->rows[cache->n_rows++];
    memset(row, 0, sizeof(*row));
    size_t pnlen = strlen(port_name);
    if (pnlen >= BCMCMD_PORT_NAME_LEN) pnlen = BCMCMD_PORT_NAME_LEN - 1;
    memcpy(row->port_name, port_name, pnlen);
    row->port_name[pnlen] = '\0';
    return row;
}

/**
 * @brief Parse 'show c all' output lines into counter_cache_t rows.
 *
 * Expected line format: "COUNTERNAME.PORTNAME : VALUE [+DELTA RATE/s]"
 * Accumulates name1 matches into val[] (for later name2 resolution).
 * Does NOT clear val[] — caller must zero rows before a new cycle.
 *
 * @param buf   NUL-terminated bcmcmd output string.
 * @param cache Counter cache to populate.
 */
static void parse_lines(const char *buf, counter_cache_t *cache)
{
    const char *p = buf;
    while (*p) {
        const char *nl = strchr(p, '\n');
        if (!nl) break;

        const char *dot = (const char *)memchr(p, '.', (size_t)(nl - p));
        if (!dot || dot <= p) { p = nl + 1; continue; }

        const char *colon = (const char *)memchr(dot, ':', (size_t)(nl - dot));
        if (!colon) { p = nl + 1; continue; }

        int cname_len = (int)(dot - p);
        if (cname_len <= 0 || cname_len >= 24) { p = nl + 1; continue; }
        char cname[24];
        memcpy(cname, p, (size_t)cname_len);
        cname[cname_len] = '\0';

        const char *pname_start = dot + 1;
        const char *pname_end   = pname_start;
        while (pname_end < nl && *pname_end != ' ' && *pname_end != '\t')
            pname_end++;
        int pname_len = (int)(pname_end - pname_start);
        if (pname_len <= 0 || pname_len >= BCMCMD_PORT_NAME_LEN) {
            p = nl + 1; continue;
        }
        char pname[BCMCMD_PORT_NAME_LEN];
        memcpy(pname, pname_start, (size_t)pname_len);
        pname[pname_len] = '\0';

        const char *vp = colon + 1;
        while (vp < nl && (*vp == ' ' || *vp == '\t')) vp++;
        uint64_t value = 0;
        int got_digit = 0;
        while (vp < nl && (*vp == ',' || (*vp >= '0' && *vp <= '9'))) {
            if (*vp != ',') { value = value * 10 + (uint64_t)(*vp - '0'); got_digit = 1; }
            vp++;
        }
        if (!got_digit) { p = nl + 1; continue; }

        port_row_t *row = cache_row(cache, pname);
        if (!row) { p = nl + 1; continue; }

        if (row->n_raw < (int)(sizeof(row->raw)/sizeof(row->raw[0]))) {
            size_t cnlen = strlen(cname);
            if (cnlen > 23) cnlen = 23;
            memcpy(row->raw[row->n_raw].name, cname, cnlen);
            row->raw[row->n_raw].name[cnlen] = '\0';
            row->raw[row->n_raw].value = value;
            row->n_raw++;
        }

        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name1 && strcmp(g_stat_map[i].name1, cname) == 0)
                row->val[i] += value;
        }

        p = nl + 1;
    }
}

/**
 * @brief Resolve name2 (second BCM counter) for compound SAI stats.
 *
 * For stats like IF_IN_NON_UCAST_PKTS = RMCA + RBCA, the name2 counter
 * must be added to the val[] entry after all lines are parsed so that
 * the raw[] lookup table is fully populated.
 *
 * @param cache Counter cache with fully parsed raw[] arrays.
 */
static void resolve_name2(counter_cache_t *cache)
{
    for (int r = 0; r < cache->n_rows; r++) {
        port_row_t *row = &cache->rows[r];
        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name2)
                row->val[i] += raw_lookup(row, g_stat_map[i].name2);
        }
    }
}

/* Clear val[] and n_raw for all cache rows — call before each poll cycle. */
void bcmcmd_cache_clear(counter_cache_t *cache)
{
    for (int i = 0; i < cache->n_rows; i++) {
        memset(cache->rows[i].val, 0, sizeof(cache->rows[i].val));
        cache->rows[i].n_raw = 0;
    }
}

int bcmcmd_fetch_counters(int fd, counter_cache_t *cache)
{
    /* 'show c all' returns ~1.35 MB for 128 ports — needs large buffer.
     * Using 'show c all' instead of 'show counters' because the latter
     * only returns ports whose counters changed since the last call,
     * causing stale data and rate computation errors. */
    static char buf[COUNTER_BUF_SIZE];

    if (write_all(fd, "show c all\n") < 0)
        return -1;
    int n = read_until_prompt(fd, buf, sizeof(buf), COUNTER_RECV_TIMEOUT_MS);
    if (n < 0) return -1;

    bcmcmd_cache_clear(cache);
    parse_lines(buf, cache);
    resolve_name2(cache);
    clock_gettime(CLOCK_MONOTONIC, &cache->fetched_at);
    return 0;
}

int bcmcmd_fetch_port_counters(int fd, counter_cache_t *cache,
                               const char ports[][BCMCMD_PORT_NAME_LEN],
                               int n_ports)
{
    /* Query each port individually with 'show c all <port>'.
     * Returns all counters per port (~268 lines, ~10KB) vs
     * ~1.35 MB / ~2s for 'show c all' (all 128 ports).
     * 12 ports: ~0.2s total — 10x faster, 92% less data. */
    static char buf[READ_BUF_SIZE];

    bcmcmd_cache_clear(cache);

    for (int p = 0; p < n_ports; p++) {
        char cmd[64];
        snprintf(cmd, sizeof(cmd), "show c all %s\n", ports[p]);
        if (write_all(fd, cmd) < 0)
            return -1;
        int n = read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS);
        if (n < 0) return -1;
        parse_lines(buf, cache);
    }

    resolve_name2(cache);
    clock_gettime(CLOCK_MONOTONIC, &cache->fetched_at);
    return 0;
}
