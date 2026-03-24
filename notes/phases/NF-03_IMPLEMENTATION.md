# NF-03 — Counters: IMPLEMENTATION

## What Was Built

No platform-specific files were created for counters. The counter infrastructure is
entirely provided by the BCM SAI, syncd, and the standard SONiC counterd/flexcounter
subsystem within the swss container.

## Counter Infrastructure Flow

```
syncd (BCM SAI)
  └─ initializes SAI_OBJECT_TYPE_PORT for each BCM port
  └─ writes OIDs to COUNTERS_PORT_NAME_MAP (COUNTERS_DB key)

counterd (inside swss container)
  └─ reads COUNTERS_PORT_NAME_MAP
  └─ writes FLEX_COUNTER_TABLE:PORT_STAT:oid:... to DB5
  └─ polls SAI for SAI_PORT_STAT_* values at PORT_STAT interval
  └─ writes COUNTERS:oid:... to COUNTERS_DB (DB2)

show interfaces counters
  └─ reads COUNTERS_DB via sonic-py-swsssdk
```

## Verified Configuration

**Flex counter polling** (verified on hardware 2026-03-02):
```
PORT_STAT         1000ms    enable
QUEUE_STAT        10000ms   enable
PORT_BUFFER_DROP  60000ms   enable
RIF_STAT          1000ms    enable
QUEUE_WATERMARK   60000ms   enable
PG_WATERMARK      60000ms   enable
PG_DROP           10000ms   enable
BUFFER_POOL_WM    60000ms   enable
ACL               10000ms   enable
```

**SAI_PORT_STAT_* fields present per port** (verified on hardware 2026-03-02):
- `SAI_PORT_STAT_IF_IN_OCTETS`
- `SAI_PORT_STAT_IF_IN_UCAST_PKTS`
- `SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS`
- `SAI_PORT_STAT_IF_IN_DISCARDS`
- `SAI_PORT_STAT_IF_IN_ERRORS`
- `SAI_PORT_STAT_IF_IN_BROADCAST_PKTS`
- `SAI_PORT_STAT_IF_IN_MULTICAST_PKTS`
- `SAI_PORT_STAT_IF_OUT_OCTETS`
- `SAI_PORT_STAT_IF_OUT_UCAST_PKTS`
- `SAI_PORT_STAT_IF_OUT_DISCARDS`
- `SAI_PORT_STAT_IF_OUT_ERRORS`
- `SAI_PORT_STAT_IN_DROPPED_PKTS`
- `SAI_PORT_STAT_OUT_DROPPED_PKTS`

**show interfaces counters column set** (verified on hardware 2026-03-02):
`IFACE STATE RX_OK RX_BPS RX_UTIL RX_ERR RX_DRP RX_OVR TX_OK TX_BPS TX_UTIL TX_ERR TX_DRP TX_OVR`

**STATE column codes:**
- `U` = Up (link up, oper=up)
- `D` = Down (admin up, link down)
- `X` = Disabled (admin down)

## Key Decisions

No platform customization was needed or applied. The standard SONiC BCM counter
infrastructure worked without changes after the BCM config was correct and syncd
initialized all 32 ports.

The only prerequisite was fixing the swss restart loop (see NF-08 implementation)
which prevented syncd from running stably long enough for COUNTERS_DB to populate.

## Hardware-Verified Facts

- verified on hardware 2026-03-02: COUNTERS_PORT_NAME_MAP has OIDs for all 32 ports
- verified on hardware 2026-03-02: all SAI_PORT_STAT_* fields present for Ethernet0
- verified on hardware 2026-03-02: RX_OK increments with LLDP traffic (~7000 pkts over 5 minutes) on Ethernet16/32/48/112
- verified on hardware 2026-03-02: `sonic-clear counters` resets to near-zero (< 100 pkts = LLDP since clear)
- verified on hardware 2026-03-02: 39/39 tests pass across stages 07, 11, 12, 13

## Remaining Known Gaps

- **Queue-level counters**: Not tested. Queue OIDs are present in COUNTERS_QUEUE_NAME_MAP
  but per-queue RX/TX counters have not been validated.
- **No traffic test with non-LLDP traffic**: RX_OK increment was verified only with LLDP
  frames. High-rate unicast traffic not tested.
- **BUFFER_POOL_WM at 60s interval**: Watermark counters have a very long poll interval.
  Not verified whether watermark values ever become non-zero.
