/* stat_map.c — SAI port stat → bcmcmd counter name mapping.
 * Empirically derived 2026-03-28 on BCM56960 / libsaibcm 14.3.0.0.0.0.3.0.
 * Hardware: Accton Wedge100S-32X (SONiC hare-lorax, kernel 6.1.0-29-2-amd64).
 * SAI enum values verified from src/sonic-sairedis/SAI/inc/saiport.h.
 * Cross-reference: Ethernet16 COUNTERS_DB vs bcmcmd 'show counters ce0'. */
#include "shim.h"

/* SAI stat enum integer values from sai_port_stat_t in saiport.h.
 * Formula: enum value = sequential position (0-based) in the enum,
 * since SAI_PORT_STAT_START = SAI_PORT_STAT_IF_IN_OCTETS = 0. */
#define S(id) ((sai_port_stat_t)(id))

const stat_map_entry_t g_stat_map[] = {
    /* Standard IF counters — empirically verified against ce0 */
    { S(0),  "RBYT",  NULL   },  /* SAI_PORT_STAT_IF_IN_OCTETS           */
    { S(1),  "RUCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_UCAST_PKTS       */
    { S(2),  "RMCA",  "RBCA" },  /* SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS  RMCA+RBCA */
    { S(3),  "RIDR",  NULL   },  /* SAI_PORT_STAT_IF_IN_DISCARDS         */
    { S(4),  "RFCS",  NULL   },  /* SAI_PORT_STAT_IF_IN_ERRORS           */
    { S(5),  NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_UNKNOWN_PROTOS — no bcmcmd equiv */
    { S(6),  "RBCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_BROADCAST_PKTS   */
    { S(7),  "RMCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_MULTICAST_PKTS   */

    { S(9),  "TBYT",  NULL   },  /* SAI_PORT_STAT_IF_OUT_OCTETS          */
    { S(10), "TUCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_UCAST_PKTS      */
    { S(11), "TMCA",  "TBCA" },  /* SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS TMCA+TBCA */
    { S(12), "TDRP",  NULL   },  /* SAI_PORT_STAT_IF_OUT_DISCARDS        */
    { S(13), "TERR",  NULL   },  /* SAI_PORT_STAT_IF_OUT_ERRORS          */
    { S(14), NULL,    NULL   },  /* SAI_PORT_STAT_IF_OUT_QLEN — queue length, not a counter */
    { S(15), "TBCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_BROADCAST_PKTS  */
    { S(16), "TMCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_MULTICAST_PKTS  */

    /* Ethernet statistics */
    { S(20), "RUND",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_UNDERSIZE_PKTS */
    { S(21), "RFRG",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_FRAGMENTS      */
    { S(33), "ROVR",  NULL   },  /* SAI_PORT_STAT_ETHER_RX_OVERSIZE_PKTS     */
    { S(34), "TOVR",  NULL   },  /* SAI_PORT_STAT_ETHER_TX_OVERSIZE_PKTS     */
    { S(35), "RJBR",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_JABBERS        */
    { S(40), "TPOK",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_TX_NO_ERRORS   */

    /* IP counters — no bcmcmd equivalent in show counters */
    { S(42), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_RECEIVES         */
    { S(44), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_UCAST_PKTS       */

    /* RX frame size buckets */
    { S(71), "R64",   NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_64_OCTETS              */
    { S(72), "R127",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_65_TO_127_OCTETS       */
    { S(73), "R255",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_128_TO_255_OCTETS      */
    { S(74), "R511",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_256_TO_511_OCTETS      */
    { S(75), "R1023", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_512_TO_1023_OCTETS     */
    { S(76), "R1518", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1024_TO_1518_OCTETS    */
    { S(77), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1519_TO_2047_OCTETS — not in show counters */
    { S(78), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_2048_TO_4095_OCTETS    */
    { S(79), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_4096_TO_9216_OCTETS    */
    { S(80), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_9217_TO_16383_OCTETS   */

    /* TX frame size buckets */
    { S(81), "T64",   NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_64_OCTETS             */
    { S(82), "T127",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_65_TO_127_OCTETS      */
    { S(83), "T255",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_128_TO_255_OCTETS     */
    { S(84), "T511",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_256_TO_511_OCTETS     */
    { S(85), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_512_TO_1023_OCTETS    */
    { S(86), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1024_TO_1518_OCTETS   */
    { S(87), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1519_TO_2047_OCTETS   */
    { S(88), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_2048_TO_4095_OCTETS   */
    { S(89), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_4096_TO_9216_OCTETS   */
    { S(90), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_9217_TO_16383_OCTETS  */

    /* IN_DROPPED / OUT_DROPPED */
    { S(99),  "RIDR",  NULL   },  /* SAI_PORT_STAT_IN_DROPPED_PKTS  */
    { S(100), "TDRP",  NULL   },  /* SAI_PORT_STAT_OUT_DROPPED_PKTS */

    /* Pause/PFC */
    { S(101), "RXCF",  NULL   },  /* SAI_PORT_STAT_PAUSE_RX_PKTS  */
    { S(102), "TXPF",  NULL   },  /* SAI_PORT_STAT_PAUSE_TX_PKTS  */
    { S(103), "RPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_RX_PKTS  */
    { S(104), "TPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_TX_PKTS  */
    { S(105), "RPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_RX_PKTS  */
    { S(106), "TPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_TX_PKTS  */
    { S(107), "RPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_RX_PKTS  */
    { S(108), "TPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_TX_PKTS  */
    { S(109), "RPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_RX_PKTS  */
    { S(110), "TPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_TX_PKTS  */
    { S(111), "RPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_RX_PKTS  */
    { S(112), "TPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_TX_PKTS  */
    { S(113), "RPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_RX_PKTS  */
    { S(114), "TPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_TX_PKTS  */
    { S(115), "RPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_RX_PKTS  */
    { S(116), "TPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_TX_PKTS  */
    { S(117), "RPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_RX_PKTS  */
    { S(118), "TPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_TX_PKTS  */

    /* FEC counters — no equivalent in bcmcmd show counters; return 0 */
    { S(178), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES    */
    { S(179), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_NOT_CORRECTABLE_FRAMES */
    { S(180), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_SYMBOL_ERRORS         */
    { S(202), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTED_BITS        */
};
#undef S

const int g_stat_map_size = (int)(sizeof(g_stat_map) / sizeof(g_stat_map[0]));

/* Linear search — called once per OID×stat_id pair, result is not cached
 * here (shim.c caches the resolved values per port). 68 entries: fast enough. */
int stat_map_index(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_stat_map_size; i++)
        if (g_stat_map[i].stat_id == stat_id)
            return i;
    return -1;
}
