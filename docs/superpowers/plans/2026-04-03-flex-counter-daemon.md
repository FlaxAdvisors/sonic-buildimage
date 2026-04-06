# Flex Counter Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the monolithic sai-stat-shim into a minimal fault-masker shim + a standalone flex counter daemon that writes real bcmcmd counters to COUNTERS_DB via Redis, fixing `port_state_change` notification breakage.

**Architecture:** The shim becomes ~50 lines (intercept `get_port_stats`, return zeros on failure). A new C daemon (`wedge100s-flex-counter-daemon`) polls bcmcmd every 3s, resolves flex ports via COUNTERS_DB key count, and writes accumulated counters via hiredis. Both ship in the existing platform .deb and run inside the syncd container.

**Tech Stack:** C (gcc), hiredis, Unix domain sockets, supervisor (syncd container)

**Spec:** `docs/superpowers/specs/2026-04-03-flex-counter-daemon-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Rewrite | `wedge100s-32x/sai-stat-shim/shim.c` | Fault masker only (~50 lines) |
| Simplify | `wedge100s-32x/sai-stat-shim/shim.h` | SAI type stubs only (remove bcmcmd/cache types) |
| Simplify | `wedge100s-32x/sai-stat-shim/Makefile` | Build only shim.o + compat.o |
| Create | `wedge100s-32x/flex-counter-daemon/daemon.c` | Main loop, Redis I/O, flex detection, OID→port mapping |
| Create | `wedge100s-32x/flex-counter-daemon/bcmcmd_client.c` | Moved from shim — socket I/O, ps/show-counters parsers |
| Create | `wedge100s-32x/flex-counter-daemon/bcmcmd_client.h` | Public API for bcmcmd client (replaces shim.h dependency) |
| Create | `wedge100s-32x/flex-counter-daemon/stat_map.c` | Moved from shim — SAI↔bcmcmd counter name table |
| Create | `wedge100s-32x/flex-counter-daemon/stat_map.h` | Public API for stat map (replaces shim.h dependency) |
| Create | `wedge100s-32x/flex-counter-daemon/compat.c` | Copied from shim — glibc __isoc23_sscanf compat |
| Create | `wedge100s-32x/flex-counter-daemon/Makefile` | Builds `wedge100s-flex-counter-daemon`, links hiredis |
| Modify | `debian/rules` | Add flex-counter-daemon build + clean blocks |
| Modify | `debian/sonic-platform-accton-wedge100s-32x.install` | Add daemon binary to /usr/bin |
| Modify | `debian/sonic-platform-accton-wedge100s-32x.postinst` | Add supervisor config for daemon in syncd container |

All paths below are relative to `platform/broadcom/sonic-platform-modules-accton/`.

---

### Task 1: Strip shim to fault-masker only

Rewrite `shim.c` and `shim.h` to contain only the fault masker logic. Remove all bcmcmd, cache, OID classification, lane mapping, and ps logic.

**Files:**
- Rewrite: `wedge100s-32x/sai-stat-shim/shim.c`
- Rewrite: `wedge100s-32x/sai-stat-shim/shim.h`
- Modify: `wedge100s-32x/sai-stat-shim/Makefile`

- [ ] **Step 1: Rewrite shim.h to SAI type stubs only**

Replace the entire contents of `wedge100s-32x/sai-stat-shim/shim.h` with:

```c
/* shim.h — SAI fault masker for Wedge100S-32X flex sub-port counters.
 * Masker-only: returns zeros+SUCCESS when real get_port_stats fails.
 * No bcmcmd, no cache, no lane mapping. */
#pragma once

#include <stdint.h>

/* ---- SAI type stubs (avoids build-time dependency on libsaibcm-dev) ---- */
typedef uint64_t sai_object_id_t;
typedef int32_t  sai_status_t;
typedef int32_t  sai_api_t;

#define SAI_STATUS_SUCCESS ((sai_status_t)0)
#define SAI_API_PORT       ((sai_api_t)2)

/* SAI port API function pointer types (subset needed by masker). */
typedef sai_status_t (*sai_get_port_stats_fn)(
    sai_object_id_t port_id, uint32_t count,
    const uint32_t *ids, uint64_t *values);

typedef sai_status_t (*sai_get_port_stats_ext_fn)(
    sai_object_id_t port_id, uint32_t count,
    const uint32_t *ids, int mode, uint64_t *values);

/* sai_port_api_t — only fields we touch.
 * Offsets verified against libsaibcm 14.3.0.0.0.0.3.0. */
typedef struct {
    void *create_port;           /* [0] */
    void *remove_port;           /* [1] */
    void *set_port_attribute;    /* [2] */
    void *get_port_attribute;    /* [3] */
    sai_get_port_stats_fn     get_port_stats;      /* [4] */
    sai_get_port_stats_ext_fn get_port_stats_ext;   /* [5] */
    void *clear_port_stats;      /* [6] */
} sai_port_api_t;

typedef sai_status_t (*sai_api_query_fn)(sai_api_t api, void **api_method_table);
```

- [ ] **Step 2: Rewrite shim.c to fault masker only**

Replace the entire contents of `wedge100s-32x/sai-stat-shim/shim.c` with:

```c
/* shim.c — LD_PRELOAD fault masker for sai_api_query(SAI_API_PORT).
 *
 * Single purpose: prevent FlexCounter from logging errors and dropping
 * flex port keys from COUNTERS_DB.
 *
 * If real get_port_stats returns SUCCESS → passthrough (no overhead).
 * If real get_port_stats returns non-SUCCESS → memset zeros, return SUCCESS.
 *
 * Does NOT touch any other function pointer in sai_port_api_t.
 * Does NOT do bcmcmd, caching, or classification.
 * The flex counter daemon handles real counter values separately.
 */
#define _GNU_SOURCE
#include "shim.h"

#include <string.h>
#include <dlfcn.h>
#include <syslog.h>

/* ---- globals ---- */

static sai_get_port_stats_fn g_real_get_port_stats = NULL;

/* Buffer for our copy of the port API struct.
 * Real SAI sai_port_api_t has 35 function pointer fields (280 bytes).
 * We copy the entire thing to be layout-agnostic. */
#define PORT_API_STRUCT_SIZE 512
static char g_shim_port_api_buf[PORT_API_STRUCT_SIZE]
    __attribute__((aligned(8)));

/* ---- fault masker ---- */

static sai_status_t shim_get_port_stats(
    sai_object_id_t oid, uint32_t count,
    const uint32_t *ids, uint64_t *values)
{
    sai_status_t st = g_real_get_port_stats(oid, count, ids, values);
    if (st == SAI_STATUS_SUCCESS)
        return st;
    memset(values, 0, count * sizeof(uint64_t));
    return SAI_STATUS_SUCCESS;
}

/* ---- sai_api_query intercept ---- */

sai_status_t sai_api_query(sai_api_t api, void **api_method_table)
{
    static sai_api_query_fn real_query = NULL;
    if (!real_query)
        real_query = (sai_api_query_fn)dlsym(RTLD_NEXT, "sai_api_query");
    if (!real_query) return -1;

    sai_status_t st = real_query(api, api_method_table);
    if (st != SAI_STATUS_SUCCESS || api != SAI_API_PORT)
        return st;

    sai_port_api_t *real_api = (sai_port_api_t *)*api_method_table;

    g_real_get_port_stats = real_api->get_port_stats;

    /* Copy the full 35-pointer struct, then patch our copy. */
    memcpy(g_shim_port_api_buf, real_api, 35 * sizeof(void *));
    sai_port_api_t *shim_api = (sai_port_api_t *)g_shim_port_api_buf;
    shim_api->get_port_stats = shim_get_port_stats;

    *api_method_table = (void *)shim_api;

    syslog(LOG_INFO, "shim: fault masker active (get_port_stats only)");
    return SAI_STATUS_SUCCESS;
}
```

- [ ] **Step 3: Update shim Makefile to build only masker**

Replace the Makefile contents:

```makefile
# Makefile for libsai-stat-shim.so (fault masker only)
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC

OBJS = shim.o compat.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so
```

- [ ] **Step 4: Remove old shim-only source files**

Delete the files that are moving to the daemon (they are NOT needed by the shim anymore):

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
rm -f bcmcmd_client.c stat_map.c test_parser.c
```

- [ ] **Step 5: Verify shim builds**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
make clean && make
```

Expected: `libsai-stat-shim.so` builds with no errors. Only `shim.o` and `compat.o` linked.

- [ ] **Step 6: Commit**

```bash
git add wedge100s-32x/sai-stat-shim/
git commit -m "refactor(shim): strip to fault-masker only — remove bcmcmd, cache, classification

The flex counter daemon (next commit) handles real counter values.
The masker's only job: return zeros+SUCCESS when real get_port_stats fails,
keeping flex port keys alive in COUNTERS_DB."
```

---

### Task 2: Create flex-counter-daemon directory with moved bcmcmd_client and stat_map

Move `bcmcmd_client.c` and `stat_map.c` into the new daemon directory with standalone headers (no shim.h dependency).

**Files:**
- Create: `wedge100s-32x/flex-counter-daemon/bcmcmd_client.h`
- Create: `wedge100s-32x/flex-counter-daemon/bcmcmd_client.c`
- Create: `wedge100s-32x/flex-counter-daemon/stat_map.h`
- Create: `wedge100s-32x/flex-counter-daemon/stat_map.c`
- Create: `wedge100s-32x/flex-counter-daemon/compat.c`

- [ ] **Step 1: Create bcmcmd_client.h**

```c
/* bcmcmd_client.h — BCM diag shell Unix socket client.
 * Standalone header for flex-counter-daemon (no shim.h dependency). */
#pragma once

#include <stdint.h>
#include <pthread.h>
#include <time.h>

#define BCMCMD_MAX_PORTS     256
#define BCMCMD_PORT_NAME_LEN 16
#define BCMCMD_MAX_STAT_IDS  80
#define BCMCMD_SOCKET_PATH   "/var/run/sswsyncd/sswsyncd.socket"

/* One row in the port counter cache: port_name + value per stat_map index. */
typedef struct {
    char     port_name[BCMCMD_PORT_NAME_LEN];
    int      sdk_port;
    uint64_t val[BCMCMD_MAX_STAT_IDS];
    int      n_raw;
    struct { char name[24]; uint64_t value; } raw[64];
} port_row_t;

typedef struct {
    port_row_t      rows[BCMCMD_MAX_PORTS];
    int             n_rows;
    struct timespec fetched_at;
    pthread_mutex_t lock;
} counter_cache_t;

int  bcmcmd_connect(const char *path, int timeout_ms);
void bcmcmd_close(int fd);
int  bcmcmd_ps(int fd, int *sdk_ports,
               char port_names[][BCMCMD_PORT_NAME_LEN], int max);
int  bcmcmd_fetch_counters(int fd, counter_cache_t *cache);
```

- [ ] **Step 2: Create bcmcmd_client.c**

Copy `sai-stat-shim/bcmcmd_client.c` to `flex-counter-daemon/bcmcmd_client.c` and change the `#include` from `"shim.h"` to `"bcmcmd_client.h"` and `"stat_map.h"`. Also replace all `SHIM_PORT_NAME_LEN` with `BCMCMD_PORT_NAME_LEN`, `SHIM_MAX_PORTS` with `BCMCMD_MAX_PORTS`, and the syslog prefix from `"shim:"` to `"flex-counter-daemon:"`.

The full file (adapted from the existing bcmcmd_client.c):

```c
/* bcmcmd_client.c — BCM diag shell Unix socket client.
 * Moved from sai-stat-shim to flex-counter-daemon. */
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

static int parse_counters(const char *buf, counter_cache_t *cache)
{
    for (int i = 0; i < cache->n_rows; i++)
        cache->rows[i].n_raw = 0;

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
```

- [ ] **Step 3: Create stat_map.h**

```c
/* stat_map.h — SAI port stat -> bcmcmd counter name mapping.
 * Standalone header for flex-counter-daemon. */
#pragma once

#include <stdint.h>

typedef uint32_t sai_port_stat_t;

typedef struct {
    sai_port_stat_t  stat_id;
    const char      *name1;
    const char      *name2;
} stat_map_entry_t;

extern const stat_map_entry_t g_stat_map[];
extern const int              g_stat_map_size;

/* SAI stat field name strings for COUNTERS_DB HSET.
 * Indexed by stat_id (enum value). Returns NULL if not mapped. */
const char *sai_stat_field_name(sai_port_stat_t stat_id);

int stat_map_index(sai_port_stat_t stat_id);
```

- [ ] **Step 4: Create stat_map.c**

Copy the existing `sai-stat-shim/stat_map.c` to `flex-counter-daemon/stat_map.c`. Change `#include "shim.h"` to `#include "stat_map.h"`. Add the `sai_stat_field_name()` function and the field name table needed by daemon.c to write COUNTERS_DB keys.

```c
/* stat_map.c — SAI port stat -> bcmcmd counter name mapping.
 * Moved from sai-stat-shim to flex-counter-daemon. */
#include "stat_map.h"

#include <string.h>

#define S(id) ((sai_port_stat_t)(id))

const stat_map_entry_t g_stat_map[] = {
    { S(0),  "RBYT",  NULL   },  /* SAI_PORT_STAT_IF_IN_OCTETS           */
    { S(1),  "RUCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_UCAST_PKTS       */
    { S(2),  "RMCA",  "RBCA" },  /* SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS   */
    { S(3),  "RIDR",  NULL   },  /* SAI_PORT_STAT_IF_IN_DISCARDS         */
    { S(4),  "RFCS",  NULL   },  /* SAI_PORT_STAT_IF_IN_ERRORS           */
    { S(5),  NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_UNKNOWN_PROTOS   */
    { S(6),  "RBCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_BROADCAST_PKTS   */
    { S(7),  "RMCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_MULTICAST_PKTS   */
    { S(9),  "TBYT",  NULL   },  /* SAI_PORT_STAT_IF_OUT_OCTETS          */
    { S(10), "TUCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_UCAST_PKTS      */
    { S(11), "TMCA",  "TBCA" },  /* SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS  */
    { S(12), "TDRP",  NULL   },  /* SAI_PORT_STAT_IF_OUT_DISCARDS        */
    { S(13), "TERR",  NULL   },  /* SAI_PORT_STAT_IF_OUT_ERRORS          */
    { S(14), NULL,    NULL   },  /* SAI_PORT_STAT_IF_OUT_QLEN            */
    { S(15), "TBCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_BROADCAST_PKTS  */
    { S(16), "TMCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_MULTICAST_PKTS  */
    { S(20), "RUND",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_UNDERSIZE_PKTS */
    { S(21), "RFRG",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_FRAGMENTS  */
    { S(33), "ROVR",  NULL   },  /* SAI_PORT_STAT_ETHER_RX_OVERSIZE_PKTS */
    { S(34), "TOVR",  NULL   },  /* SAI_PORT_STAT_ETHER_TX_OVERSIZE_PKTS */
    { S(35), "RJBR",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_JABBERS   */
    { S(40), "TPOK",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_TX_NO_ERRORS */
    { S(42), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_RECEIVES         */
    { S(44), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_UCAST_PKTS       */
    { S(71), "R64",   NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_64_OCTETS */
    { S(72), "R127",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_65_TO_127_OCTETS */
    { S(73), "R255",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_128_TO_255_OCTETS */
    { S(74), "R511",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_256_TO_511_OCTETS */
    { S(75), "R1023", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_512_TO_1023_OCTETS */
    { S(76), "R1518", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1024_TO_1518_OCTETS */
    { S(77), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1519_TO_2047_OCTETS */
    { S(78), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_2048_TO_4095_OCTETS */
    { S(79), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_4096_TO_9216_OCTETS */
    { S(80), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_9217_TO_16383_OCTETS */
    { S(81), "T64",   NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_64_OCTETS */
    { S(82), "T127",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_65_TO_127_OCTETS */
    { S(83), "T255",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_128_TO_255_OCTETS */
    { S(84), "T511",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_256_TO_511_OCTETS */
    { S(85), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_512_TO_1023_OCTETS */
    { S(86), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1024_TO_1518_OCTETS */
    { S(87), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1519_TO_2047_OCTETS */
    { S(88), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_2048_TO_4095_OCTETS */
    { S(89), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_4096_TO_9216_OCTETS */
    { S(90), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_9217_TO_16383_OCTETS */
    { S(99),  "RIDR",  NULL   },  /* SAI_PORT_STAT_IN_DROPPED_PKTS       */
    { S(100), "TDRP",  NULL   },  /* SAI_PORT_STAT_OUT_DROPPED_PKTS      */
    { S(101), "RXCF",  NULL   },  /* SAI_PORT_STAT_PAUSE_RX_PKTS         */
    { S(102), "TXPF",  NULL   },  /* SAI_PORT_STAT_PAUSE_TX_PKTS         */
    { S(103), "RPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_RX_PKTS         */
    { S(104), "TPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_TX_PKTS         */
    { S(105), "RPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_RX_PKTS         */
    { S(106), "TPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_TX_PKTS         */
    { S(107), "RPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_RX_PKTS         */
    { S(108), "TPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_TX_PKTS         */
    { S(109), "RPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_RX_PKTS         */
    { S(110), "TPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_TX_PKTS         */
    { S(111), "RPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_RX_PKTS         */
    { S(112), "TPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_TX_PKTS         */
    { S(113), "RPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_RX_PKTS         */
    { S(114), "TPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_TX_PKTS         */
    { S(115), "RPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_RX_PKTS         */
    { S(116), "TPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_TX_PKTS         */
    { S(117), "RPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_RX_PKTS         */
    { S(118), "TPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_TX_PKTS         */
    { S(178), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES */
    { S(179), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_NOT_CORRECTABLE_FRAMES */
    { S(180), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_SYMBOL_ERRORS */
    { S(202), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTED_BITS */
};
#undef S

const int g_stat_map_size = (int)(sizeof(g_stat_map) / sizeof(g_stat_map[0]));

int stat_map_index(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_stat_map_size; i++)
        if (g_stat_map[i].stat_id == stat_id)
            return i;
    return -1;
}

/* SAI stat field name table — maps stat enum value → COUNTERS_DB hash field name.
 * Only entries that appear in g_stat_map are populated. */
static const struct { uint32_t id; const char *name; } g_field_names[] = {
    {   0, "SAI_PORT_STAT_IF_IN_OCTETS" },
    {   1, "SAI_PORT_STAT_IF_IN_UCAST_PKTS" },
    {   2, "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS" },
    {   3, "SAI_PORT_STAT_IF_IN_DISCARDS" },
    {   4, "SAI_PORT_STAT_IF_IN_ERRORS" },
    {   5, "SAI_PORT_STAT_IF_IN_UNKNOWN_PROTOS" },
    {   6, "SAI_PORT_STAT_IF_IN_BROADCAST_PKTS" },
    {   7, "SAI_PORT_STAT_IF_IN_MULTICAST_PKTS" },
    {   9, "SAI_PORT_STAT_IF_OUT_OCTETS" },
    {  10, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS" },
    {  11, "SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS" },
    {  12, "SAI_PORT_STAT_IF_OUT_DISCARDS" },
    {  13, "SAI_PORT_STAT_IF_OUT_ERRORS" },
    {  14, "SAI_PORT_STAT_IF_OUT_QLEN" },
    {  15, "SAI_PORT_STAT_IF_OUT_BROADCAST_PKTS" },
    {  16, "SAI_PORT_STAT_IF_OUT_MULTICAST_PKTS" },
    {  20, "SAI_PORT_STAT_ETHER_STATS_UNDERSIZE_PKTS" },
    {  21, "SAI_PORT_STAT_ETHER_STATS_FRAGMENTS" },
    {  33, "SAI_PORT_STAT_ETHER_RX_OVERSIZE_PKTS" },
    {  34, "SAI_PORT_STAT_ETHER_TX_OVERSIZE_PKTS" },
    {  35, "SAI_PORT_STAT_ETHER_STATS_JABBERS" },
    {  40, "SAI_PORT_STAT_ETHER_STATS_TX_NO_ERRORS" },
    {  42, "SAI_PORT_STAT_IP_IN_RECEIVES" },
    {  44, "SAI_PORT_STAT_IP_IN_UCAST_PKTS" },
    {  71, "SAI_PORT_STAT_ETHER_IN_PKTS_64_OCTETS" },
    {  72, "SAI_PORT_STAT_ETHER_IN_PKTS_65_TO_127_OCTETS" },
    {  73, "SAI_PORT_STAT_ETHER_IN_PKTS_128_TO_255_OCTETS" },
    {  74, "SAI_PORT_STAT_ETHER_IN_PKTS_256_TO_511_OCTETS" },
    {  75, "SAI_PORT_STAT_ETHER_IN_PKTS_512_TO_1023_OCTETS" },
    {  76, "SAI_PORT_STAT_ETHER_IN_PKTS_1024_TO_1518_OCTETS" },
    {  77, "SAI_PORT_STAT_ETHER_IN_PKTS_1519_TO_2047_OCTETS" },
    {  78, "SAI_PORT_STAT_ETHER_IN_PKTS_2048_TO_4095_OCTETS" },
    {  79, "SAI_PORT_STAT_ETHER_IN_PKTS_4096_TO_9216_OCTETS" },
    {  80, "SAI_PORT_STAT_ETHER_IN_PKTS_9217_TO_16383_OCTETS" },
    {  81, "SAI_PORT_STAT_ETHER_OUT_PKTS_64_OCTETS" },
    {  82, "SAI_PORT_STAT_ETHER_OUT_PKTS_65_TO_127_OCTETS" },
    {  83, "SAI_PORT_STAT_ETHER_OUT_PKTS_128_TO_255_OCTETS" },
    {  84, "SAI_PORT_STAT_ETHER_OUT_PKTS_256_TO_511_OCTETS" },
    {  85, "SAI_PORT_STAT_ETHER_OUT_PKTS_512_TO_1023_OCTETS" },
    {  86, "SAI_PORT_STAT_ETHER_OUT_PKTS_1024_TO_1518_OCTETS" },
    {  87, "SAI_PORT_STAT_ETHER_OUT_PKTS_1519_TO_2047_OCTETS" },
    {  88, "SAI_PORT_STAT_ETHER_OUT_PKTS_2048_TO_4095_OCTETS" },
    {  89, "SAI_PORT_STAT_ETHER_OUT_PKTS_4096_TO_9216_OCTETS" },
    {  90, "SAI_PORT_STAT_ETHER_OUT_PKTS_9217_TO_16383_OCTETS" },
    {  99, "SAI_PORT_STAT_IN_DROPPED_PKTS" },
    { 100, "SAI_PORT_STAT_OUT_DROPPED_PKTS" },
    { 101, "SAI_PORT_STAT_PAUSE_RX_PKTS" },
    { 102, "SAI_PORT_STAT_PAUSE_TX_PKTS" },
    { 103, "SAI_PORT_STAT_PFC_0_RX_PKTS" },
    { 104, "SAI_PORT_STAT_PFC_0_TX_PKTS" },
    { 105, "SAI_PORT_STAT_PFC_1_RX_PKTS" },
    { 106, "SAI_PORT_STAT_PFC_1_TX_PKTS" },
    { 107, "SAI_PORT_STAT_PFC_2_RX_PKTS" },
    { 108, "SAI_PORT_STAT_PFC_2_TX_PKTS" },
    { 109, "SAI_PORT_STAT_PFC_3_RX_PKTS" },
    { 110, "SAI_PORT_STAT_PFC_3_TX_PKTS" },
    { 111, "SAI_PORT_STAT_PFC_4_RX_PKTS" },
    { 112, "SAI_PORT_STAT_PFC_4_TX_PKTS" },
    { 113, "SAI_PORT_STAT_PFC_5_RX_PKTS" },
    { 114, "SAI_PORT_STAT_PFC_5_TX_PKTS" },
    { 115, "SAI_PORT_STAT_PFC_6_RX_PKTS" },
    { 116, "SAI_PORT_STAT_PFC_6_TX_PKTS" },
    { 117, "SAI_PORT_STAT_PFC_7_RX_PKTS" },
    { 118, "SAI_PORT_STAT_PFC_7_TX_PKTS" },
    { 178, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES" },
    { 179, "SAI_PORT_STAT_IF_IN_FEC_NOT_CORRECTABLE_FRAMES" },
    { 180, "SAI_PORT_STAT_IF_IN_FEC_SYMBOL_ERRORS" },
    { 202, "SAI_PORT_STAT_IF_IN_FEC_CORRECTED_BITS" },
};

static const int g_field_names_size =
    (int)(sizeof(g_field_names) / sizeof(g_field_names[0]));

const char *sai_stat_field_name(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_field_names_size; i++)
        if (g_field_names[i].id == stat_id)
            return g_field_names[i].name;
    return NULL;
}
```

- [ ] **Step 5: Copy compat.c**

```bash
cp wedge100s-32x/sai-stat-shim/compat.c wedge100s-32x/flex-counter-daemon/compat.c
```

No changes needed — identical glibc compat shim.

- [ ] **Step 6: Commit**

```bash
git add wedge100s-32x/flex-counter-daemon/bcmcmd_client.h
git add wedge100s-32x/flex-counter-daemon/bcmcmd_client.c
git add wedge100s-32x/flex-counter-daemon/stat_map.h
git add wedge100s-32x/flex-counter-daemon/stat_map.c
git add wedge100s-32x/flex-counter-daemon/compat.c
git commit -m "feat(flex-counter-daemon): add bcmcmd_client and stat_map (moved from shim)

Standalone headers replace shim.h dependency. Identical logic, just decoupled
from the LD_PRELOAD library so the daemon can be a regular binary."
```

---

### Task 3: Write daemon.c — main loop with Redis I/O and flex detection

The core daemon: polls bcmcmd, detects flex ports via COUNTERS_DB key count, writes accumulated counters.

**Files:**
- Create: `wedge100s-32x/flex-counter-daemon/daemon.c`
- Create: `wedge100s-32x/flex-counter-daemon/Makefile`

- [ ] **Step 1: Create daemon.c**

```c
/* daemon.c — Flex counter daemon for Wedge100S-32X.
 *
 * Polls bcmcmd 'show counters' every 3s, accumulates per-port deltas,
 * identifies flex sub-ports by COUNTERS_DB key count (<=2 = flex),
 * and writes all 66 SAI stat fields to COUNTERS_DB via Redis HMSET.
 *
 * OID-to-port resolution chain:
 *   COUNTERS_PORT_NAME_MAP (DB 2) -> Ethernet name -> SAI OID
 *   CONFIG_DB PORT|EthernetN (DB 4) -> lanes field
 *   BCM config portmap -> lane -> SDK port number
 *   bcmcmd ps -> SDK port number -> port name (xe86, ce0, ...)
 */
#include "bcmcmd_client.h"
#include "stat_map.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <syslog.h>
#include <time.h>
#include <hiredis/hiredis.h>

#define DAEMON_NAME        "flex-counter-daemon"
#define POLL_INTERVAL_S    3
#define CONNECT_TIMEOUT_MS 50
#define FLEX_KEY_THRESHOLD 2
#define MAX_LANE_MAP       512
#define MAX_PS_MAP         256
#define REDIS_TIMEOUT_MS   500

/* ---- lane map (BCM config: lane -> SDK port) ---- */

typedef struct {
    uint32_t physical_lane;
    int      sdk_port;
} lane_entry_t;

static lane_entry_t g_lane_map[MAX_LANE_MAP];
static int          g_lane_map_size = 0;

/* ---- ps map (bcmcmd ps: SDK port -> port name) ---- */

static struct {
    int  sdk_port;
    char name[BCMCMD_PORT_NAME_LEN];
} g_ps_map[MAX_PS_MAP];
static int g_ps_map_size = 0;

/* ---- counter cache ---- */

static counter_cache_t g_cache;

/* ---- signal handling ---- */

static volatile sig_atomic_t g_running = 1;

static void sig_handler(int sig)
{
    (void)sig;
    g_running = 0;
}

/* ---- BCM config parser ---- */

static void parse_bcm_config(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        syslog(LOG_ERR, "%s: cannot open BCM config '%s': %m", DAEMON_NAME, path);
        return;
    }
    char line[256];
    while (fgets(line, sizeof(line), f) && g_lane_map_size < MAX_LANE_MAP) {
        int sdk_port, phys_lane, speed;
        if (sscanf(line, "portmap_%d.0=%d:%d", &sdk_port, &phys_lane, &speed) == 3) {
            g_lane_map[g_lane_map_size].physical_lane = (uint32_t)phys_lane;
            g_lane_map[g_lane_map_size].sdk_port      = sdk_port;
            g_lane_map_size++;
        }
    }
    fclose(f);
    syslog(LOG_INFO, "%s: parsed %d lane entries from %s",
           DAEMON_NAME, g_lane_map_size, path);
}

/* ---- lookup helpers ---- */

static int sdk_port_for_lane(uint32_t lane)
{
    for (int i = 0; i < g_lane_map_size; i++)
        if (g_lane_map[i].physical_lane == lane)
            return g_lane_map[i].sdk_port;
    return -1;
}

static const char *port_name_for_sdk(int sdk_port)
{
    for (int i = 0; i < g_ps_map_size; i++)
        if (g_ps_map[i].sdk_port == sdk_port)
            return g_ps_map[i].name;
    return NULL;
}

/* ---- bcmcmd ps map refresh ---- */

static int refresh_ps_map(void)
{
    int fd = bcmcmd_connect(BCMCMD_SOCKET_PATH, CONNECT_TIMEOUT_MS);
    if (fd < 0) return -1;

    int sdk_ports[BCMCMD_MAX_PORTS];
    char names[BCMCMD_MAX_PORTS][BCMCMD_PORT_NAME_LEN];
    int n = bcmcmd_ps(fd, sdk_ports, names, BCMCMD_MAX_PORTS);
    bcmcmd_close(fd);

    if (n < 0) return -1;

    g_ps_map_size = n;
    for (int i = 0; i < n; i++) {
        g_ps_map[i].sdk_port = sdk_ports[i];
        strncpy(g_ps_map[i].name, names[i], BCMCMD_PORT_NAME_LEN - 1);
        g_ps_map[i].name[BCMCMD_PORT_NAME_LEN - 1] = '\0';
    }
    syslog(LOG_INFO, "%s: ps map refreshed (%d ports)", DAEMON_NAME, n);
    return 0;
}

/* ---- Redis helpers ---- */

static redisContext *redis_connect(int db)
{
    struct timeval tv = { .tv_sec = 0, .tv_usec = REDIS_TIMEOUT_MS * 1000 };
    redisContext *c = redisConnectWithTimeout("127.0.0.1", 6379, tv);
    if (!c || c->err) {
        if (c) redisFree(c);
        return NULL;
    }
    redisReply *r = redisCommand(c, "SELECT %d", db);
    if (r) freeReplyObject(r);
    return c;
}

/* Check how many SAI_PORT_STAT_* keys exist for an OID in COUNTERS_DB.
 * Returns key count, or -1 on error. */
static int counters_key_count(redisContext *c, const char *oid)
{
    redisReply *r = redisCommand(c, "HKEYS COUNTERS:%s", oid);
    if (!r || r->type != REDIS_REPLY_ARRAY) {
        if (r) freeReplyObject(r);
        return -1;
    }
    int count = 0;
    for (size_t i = 0; i < r->elements; i++) {
        if (r->element[i]->type == REDIS_REPLY_STRING &&
            strncmp(r->element[i]->str, "SAI_PORT_STAT_", 14) == 0)
            count++;
    }
    freeReplyObject(r);
    return count;
}

/* Resolve Ethernet port name -> bcmcmd port name via CONFIG_DB lanes -> lane_map -> ps_map.
 * Returns NULL if resolution fails. */
static const char *resolve_port(redisContext *cfg_db, const char *eth_name)
{
    /* Get lanes from CONFIG_DB PORT|EthernetN */
    redisReply *r = redisCommand(cfg_db, "HGET PORT|%s lanes", eth_name);
    if (!r || r->type != REDIS_REPLY_STRING || !r->str) {
        if (r) freeReplyObject(r);
        return NULL;
    }

    /* lanes is a comma-separated list; use the first lane for port lookup. */
    uint32_t first_lane = (uint32_t)atoi(r->str);
    freeReplyObject(r);

    int sdk_port = sdk_port_for_lane(first_lane);
    if (sdk_port < 0) return NULL;

    return port_name_for_sdk(sdk_port);
}

/* Write all 66 SAI stat fields for a flex OID using HMSET.
 * Values come from the counter cache row. */
static void write_counters(redisContext *c, const char *oid, const port_row_t *row)
{
    /* Build HMSET command with all stat fields.
     * Format: HMSET COUNTERS:<oid> field1 val1 field2 val2 ... */
    char cmd[8192];
    int pos = snprintf(cmd, sizeof(cmd), "HMSET COUNTERS:%s", oid);

    for (int i = 0; i < g_stat_map_size && pos < (int)sizeof(cmd) - 128; i++) {
        const char *field = sai_stat_field_name(g_stat_map[i].stat_id);
        if (!field) continue;
        pos += snprintf(cmd + pos, sizeof(cmd) - (size_t)pos,
                        " %s %lu", field, (unsigned long)row->val[i]);
    }

    redisReply *r = redisCommand(c, cmd);
    if (r) freeReplyObject(r);
}

/* ---- main loop ---- */

int main(void)
{
    openlog(DAEMON_NAME, LOG_PID | LOG_NDELAY, LOG_DAEMON);
    signal(SIGTERM, sig_handler);
    signal(SIGINT,  sig_handler);

    /* Parse BCM config. */
    const char *bcm_cfg = getenv("WEDGE100S_BCM_CONFIG");
    if (bcm_cfg)
        parse_bcm_config(bcm_cfg);
    else
        syslog(LOG_WARNING, "%s: WEDGE100S_BCM_CONFIG not set", DAEMON_NAME);

    /* Init counter cache. */
    memset(&g_cache, 0, sizeof(g_cache));
    pthread_mutex_init(&g_cache.lock, NULL);

    /* Initial ps map — may fail if bcmcmd not ready yet; retried in loop. */
    refresh_ps_map();

    int bcmcmd_warn_logged = 0;
    int redis_warn_logged = 0;
    int ps_map_retries = 0;

    syslog(LOG_INFO, "%s: starting (poll interval %ds)", DAEMON_NAME, POLL_INTERVAL_S);

    while (g_running) {
        sleep(POLL_INTERVAL_S);
        if (!g_running) break;

        /* Retry ps map if it failed at startup. */
        if (g_ps_map_size == 0 && ps_map_retries < 10) {
            refresh_ps_map();
            ps_map_retries++;
        }

        /* Connect to bcmcmd, fetch counters, disconnect. */
        int fd = bcmcmd_connect(BCMCMD_SOCKET_PATH, CONNECT_TIMEOUT_MS);
        if (fd < 0) {
            if (!bcmcmd_warn_logged) {
                syslog(LOG_WARNING, "%s: bcmcmd socket unavailable, will retry",
                       DAEMON_NAME);
                bcmcmd_warn_logged = 1;
            }
            continue;
        }
        if (bcmcmd_warn_logged) {
            syslog(LOG_INFO, "%s: bcmcmd connected", DAEMON_NAME);
            bcmcmd_warn_logged = 0;
        }

        /* If ps map is still empty, try via this connection. */
        if (g_ps_map_size == 0) {
            int sdk_ports[BCMCMD_MAX_PORTS];
            char names[BCMCMD_MAX_PORTS][BCMCMD_PORT_NAME_LEN];
            int n = bcmcmd_ps(fd, sdk_ports, names, BCMCMD_MAX_PORTS);
            if (n > 0) {
                g_ps_map_size = n;
                for (int i = 0; i < n; i++) {
                    g_ps_map[i].sdk_port = sdk_ports[i];
                    strncpy(g_ps_map[i].name, names[i], BCMCMD_PORT_NAME_LEN - 1);
                    g_ps_map[i].name[BCMCMD_PORT_NAME_LEN - 1] = '\0';
                }
                syslog(LOG_INFO, "%s: late ps fetch got %d ports", DAEMON_NAME, n);
            }
        }

        bcmcmd_fetch_counters(fd, &g_cache);
        bcmcmd_close(fd);

        /* Connect to Redis COUNTERS_DB (2) and CONFIG_DB (4). */
        redisContext *cdb = redis_connect(2);
        redisContext *cfgdb = redis_connect(4);
        if (!cdb || !cfgdb) {
            if (!redis_warn_logged) {
                syslog(LOG_WARNING, "%s: Redis unavailable, will retry", DAEMON_NAME);
                redis_warn_logged = 1;
            }
            if (cdb) redisFree(cdb);
            if (cfgdb) redisFree(cfgdb);
            continue;
        }
        if (redis_warn_logged) {
            syslog(LOG_INFO, "%s: Redis connected", DAEMON_NAME);
            redis_warn_logged = 0;
        }

        /* Read COUNTERS_PORT_NAME_MAP: Ethernet name -> OID. */
        redisReply *map_reply = redisCommand(cdb, "HGETALL COUNTERS_PORT_NAME_MAP");
        if (!map_reply || map_reply->type != REDIS_REPLY_ARRAY) {
            if (map_reply) freeReplyObject(map_reply);
            redisFree(cdb);
            redisFree(cfgdb);
            continue;
        }

        /* For each port in the map, check if it's flex. */
        for (size_t i = 0; i + 1 < map_reply->elements; i += 2) {
            const char *eth_name = map_reply->element[i]->str;
            const char *oid      = map_reply->element[i + 1]->str;
            if (!eth_name || !oid) continue;

            /* Check key count — flex ports have <=2 keys. */
            int nkeys = counters_key_count(cdb, oid);
            if (nkeys < 0 || nkeys > FLEX_KEY_THRESHOLD) continue;

            /* Resolve Ethernet -> bcmcmd port name. */
            const char *pname = resolve_port(cfgdb, eth_name);
            if (!pname) continue;

            /* Find the port in the counter cache. */
            pthread_mutex_lock(&g_cache.lock);
            port_row_t *row = NULL;
            for (int r = 0; r < g_cache.n_rows; r++) {
                if (strcmp(g_cache.rows[r].port_name, pname) == 0) {
                    row = &g_cache.rows[r];
                    break;
                }
            }
            if (row) {
                write_counters(cdb, oid, row);
            }
            pthread_mutex_unlock(&g_cache.lock);
        }

        freeReplyObject(map_reply);
        redisFree(cdb);
        redisFree(cfgdb);
    }

    syslog(LOG_INFO, "%s: shutting down", DAEMON_NAME);
    closelog();
    return 0;
}
```

- [ ] **Step 2: Create Makefile**

```makefile
# Makefile for wedge100s-flex-counter-daemon
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g
LDFLAGS = -lhiredis -lpthread

OBJS = daemon.o bcmcmd_client.o stat_map.o compat.o

wedge100s-flex-counter-daemon: $(OBJS)
	$(CC) -o $@ $(OBJS) $(LDFLAGS)

daemon.o: daemon.c bcmcmd_client.h stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

bcmcmd_client.o: bcmcmd_client.c bcmcmd_client.h stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

stat_map.o: stat_map.c stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

compat.o: compat.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o wedge100s-flex-counter-daemon
```

- [ ] **Step 3: Verify daemon builds locally**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/flex-counter-daemon
make clean && make
```

Expected: `wedge100s-flex-counter-daemon` binary produced with no errors.

Note: This requires `libhiredis-dev` installed on the build host. If not available, skip this step — the .deb build environment (sonic-slave container) has it. Verify with: `dpkg -l libhiredis-dev 2>/dev/null || echo "hiredis-dev not on host; will build in sonic-slave"`

- [ ] **Step 4: Commit**

```bash
git add wedge100s-32x/flex-counter-daemon/daemon.c
git add wedge100s-32x/flex-counter-daemon/Makefile
git commit -m "feat(flex-counter-daemon): add daemon main loop with Redis I/O

Polls bcmcmd every 3s, detects flex ports by COUNTERS_DB key count (<=2),
resolves OID->port via CONFIG_DB lanes + BCM config + ps map,
writes all 66 SAI stat fields per flex port via HMSET."
```

---

### Task 4: Update debian build and packaging

Wire the daemon into the .deb build, install, and postinst.

**Files:**
- Modify: `debian/rules`
- Modify: `debian/sonic-platform-accton-wedge100s-32x.install`
- Modify: `debian/sonic-platform-accton-wedge100s-32x.postinst`

- [ ] **Step 1: Add daemon build block to debian/rules override_dh_auto_build**

After the existing `sai-stat-shim` build block (the `if [ -d ... sai-stat-shim ]` block around line 85-88), add:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon ]; then \
			$(MAKE) $(MAKE_FLAGS) -C $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon; \
			echo "Built wedge100s-flex-counter-daemon for $$mod"; \
		fi; \
```

- [ ] **Step 2: Add daemon clean block to debian/rules override_dh_auto_clean**

After the existing `sai-stat-shim` clean block (around line 49-51), add:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon ]; then \
			$(MAKE) -C $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon clean; \
		fi; \
```

- [ ] **Step 3: Add daemon install block to debian/rules override_dh_auto_install**

After the existing `sai-stat-shim` install block (the `if [ -d ... sai-stat-shim ]` block around line 127-132), add:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon ] && \
		   [ -f $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon/wedge100s-flex-counter-daemon ]; then \
			cp $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon/wedge100s-flex-counter-daemon \
			   debian/$(PACKAGE_PRE_NAME)-$${mod}/usr/bin/; \
		fi; \
```

- [ ] **Step 4: Add supervisor config to postinst**

In `debian/sonic-platform-accton-wedge100s-32x.postinst`, after the existing syncd.sh LD_PRELOAD patch block (ends around line 554), add the daemon supervisor config injection:

```bash
# Install flex-counter-daemon supervisor config inside syncd container.
# The daemon runs inside syncd (needs bcmcmd socket + Redis access).
# supervisor is the process manager inside all SONiC Docker containers.
if command -v docker >/dev/null 2>&1; then
    SYNCD_STATUS=$(docker inspect --format='{{.State.Status}}' syncd 2>/dev/null || true)
    if [ "$SYNCD_STATUS" = "running" ]; then
        docker exec syncd sh -c 'cat > /etc/supervisor/conf.d/flex-counter-daemon.conf' <<'SUPEOF'
[program:flex-counter-daemon]
command=/usr/bin/wedge100s-flex-counter-daemon
priority=100
autostart=true
autorestart=true
startsecs=10
startretries=3
stdout_logfile=syslog
stderr_logfile=syslog
environment=WEDGE100S_BCM_CONFIG="/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm"
SUPEOF
        docker exec syncd supervisorctl reread >/dev/null 2>&1 || true
        docker exec syncd supervisorctl update >/dev/null 2>&1 || true
        echo "wedge100s postinst: installed flex-counter-daemon supervisor config in syncd"
    else
        echo "wedge100s postinst: NOTE: syncd not running — flex-counter-daemon config will be installed on next restart"
    fi
fi
```

- [ ] **Step 5: Copy daemon binary into syncd container in postinst**

Add immediately after the supervisor config block above:

```bash
# Copy the flex-counter-daemon binary into the running syncd container.
# The binary is installed to the host at /usr/bin/ by the .deb;
# syncd's filesystem is isolated, so we docker cp it in.
if command -v docker >/dev/null 2>&1; then
    SYNCD_STATUS=$(docker inspect --format='{{.State.Status}}' syncd 2>/dev/null || true)
    if [ "$SYNCD_STATUS" = "running" ]; then
        if [ -f /usr/bin/wedge100s-flex-counter-daemon ]; then
            docker cp /usr/bin/wedge100s-flex-counter-daemon syncd:/usr/bin/ 2>/dev/null || true
            docker exec syncd supervisorctl start flex-counter-daemon 2>/dev/null || true
            echo "wedge100s postinst: copied and started flex-counter-daemon in syncd"
        fi
    fi
fi
```

- [ ] **Step 6: Commit**

```bash
git add debian/rules
git add debian/sonic-platform-accton-wedge100s-32x.postinst
git commit -m "feat(packaging): wire flex-counter-daemon into deb build and postinst

- debian/rules: build, clean, and install the daemon binary
- postinst: inject supervisor config + copy binary into syncd container"
```

---

### Task 5: Build the .deb and deploy to target

End-to-end verification: build the .deb in the sonic-slave container, deploy to the target switch, verify the daemon starts and existing tests pass.

**Files:**
- None created/modified — build and deploy only

- [ ] **Step 1: Build the .deb**

```bash
cd /export/sonic/sonic-buildimage.claude
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Expected: Build succeeds, producing `target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`.

- [ ] **Step 2: Deploy to target**

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 'sudo systemctl stop pmon && sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && sudo systemctl start pmon'
```

- [ ] **Step 3: Verify daemon is running in syncd**

```bash
ssh admin@192.168.88.12 'sudo docker exec syncd supervisorctl status flex-counter-daemon'
```

Expected: `flex-counter-daemon    RUNNING   pid XXXXX, uptime X:XX:XX`

- [ ] **Step 4: Verify port_state_change notifications work**

```bash
ssh admin@192.168.88.12 'show interfaces status 2>&1 | head -20'
```

Expected: Ports show `up` in the oper column (not all `down`). This is the primary regression the redesign fixes — the old monolithic shim broke port_state_change notifications.

- [ ] **Step 5: Verify daemon syslog output**

```bash
ssh admin@192.168.88.12 'sudo grep flex-counter-daemon /var/log/syslog | tail -10'
```

Expected: Log lines showing `bcmcmd connected`, `ps map refreshed`, `Redis connected`.

- [ ] **Step 6: Run existing test stages**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_24_counters/ -v
python3 -m pytest stage_25_shim/ -v
```

Expected: All tests pass. The observable behavior is identical — flex ports have 66+ keys with real counter values in COUNTERS_DB.

- [ ] **Step 7: Commit (if any fixups needed)**

If build or deployment required any adjustments, commit them now.

---

### Task 6: Verify DPB and daemon recovery

Validate dynamic port breakout and daemon resilience.

**Files:**
- None created/modified — validation only

- [ ] **Step 1: Verify DPB round-trip**

The existing `test_shim_breakout_transition` test in stage_25_shim covers this, but verify manually:

```bash
ssh admin@192.168.88.12 "sudo config interface breakout Ethernet0 '1x100G[40G]' -y -f -l"
sleep 10
ssh admin@192.168.88.12 "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0"
ssh admin@192.168.88.12 "sudo config interface breakout Ethernet0 '4x25G[10G]' -y -f -l"
sleep 10
ssh admin@192.168.88.12 "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0"
```

Expected: Ethernet0 disappears and reappears in COUNTERS_PORT_NAME_MAP across DPB. After restore, flex ports get 66+ stat keys within one daemon poll cycle (3s).

- [ ] **Step 2: Verify daemon survives syncd restart**

```bash
ssh admin@192.168.88.12 'sudo systemctl restart syncd'
sleep 30
ssh admin@192.168.88.12 'sudo docker exec syncd supervisorctl status flex-counter-daemon'
```

Expected: `flex-counter-daemon    RUNNING` — supervisor's `autorestart=true` brings it back.

- [ ] **Step 3: Final test suite run**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_24_counters/ stage_25_shim/ -v
```

Expected: All tests pass.
