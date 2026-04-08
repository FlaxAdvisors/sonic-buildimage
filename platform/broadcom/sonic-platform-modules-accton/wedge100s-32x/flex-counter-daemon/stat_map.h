/**
 * @file stat_map.h
 * @brief SAI port stat to bcmcmd counter name mapping.
 *
 * Maps sai_port_stat_t enum values to BCM 'show c all' counter names.
 * Each entry has an optional name2 for compound stats (e.g. NON_UCAST
 * = RMCA + RBCA) where two BCM counters must be summed.
 */
#pragma once

#include <stdint.h>

typedef uint32_t sai_port_stat_t;

/** Mapping entry from one SAI stat to one or two BCM counter names. */
typedef struct {
    sai_port_stat_t  stat_id; /**< SAI port stat enum value */
    const char      *name1;   /**< Primary BCM counter name (NULL if not mapped) */
    const char      *name2;   /**< Secondary BCM counter name for compound stats (NULL if N/A) */
} stat_map_entry_t;

extern const stat_map_entry_t g_stat_map[];
extern const int              g_stat_map_size;

/**
 * @brief Look up the COUNTERS_DB field name for a SAI stat.
 *
 * Returns the string used as the Redis HSET field when writing to
 * COUNTERS:<oid> (e.g. "SAI_PORT_STAT_IF_IN_OCTETS").
 *
 * @param stat_id SAI port stat enum value.
 * @return Field name string, or NULL if stat_id is not in the table.
 */
const char *sai_stat_field_name(sai_port_stat_t stat_id);

/**
 * @brief Return the index of stat_id in g_stat_map[].
 *
 * @param stat_id SAI port stat enum value.
 * @return Array index on success, -1 if not found.
 */
int stat_map_index(sai_port_stat_t stat_id);
