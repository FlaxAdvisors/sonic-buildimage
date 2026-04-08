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
