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
