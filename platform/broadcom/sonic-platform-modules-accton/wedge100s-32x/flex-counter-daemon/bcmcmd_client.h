/**
 * @file bcmcmd_client.h
 * @brief BCM diagnostic shell Unix socket client.
 *
 * Standalone header for flex-counter-daemon (no shim.h dependency).
 * Provides functions to connect to the sswsyncd BCM diag shell socket,
 * issue 'ps' and 'show c all' commands, and parse results into a cache.
 */
#pragma once

#include <stdint.h>
#include <time.h>

#define BCMCMD_MAX_PORTS     256
#define BCMCMD_PORT_NAME_LEN 16
#define BCMCMD_MAX_STAT_IDS  80

/* Socket paths: host vs container. */
#define BCMCMD_SOCKET_HOST   "/var/run/docker-syncd/sswsyncd.socket"
#define BCMCMD_SOCKET_CONTAINER "/var/run/sswsyncd/sswsyncd.socket"

/* Buffer for 'show c all' — returns ~1.35 MB for 128 ports. */
#define COUNTER_BUF_SIZE    2097152

/** One row in the port counter cache: port_name + value per stat_map index. */
typedef struct {
    char     port_name[BCMCMD_PORT_NAME_LEN]; /**< BCM port name (e.g. "xe0") */
    int      sdk_port;                         /**< BCM SDK port number */
    uint64_t val[BCMCMD_MAX_STAT_IDS];        /**< Counter values indexed by stat_map */
    int      n_raw;                            /**< Number of entries in raw[] */
    struct { char name[24]; uint64_t value; } raw[64]; /**< Raw BCM counter name/value pairs */
} port_row_t;

/** Per-cycle counter cache holding all port rows and a fetch timestamp. */
typedef struct {
    port_row_t      rows[BCMCMD_MAX_PORTS]; /**< Per-port counter rows */
    int             n_rows;                  /**< Number of populated rows */
    struct timespec fetched_at;              /**< CLOCK_MONOTONIC time of last fetch */
} counter_cache_t;

/**
 * @brief Connect to the bcmcmd Unix domain socket.
 *
 * Opens a non-blocking connect with a configurable timeout, then reads
 * the initial banner prompt.
 *
 * @param path       Path to the Unix domain socket.
 * @param timeout_ms Connection timeout in milliseconds.
 * @return Socket file descriptor on success, -1 on error.
 */
int  bcmcmd_connect(const char *path, int timeout_ms);

/**
 * @brief Close the bcmcmd socket file descriptor.
 *
 * @param fd File descriptor returned by bcmcmd_connect(), or -1 (no-op).
 */
void bcmcmd_close(int fd);

/**
 * @brief Run 'ps' and parse SDK port number / name pairs.
 *
 * Parses output lines of the form "NAME(SDK_PORT) ..." into sdk_ports[]
 * and port_names[].
 *
 * @param fd         Open bcmcmd socket.
 * @param sdk_ports  Output array for SDK port numbers.
 * @param port_names Output array for BCM port name strings.
 * @param max        Maximum number of entries to fill.
 * @return Number of ports parsed, or -1 on socket error.
 */
int  bcmcmd_ps(int fd, int *sdk_ports,
               char port_names[][BCMCMD_PORT_NAME_LEN], int max);

/**
 * @brief Clear counter values and raw entries in all cache rows.
 *
 * Must be called before each poll cycle to prevent accumulation.
 *
 * @param cache Pointer to the counter cache to clear.
 */
void bcmcmd_cache_clear(counter_cache_t *cache);

/**
 * @brief Fetch all port counters with 'show c all' and populate the cache.
 *
 * Issues a bulk 'show c all' command (~1.35 MB output for 128 ports),
 * parses all counter lines, and resolves compound stats via name2.
 *
 * @param fd    Open bcmcmd socket.
 * @param cache Counter cache to populate.
 * @return 0 on success, -1 on socket or parse error.
 */
int  bcmcmd_fetch_counters(int fd, counter_cache_t *cache);

/**
 * @brief Fetch counters for specific ports with per-port 'show c all <port>'.
 *
 * Issues one 'show c all <port>' command per entry in ports[].  Faster
 * than bcmcmd_fetch_counters() for small port sets (~17 ms per port).
 *
 * @param fd      Open bcmcmd socket.
 * @param cache   Counter cache to populate.
 * @param ports   Array of BCM port name strings to query.
 * @param n_ports Number of ports in the ports[] array.
 * @return 0 on success, -1 on socket or parse error.
 */
int  bcmcmd_fetch_port_counters(int fd, counter_cache_t *cache,
                                const char ports[][BCMCMD_PORT_NAME_LEN],
                                int n_ports);
