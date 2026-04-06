# Flex Counter Daemon — Design Spec

**Date:** 2026-04-03
**Platform:** Accton Wedge100S-32X (BCM56960 / Tomahawk)
**Branch:** `wedge100s`
**Replaces:** sai-stat-shim LD_PRELOAD-only architecture (2026-03-27 design)

---

## Problem Statement

12 flex sub-ports (Ethernet0-3, Ethernet64-67, Ethernet80-83) — and potentially all
breakout ports in any configuration — populate only 2 keys in COUNTERS_DB instead of
the expected 66. `libsaibcm 14.3.0.0.0.0.3.0` `brcm_sai_get_port_stats()` returns
non-SUCCESS for BCM logical ports in flex `.0` portmap entries.

The previous LD_PRELOAD shim intercepted `sai_api_query(SAI_API_PORT)` and replaced
the `get_port_stats` function pointer to inject bcmcmd-sourced counters inline. This
approach — whether via `mprotect` in-place patching or struct-copy replacement —
**breaks `port_state_change` notifications**, leaving all ports oper-down. Verified
2026-04-03: disabling the shim immediately restores link-up notifications.

**No alternative libsaibcm exists.** `bcm_stat_multi_get` via dlsym returns zeros for
all enum values on this platform (BCM56960 / libsaibcm 14.3.x). `/dev/mem` SCHAN
register reads require proprietary SDK register maps. The only proven counter source
is `bcmcmd show counters` via the diag shell socket.

---

## Requirements

1. All 66 `PORT_STAT_COUNTER` SAI stat IDs populated for all flex sub-ports.
2. Single-lane (25G/10G), dual-lane (50G) breakout supported.
3. Non-breakout, quad-lane (100G) ports: zero behavior change — SAI handles them natively.
4. Dynamic flex detection — no hardcoded port list; works after `config interface breakout`.
5. **Must not interfere with `port_state_change` notifications.**
6. Ships inside the existing `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`.

---

## Architecture

Two components running inside the syncd container:

### Component 1: Fault Masker Shim (`libsai-stat-shim.so`)

An LD_PRELOAD shared library with a single purpose: prevent FlexCounter from logging
errors and dropping flex port keys from COUNTERS_DB.

- Intercepts `sai_api_query(SAI_API_PORT)`, replaces `get_port_stats` only.
- Shim function: call real `get_port_stats`. If SUCCESS → return (non-flex passthrough).
  If non-SUCCESS → `memset(values, 0, count * sizeof(uint64_t))`, return
  `SAI_STATUS_SUCCESS`.
- No bcmcmd, no cache, no socket, no stat map, no OID classification, no lane mapping.
- ~50 lines of C.

**Risk:** Any `sai_port_api_t` struct modification may break notifications. If the
masker shim breaks notifications on deployment, fall back to no-shim operation. The
daemon works identically without the masker — FlexCounter produces more error log
noise but the daemon's COUNTERS_DB writes are unaffected.

### Component 2: Flex Counter Daemon (`wedge100s-flex-counter-daemon`)

A standalone C binary, supervisor-managed, that polls bcmcmd and writes real counter
values to COUNTERS_DB via Redis.

- **Poll interval:** 3 seconds (configurable). Minimizes bcmcmd socket contention.
- **Socket strategy:** Connect-per-cycle. Connect → `show counters` → parse → disconnect.
  Socket held for ~70ms per cycle (~2.3% duty cycle), leaving it available to other
  callers 97% of the time.
- **Flex detection:** Observes COUNTERS_DB. Ports with ≤2 stat keys are flex; ports with
  66+ keys are handled by SAI. Self-correcting — no config parsing for classification.
- **Accumulation:** `bcmcmd show counters` returns per-call deltas. The daemon maintains
  running totals in memory and writes absolute values to COUNTERS_DB, matching what
  SONiC consumers expect.

---

## Data Flow

```
Every 3 seconds:
  daemon connects to /var/run/sswsyncd/sswsyncd.socket
  daemon sends "\n" (trigger prompt), waits for "drivshell>"
  daemon sends "show counters\n", reads until next prompt
  daemon parses per-port counter rows, accumulates deltas
  daemon reads COUNTERS_PORT_NAME_MAP from Redis (Ethernet→OID)
  daemon identifies flex OIDs (≤2 keys in COUNTERS_DB)
  daemon writes all 66 SAI stat fields per flex OID to COUNTERS_DB
  daemon disconnects

Meanwhile (every 1s, inside syncd):
  FlexCounter calls get_port_stats for each port
    non-flex → real SAI succeeds → FlexCounter writes COUNTERS_DB (normal path)
    flex → real SAI fails → masker returns zeros+SUCCESS → FlexCounter writes zeros
    daemon overwrites zeros with real accumulated values within 3s
```

---

## OID-to-Port Resolution

The daemon maps COUNTERS_DB OIDs to bcmcmd port names through a four-step chain:

1. **Redis `COUNTERS_PORT_NAME_MAP`** (DB 2) → Ethernet name → SAI OID
2. **Redis CONFIG_DB `PORT|EthernetN`** (DB 4) → `lanes` field (e.g., `117`)
3. **BCM config portmap** (`th-wedge100s-32x-flex.config.bcm`) → lane → SDK port number
   (e.g., lane 117 → SDK port 118)
4. **bcmcmd `ps`** → SDK port number → port name (e.g., SDK 118 → `xe86`)

Steps 3-4 are resolved once at startup (and on DPB). Steps 1-2 are re-read every cycle
to track port additions/removals.

The BCM config path is supplied via the `WEDGE100S_BCM_CONFIG` environment variable,
set in the syncd supervisor configuration.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| bcmcmd socket unavailable at startup | Skip cycle, retry in 3s. No log spam (single warning on first failure, then silent until success). |
| bcmcmd connect timeout (50ms) | Same as unavailable. Accumulated deltas preserved in memory — no data lost. |
| Redis unavailable | Skip write, retry in 3s. In-memory accumulators continue updating from bcmcmd. |
| No flex ports detected | Daemon idles. Still polls bcmcmd so accumulators stay current for when breakout is configured. |
| DPB (breakout change) | COUNTERS_PORT_NAME_MAP changes. Daemon detects new/removed Ethernet entries on next cycle. Lane→port mapping re-resolved. Accumulators for removed ports discarded; new ports start from zero. |
| Masker shim breaks notifications | Remove shim (no LD_PRELOAD). Daemon works identically. FlexCounter logs errors for flex ports — cosmetic issue only. |
| Malformed `show counters` output | Skip affected port rows, log WARNING once. |

---

## Components — Detailed

### Fault Masker Shim

```c
static sai_status_t shim_get_port_stats(
    sai_object_id_t oid, uint32_t count,
    const uint32_t *ids, uint64_t *values)
{
    sai_status_t st = g_real_get_port_stats(oid, count, ids, values);
    if (st == SAI_STATUS_SUCCESS)
        return st;
    memset(values, 0, count * sizeof(uint64_t));
    return SAI_STATUS_SUCCESS;
}
```

The `sai_api_query` intercept saves the real `get_port_stats` pointer and replaces it
in the struct. The struct modification approach (copy vs mprotect) is an implementation
detail to be determined during testing. If neither approach preserves notifications,
the masker is removed entirely.

### Flex Counter Daemon — Main Loop

```
init:
  parse BCM config → build lane_map[]
  connect to bcmcmd → run "ps" → build ps_map[] (SDK port → name)
  connect to Redis

loop (every 3s):
  connect to bcmcmd socket (50ms timeout)
  send "show counters\n"
  parse response → per-port delta values
  accumulate deltas into running totals
  read COUNTERS_PORT_NAME_MAP from Redis
  for each Ethernet port in the map:
    check COUNTERS:<oid> key count
    if ≤2 keys (flex port):
      resolve Ethernet → lanes → SDK port → port name
      look up port name in running totals
      HSET COUNTERS:<oid> with all 66 SAI stat fields
  disconnect from bcmcmd
  sleep(3)
```

### bcmcmd Client (reused from existing code)

The existing `bcmcmd_client.c` provides:
- `bcmcmd_connect(path, timeout_ms)` — non-blocking connect with timeout
- `bcmcmd_close(fd)` — close connection
- `bcmcmd_ps(fd, ...)` — parse `ps` command output into SDK port → name table
- `bcmcmd_fetch_counters(fd, cache)` — parse `show counters` into per-port values

These functions are moved to the daemon build and used as-is. The counter accumulation
logic (delta → running total) is preserved from the existing implementation.

### SAI→bcmcmd Stat Map (reused from existing code)

The existing `stat_map.c` maps SAI port stat IDs to bcmcmd counter names:
- `SAI_PORT_STAT_IF_IN_OCTETS` (0) → `"RBYT"`
- `SAI_PORT_STAT_IF_IN_UCAST_PKTS` (1) → `"RUCA"`
- etc. (68 entries total, covering all PORT_STAT_COUNTER IDs)

Moved to the daemon build and used as-is. The table has 68 entries covering all
PORT_STAT_COUNTER IDs that FlexCounter polls (some map to NULL where bcmcmd has no
equivalent — those return 0). The daemon uses this table to translate accumulated
bcmcmd counter values into SAI stat field names for Redis writes.

---

## Source Layout

```
wedge100s-32x/
  sai-stat-shim/               # simplified — masker only
    shim.c                     # ~50 lines, fault masker
    shim.h                     # SAI type stubs only (no bcmcmd types)
    compat.c                   # glibc __isoc23_sscanf compat
    Makefile                   # builds libsai-stat-shim.so

  flex-counter-daemon/
    daemon.c                   # main loop, Redis I/O, OID→port mapping, flex detection
    bcmcmd_client.c            # socket I/O, ps/show-counters parsers (from sai-stat-shim)
    bcmcmd_client.h            # bcmcmd function declarations
    stat_map.c                 # SAI→bcmcmd counter name table (from sai-stat-shim)
    stat_map.h                 # stat map declarations
    Makefile                   # builds wedge100s-flex-counter-daemon, links hiredis
```

---

## Build & Packaging

### Build Integration

`debian/rules` `override_dh_auto_build` gains a new block for the daemon:

```makefile
if [ -d $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon ]; then \
    $(MAKE) $(MAKE_FLAGS) -C $(MOD_SRC_DIR)/$${mod}/flex-counter-daemon; \
fi;
```

The masker shim build remains in the existing `sai-stat-shim` block.

### Installation

- `libsai-stat-shim.so` → `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/`
- `wedge100s-flex-counter-daemon` → `/usr/bin/`

### Syncd Container Integration

The daemon runs as a supervisor program inside the syncd container. The `postinst`
script adds a supervisor config file:

```ini
[program:flex-counter-daemon]
command=/usr/bin/wedge100s-flex-counter-daemon
priority=100
autostart=true
autorestart=true
startsecs=10
startretries=3
stdout_logfile=syslog
stderr_logfile=syslog
```

The daemon must start after syncd has initialized the BCM SDK (bcmcmd socket available).
The 10-second `startsecs` plus internal connect-retry logic handles the startup race.

### Dependencies

- `hiredis` — C Redis client library. Available in the SONiC build environment.
  Link with `-lhiredis`. Header: `<hiredis/hiredis.h>`.

---

## Redis Interface

All writes target COUNTERS_DB (Redis DB 2).

### Reads

| Key | DB | Purpose |
|---|---|---|
| `COUNTERS_PORT_NAME_MAP` | 2 | Ethernet name → SAI OID mapping |
| `COUNTERS:<oid>` | 2 | Check key count (flex detection) |
| `PORT\|EthernetN` | 4 (CONFIG_DB) | `lanes` field for OID→SDK port resolution |

### Writes

```
HSET COUNTERS:<oid> SAI_PORT_STAT_IF_IN_OCTETS <uint64>
HSET COUNTERS:<oid> SAI_PORT_STAT_IF_IN_UCAST_PKTS <uint64>
HSET COUNTERS:<oid> SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS <uint64>
... (all 66 SAI port stat fields)
```

Values are stringified uint64 (matching FlexCounter's format). Written as a single
`HMSET` per OID per cycle for atomicity.

---

## Testing

### Existing Tests (run unmodified)

- **stage_24_counters** (10 tests) — validates COUNTERS_DB structure, key counts,
  `show interfaces counters` output, counter increment under traffic.
- **stage_25_shim** (11 tests) — validates flex port key count ≥60, counter
  accumulation, DPB round-trips, `sonic-clear counters`.

The observable behavior is identical to the original shim: flex ports have 66+ keys
with real counter values in COUNTERS_DB. Tests pass without modification.

### New Validation

- Verify `port_state_change` notifications work (ports reach oper-up) — the primary
  regression that motivated this redesign.
- Verify daemon survives syncd restart (supervisor autorestart).
- Verify daemon handles DPB correctly (port map changes mid-operation).

---

## Migration from Previous Implementation

1. Strip `sai-stat-shim/shim.c` to masker-only (~50 lines). Remove all bcmcmd,
   cache, classification, lane mapping code.
2. Remove `sai-stat-shim/bcmcmd_client.c` and `sai-stat-shim/stat_map.c` from the
   shim build (they move to the daemon).
3. Simplify `sai-stat-shim/shim.h` to SAI type stubs only.
4. Create `flex-counter-daemon/` with `daemon.c`, moved `bcmcmd_client.c`,
   moved `stat_map.c`, and new `Makefile`.
5. Update `debian/rules` to build and install the daemon.
6. Update `postinst` to add supervisor config for the daemon.

---

## File Locations

| Resource | Path |
|---|---|
| Masker shim source | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/` |
| Daemon source | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/flex-counter-daemon/` |
| BCM config | `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` |
| Platform .deb rules | `platform/broadcom/sonic-platform-modules-accton/debian/rules` |
| Supervisor config (target) | `/etc/supervisor/conf.d/flex-counter-daemon.conf` (inside syncd container) |
| Test stages | `tests/stage_24_counters/`, `tests/stage_25_shim/` |
| This spec | `docs/superpowers/specs/2026-04-03-flex-counter-daemon-design.md` |
