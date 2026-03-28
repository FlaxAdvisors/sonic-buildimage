# SAI Stat Shim — Implementation Continuation Prompt

## Context

Working on Accton Wedge100S-32X SONiC port (branch `wedge100s`).
Design is complete and approved. Ready to implement.

## What Was Decided

Full design spec at: `docs/superpowers/specs/2026-03-27-sai-stat-shim-design.md`

Summary:
- `LD_PRELOAD` library (`libsai-stat-shim.so`) injected into syncd container
- Intercepts `sai_api_query(SAI_API_PORT)`, replaces `get_port_stats` / `get_port_stats_ext` in-place
- Flex detection: dynamic via `SAI_PORT_ATTR_HW_LANE_LIST` + BCM config file parse — no hardcoded ports
- Backend: single `show counters\n` batch to bcmcmd Unix socket per 500ms TTL window
- All 128 ports refreshed in one shot per batch (supports full 32x4 breakout)
- Non-flex ports: pure passthrough to original `brcm_sai_get_port_stats()`
- Startup race: return zeros + SAI_STATUS_SUCCESS until socket ready
- 50ms non-blocking connect timeout
- Static SAI→bcmcmd stat ID map (66 entries), derived empirically before coding
- Ships inside existing `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`
- postinst patches syncd supervisor config with LD_PRELOAD + WEDGE100S_BCM_CONFIG env vars
- Thread safety: mutex on cache reads/writes, fetch-in-progress flag for dedup

## Pre-Implementation Step (Do First)

Before writing any shim code, derive the empirical SAI→bcmcmd stat ID mapping:

```bash
# 1. Pick a working non-breakout port OID
ssh admin@192.168.88.12 "redis-cli -n 2 keys 'COUNTERS:oid:*' | head -5"

# 2. Get its BCM port number from the port table
ssh admin@192.168.88.12 "redis-cli -n 4 hget 'PORT_TABLE|Ethernet8' 'lanes'"

# 3. Get SAI counter names and values
ssh admin@192.168.88.12 "redis-cli -n 2 hgetall COUNTERS:<OID>"

# 4. Get bcmcmd counter names and values for same port
ssh admin@192.168.88.12 "sudo docker exec syncd bcmcmd 'show counters xe<N>'"

# 5. Cross-reference to build the 66-entry mapping table
```

The mapping is the foundation of `stat_map.c` — do this before any other implementation.

## Implementation Steps (In Order)

1. Derive empirical stat ID mapping → write `stat_map.c`
2. Implement `bcmcmd_client.c` (socket connect, send, read-until-prompt, parse)
3. Implement `shim.c` (sai_api_query intercept, port classifier, cache, get_port_stats)
4. Write `Makefile` for the shim
5. Wire into `platform-modules-accton.mk`
6. Write `postinst`/`prerm` patches for syncd supervisor config
7. Write `tests/stage_25_shim/` pytest stage
8. Build .deb, deploy to target, run tests

## Key File Paths

| Resource | Path |
|---|---|
| Shim source (to create) | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/` |
| Platform .mk | `platform/broadcom/platform-modules-accton.mk` |
| postinst (existing) | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/scripts/postinst` |
| BCM config | `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` |
| Design spec | `docs/superpowers/specs/2026-03-27-sai-stat-shim-design.md` |
| Test suite | `tests/` |

## bcmcmd Socket Path

Verify the exact socket path before implementing bcmcmd_client.c:
```bash
ssh admin@192.168.88.12 "sudo docker exec syncd find /var/run /tmp -name '*.sock' -o -name 'sai*' 2>/dev/null | head -20"
ssh admin@192.168.88.12 "sudo docker exec syncd ls /var/run/sswsyncd/ 2>/dev/null"
```

## Continuation Instructions

To continue: invoke the `superpowers:writing-plans` skill, reference this file and the
design spec for full context, then generate the implementation plan and begin execution.
