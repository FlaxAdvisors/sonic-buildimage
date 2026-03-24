# NF-03 — Counters: PLAN

## Problem Statement

SONiC's `counterd` (flex counter daemon inside swss) polls the BCM SAI for per-port
packet and byte statistics and writes them to COUNTERS_DB (DB 2). The `show interfaces
counters` CLI reads COUNTERS_DB. Without working counters, operators cannot see traffic
rates, errors, or drops, making troubleshooting impossible.

For the Wedge 100S-32X, counters are entirely BCM SAI functionality — there is no
platform-specific code required. The work is validating that:
1. The flex counter infrastructure (FLEX_COUNTER_TABLE) is configured and enabled
2. COUNTERS_PORT_NAME_MAP has OIDs for all 32 ports
3. COUNTERS_DB is populated with `SAI_PORT_STAT_*` entries
4. Counters increment with live traffic

## Proposed Approach

No platform code changes are needed. Validation only:
1. After syncd initializes ports, verify COUNTERS_PORT_NAME_MAP is populated.
2. Verify `counterpoll show` reports PORT_STAT enabled at a reasonable interval.
3. Verify individual `COUNTERS:oid:...` keys have `SAI_PORT_STAT_*` fields.
4. With an active link (RS-FEC configured), verify RX_OK increments (LLDP traffic).
5. Verify `sonic-clear counters` resets displayed values.

## Files to Change

None. Counter infrastructure is standard SONiC BCM SAI — no platform customization.

## Acceptance Criteria

- `COUNTERS_PORT_NAME_MAP` has >= 32 Ethernet entries
- `PORT_STAT` is `enable` in `counterpoll show` with interval <= 60000ms
- `COUNTERS:oid:...` for Ethernet0 contains at minimum:
  `SAI_PORT_STAT_IF_IN_OCTETS`, `SAI_PORT_STAT_IF_OUT_OCTETS`,
  `SAI_PORT_STAT_IF_IN_ERRORS`, `SAI_PORT_STAT_IF_OUT_ERRORS`
- `show interfaces counters` shows >= 32 port rows with all expected columns
- Link-up ports show STATE=U and RX_OK > 0 (LLDP traffic)
- `sonic-clear counters` resets RX_OK to near-zero

## Risks and Watch-Outs

- **Counters require syncd to be up**: If syncd is restarting (swss restart loop), COUNTERS_DB
  will be empty. The swss restart loop caused by teamd masking must be fixed (see NF-08).
- **RX_OK = 0 on link-up port**: If a port is oper=up but counters stay at 0, the flex counter
  polling may not be running. Check: `redis-cli -n 5 keys 'FLEX_COUNTER_TABLE:PORT_STAT:*'`
- **PORT_STAT interval**: Default 1000ms. If changed to > 60000ms, counters will appear stale.
- **No platform-specific counter OIDs**: Some platforms add custom counter groups (e.g.,
  per-queue). The Wedge 100S-32X uses standard SAI_PORT_STAT_* only.
