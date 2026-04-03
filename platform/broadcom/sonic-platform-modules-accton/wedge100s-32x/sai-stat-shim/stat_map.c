/* stat_map.c — SAI port stat → bcm_stat_val_t mapping.
 * Empirically derived 2026-03-28 on BCM56960 / libsaibcm 14.3.0.0.0.0.3.0.
 * Migrated from string-based (bcmcmd counter names) to integer-based
 * (bcm_stat_val_t enum values) on 2026-04-03 for direct bcm_stat_multi_get.
 * Hardware: Accton Wedge100S-32X (SONiC hare-lorax, kernel 6.1.0-29-2-amd64). */
#include "shim.h"

#define S(id) ((sai_port_stat_t)(id))

const stat_map_entry_t g_stat_map[] = {
    /* Standard IF counters — HC (64-bit) variants used where available */
    { S(0),  snmpIfHCInOctets,          -1                       },  /* IF_IN_OCTETS           */
    { S(1),  snmpIfHCInUcastPkts,       -1                       },  /* IF_IN_UCAST_PKTS       */
    { S(2),  snmpIfHCInMulticastPkts,   snmpIfHCInBroadcastPkts  },  /* IF_IN_NON_UCAST_PKTS   */
    { S(3),  snmpIfInDiscards,          -1                       },  /* IF_IN_DISCARDS          */
    { S(4),  snmpIfInErrors,            -1                       },  /* IF_IN_ERRORS            */
    { S(5),  -1,                        -1                       },  /* IF_IN_UNKNOWN_PROTOS    */
    { S(6),  snmpIfHCInBroadcastPkts,   -1                       },  /* IF_IN_BROADCAST_PKTS    */
    { S(7),  snmpIfHCInMulticastPkts,   -1                       },  /* IF_IN_MULTICAST_PKTS    */

    { S(9),  snmpIfHCOutOctets,         -1                       },  /* IF_OUT_OCTETS           */
    { S(10), snmpIfHCOutUcastPkts,      -1                       },  /* IF_OUT_UCAST_PKTS       */
    { S(11), snmpIfHCOutMulticastPkts,  snmpIfHCOutBroadcastPckts},  /* IF_OUT_NON_UCAST_PKTS   */
    { S(12), snmpIfOutDiscards,         -1                       },  /* IF_OUT_DISCARDS          */
    { S(13), snmpIfOutErrors,           -1                       },  /* IF_OUT_ERRORS            */
    { S(14), -1,                        -1                       },  /* IF_OUT_QLEN              */
    { S(15), snmpIfHCOutBroadcastPckts, -1                       },  /* IF_OUT_BROADCAST_PKTS    */
    { S(16), snmpIfHCOutMulticastPkts,  -1                       },  /* IF_OUT_MULTICAST_PKTS    */

    /* Ethernet statistics */
    { S(20), snmpEtherStatsUndersizePkts, -1                     },  /* ETHER_STATS_UNDERSIZE    */
    { S(21), snmpEtherStatsFragments,     -1                     },  /* ETHER_STATS_FRAGMENTS    */
    { S(33), snmpEtherRxOversizePkts,     -1                     },  /* ETHER_RX_OVERSIZE        */
    { S(34), snmpEtherTxOversizePkts,     -1                     },  /* ETHER_TX_OVERSIZE        */
    { S(35), snmpEtherStatsJabbers,       -1                     },  /* ETHER_STATS_JABBERS      */
    { S(40), snmpEtherStatsTXNoErrors,    -1                     },  /* ETHER_STATS_TX_NO_ERRORS */

    /* IP counters — no BCM equivalent */
    { S(42), -1,                        -1                       },  /* IP_IN_RECEIVES           */
    { S(44), -1,                        -1                       },  /* IP_IN_UCAST_PKTS         */

    /* RX frame size buckets */
    { S(71), snmpEtherStatsPkts64Octets,        -1               },  /* IN_PKTS_64              */
    { S(72), snmpEtherStatsPkts65to127Octets,   -1               },  /* IN_PKTS_65_TO_127       */
    { S(73), snmpEtherStatsPkts128to255Octets,  -1               },  /* IN_PKTS_128_TO_255      */
    { S(74), snmpEtherStatsPkts256to511Octets,  -1               },  /* IN_PKTS_256_TO_511      */
    { S(75), snmpEtherStatsPkts512to1023Octets, -1               },  /* IN_PKTS_512_TO_1023     */
    { S(76), snmpEtherStatsPkts1024to1518Octets,-1               },  /* IN_PKTS_1024_TO_1518    */
    { S(77), -1,                        -1                       },  /* IN_PKTS_1519_TO_2047    */
    { S(78), -1,                        -1                       },  /* IN_PKTS_2048_TO_4095    */
    { S(79), -1,                        -1                       },  /* IN_PKTS_4096_TO_9216    */
    { S(80), -1,                        -1                       },  /* IN_PKTS_9217_TO_16383   */

    /* TX frame size buckets — the snmpEtherStatsPkts* enum values (17-22) are
     * aggregate RX+TX counters.  The BCM SDK has separate TX-only counters at
     * higher enum values (snmpEtherStatsTXPkts64Octets etc.) but their exact
     * values vary by SDK version.  Return 0 for now; add when verified. */
    { S(81), -1,                                -1               },  /* OUT_PKTS_64             */
    { S(82), -1,                                -1               },  /* OUT_PKTS_65_TO_127      */
    { S(83), -1,                                -1               },  /* OUT_PKTS_128_TO_255     */
    { S(84), -1,                                -1               },  /* OUT_PKTS_256_TO_511     */
    { S(85), -1,                        -1                       },  /* OUT_PKTS_512_TO_1023    */
    { S(86), -1,                        -1                       },  /* OUT_PKTS_1024_TO_1518   */
    { S(87), -1,                        -1                       },  /* OUT_PKTS_1519_TO_2047   */
    { S(88), -1,                        -1                       },  /* OUT_PKTS_2048_TO_4095   */
    { S(89), -1,                        -1                       },  /* OUT_PKTS_4096_TO_9216   */
    { S(90), -1,                        -1                       },  /* OUT_PKTS_9217_TO_16383  */

    /* IN_DROPPED / OUT_DROPPED */
    { S(99),  snmpIfInDiscards,         -1                       },  /* IN_DROPPED_PKTS         */
    { S(100), snmpIfOutDiscards,        -1                       },  /* OUT_DROPPED_PKTS        */

    /* Pause/PFC — these use bcm_stat_val_t values not in our enum stubs.
     * PFC counters are typically at enum values 150+ (snmpBcmRxPFCControlFrame
     * etc).  For now, return 0 — PFC counters on flex sub-ports are not
     * critical and can be added later if needed. */
    { S(101), -1,                       -1                       },  /* PAUSE_RX_PKTS           */
    { S(102), -1,                       -1                       },  /* PAUSE_TX_PKTS           */
    { S(103), -1,                       -1                       },  /* PFC_0_RX_PKTS           */
    { S(104), -1,                       -1                       },  /* PFC_0_TX_PKTS           */
    { S(105), -1,                       -1                       },  /* PFC_1_RX_PKTS           */
    { S(106), -1,                       -1                       },  /* PFC_1_TX_PKTS           */
    { S(107), -1,                       -1                       },  /* PFC_2_RX_PKTS           */
    { S(108), -1,                       -1                       },  /* PFC_2_TX_PKTS           */
    { S(109), -1,                       -1                       },  /* PFC_3_RX_PKTS           */
    { S(110), -1,                       -1                       },  /* PFC_3_TX_PKTS           */
    { S(111), -1,                       -1                       },  /* PFC_4_RX_PKTS           */
    { S(112), -1,                       -1                       },  /* PFC_4_TX_PKTS           */
    { S(113), -1,                       -1                       },  /* PFC_5_RX_PKTS           */
    { S(114), -1,                       -1                       },  /* PFC_5_TX_PKTS           */
    { S(115), -1,                       -1                       },  /* PFC_6_RX_PKTS           */
    { S(116), -1,                       -1                       },  /* PFC_6_TX_PKTS           */
    { S(117), -1,                       -1                       },  /* PFC_7_RX_PKTS           */
    { S(118), -1,                       -1                       },  /* PFC_7_TX_PKTS           */

    /* FEC counters — no BCM equivalent; return 0 */
    { S(178), -1,                       -1                       },  /* FEC_CORRECTABLE         */
    { S(179), -1,                       -1                       },  /* FEC_NOT_CORRECTABLE     */
    { S(180), -1,                       -1                       },  /* FEC_SYMBOL_ERRORS       */
    { S(202), -1,                       -1                       },  /* FEC_CORRECTED_BITS      */
};
#undef S

const int g_stat_map_size = (int)(sizeof(g_stat_map) / sizeof(g_stat_map[0]));

/* Linear search — called once per OID×stat_id pair. 68 entries: fast enough. */
int stat_map_index(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_stat_map_size; i++)
        if (g_stat_map[i].stat_id == stat_id)
            return i;
    return -1;
}
