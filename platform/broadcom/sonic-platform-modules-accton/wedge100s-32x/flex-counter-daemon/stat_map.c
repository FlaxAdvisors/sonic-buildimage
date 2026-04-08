/**
 * @file stat_map.c
 * @brief SAI port stat to bcmcmd counter name mapping table.
 *
 * Defines g_stat_map[] mapping sai_port_stat_t values to BCM 'show c all'
 * counter names.  Compound stats (e.g. IF_IN_NON_UCAST_PKTS) use name2
 * to specify a second counter to add.  Also provides field name strings
 * for COUNTERS_DB HSET operations.
 *
 * Moved from sai-stat-shim to flex-counter-daemon.
 */
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

/* SAI stat field name table — maps stat enum value -> COUNTERS_DB hash field name.
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
