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

## Why Not Replace libsai.so Entirely?

This was considered and rejected on 2026-03-28. The reasoning is preserved here because
it will come up again.

### What libsai.so actually does

`libsai.so.1.0` (506MB monolithic binary) implements the full SAI surface that syncd
depends on. This is not just port stats — it is the entire hardware programming layer:

- Switch init: BCM SDK bootstrap, `.config.bcm` loading, flex port programming, MMU init
- Port create/delete/attributes/stats
- FDB (MAC table management)
- L3: routes, neighbors, nexthops, ECMP groups
- ACLs — thousands of lines of spec alone
- QoS: queues, schedulers, WRED, buffer profiles, priority groups
- LAG, VLANs, tunnels (VXLAN), mirror sessions, BFD, STP, policers
- Hostif: CPU packet I/O, kernel netdev integration (how SONiC receives packets)
- And the stats path that is broken for flex sub-ports

A drop-in replacement for syncd must implement all of this. That is years of work for a
team, not a single-platform effort.

### Why Cumulus/EOS don't help

Cumulus Linux 3.7 worked on Wedge 100S and had correct breakout stats, but `switchd` is
fully closed source. It bypasses SAI entirely and calls `bcm_stat_get()` directly
against the BCM SDK it initialized internally. There is no open-source technique to
borrow — the approach is simply "no SAI layer."

Arista EOS on the peer device uses a completely different NOS architecture (AgentX/Sysdb)
that predates SAI and never had this problem. Same conclusion.

FBOSS (Facebook, also runs on Wedge 100S) similarly calls `bcm_stat_sync_multi_get()`
directly from `BcmPort.cpp`, with BCM logical port IDs obtained at port creation time.
No SAI. This is open source but requires the proprietary BCM SDK to run.

### Why reverse-engineering libsai.so doesn't help

The SAI API surface is already fully public — OCP publishes all headers. That is not the
unknown. The unknown is the implementation detail of how `libsaibcm` maps flex sub-port
OIDs to BCM logical port IDs on BCM56960. Reconstructing that from a 506MB binary is:
(a) legally questionable under Broadcom's SDK license, and
(b) irrelevant — we don't need to know how the broken code works, we need to route
around it for the one narrow failure case.

### Why the shim is the right scope

The bug is narrow: `brcm_sai_get_port_stats()` returns non-SUCCESS for flex `.0`
portmap sub-port OIDs. Everything else — routing, ACLs, QoS, FDB, hostif, port
attributes — works correctly. A full libsai.so replacement fixes a one-function bug
with the maximum possible blast radius.

### Why bcmcmd over OpenNSL userspace library

Direct `bcm_stat_get()` access via the OpenNSL/OpenBCM userspace library was considered
as a cleaner alternative to bcmcmd text parsing. Rejected because:

- `libsai.so` already initialized the BCM SDK. Loading a second userspace SDK instance
  against the same kernel module (saibcm-modules) is undefined behavior — risk of
  double-init corruption.
- bcmcmd goes through the *already-running* SDK instance via its Unix socket — no
  coexistence risk, no second init.

**Known fragility of bcmcmd approach:** If Broadcom changes `show counters` output
format in a future `libsaibcm` upgrade, the parser in `bcmcmd_client.c` breaks. This
is a one-file fix scoped to the parser — acceptable maintenance cost for a permanent
production component on a platform that is unlikely to receive further libsaibcm updates.

## Continuation Instructions

To continue: invoke the `superpowers:writing-plans` skill, reference this file and the
design spec for full context, then generate the implementation plan and begin execution.
