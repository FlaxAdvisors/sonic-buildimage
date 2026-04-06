# Breakout Port Counter Problem — Architecture & Component Map

**Date:** 2026-04-05  
**Platform:** Wedge100S-32X, Broadcom Tomahawk, SONiC  
**Problem:** `show interfaces counters` shows 0 for all breakout sub-ports (Ethernet0/1/66/67/80/81), even though real traffic is flowing and BCM hardware counters are non-zero.

---

## The Five Layers

```
 ┌──────────────────────────────────────────────────────────┐
 │  CLI / Application                                       │
 │  `show interfaces counters`  (sonic-utilities)           │
 │  Reads: COUNTERS:<oid> + RATES:<oid>  from DB 2          │
 └────────────────────────┬────────────��────────────────────┘
                          │ redis (DB 2 = COUNTERS_DB)
 ┌────────────────────────▼─────────────────────────────────┐
 │  Rate Computation — port_rates.lua                       │
 │  Loaded by FlexCounter as a "plugin"                     │
 │  Called after each poll with OIDs as KEYS                 │
 │  Reads:  COUNTERS:<oid>  (current counters)              │
 │  Reads:  RATES:<oid>     (_last values from prev cycle)  │
 │  Writes: RATES:<oid>     (RX_BPS, TX_BPS, RX_PPS, ...)  │
 │  Writes: RATES:<oid>:PORT (state machine: INIT_DONE)     │
 └─���──────────────────────┬─────────────���───────────────────┘
                          │ called by FlexCounter after SAI poll
 ┌───────────────��────────▼────────────────���────────────────┐
 │  FlexCounter  (C++, inside syncd process)                │
 │  src/sonic-sairedis/syncd/FlexCounter.cpp                │
 │  Configured by: DB 5 FLEX_COUNTER_TABLE + GROUP_TABLE    │
 │  Every 1000ms, for each OID in PORT_STAT_COUNTER group:  │
 │    calls vendorSai->getStats(PORT, oid, stat_ids, buf)   │
 │    on success: writes buf values to COUNTERS:<oid> DB 2   │
 │    on failure: logs error, skips this port this cycle     │
 │  Then runs port_rates.lua plugin with all OIDs           │
 └─────────���──────────────┬───────────���─────────────────────┘
                          │ C function call (in-process)
 ┌────────────────────────▼─────────────��───────────────────┐
 │  SAI (Switch Abstraction Interface)                      │
 │  libsai.so — Broadcom's proprietary implementation       │
 │  sai_port_api->get_port_stats(oid, n, stat_ids, values)  │
 │  Translates SAI port OIDs to internal SDK port handles   │
 │  Calls BCM SDK to read hardware counters                 │
 │  *** FAILS for breakout sub-ports on Tomahawk ***        │
 └─────────────��──────────┬──────���──────────────────────��───┘
                          │ BCM SDK call
 ┌─────────────��──────────▼────────���────────────────────────┐
 ���  Memory-Mapped Hardware Counters (MEMORY_COUNTER_INFO)   │
 │  SOBMH + TH MAC stats registers per physical port        │
 │  Readable via bcmcmd "show counters", correct per-port   │
 │  HW doesn't know about SAI OIDs; speaks port names       │
 │  (xe38, xe86, ce0, ...)                                  │
 └─────────────────��────────────────────��───────────────────┘
```

## Why SAI Fails for Breakout Ports

Broadcom's SAI on Tomahawk maps SAI port OIDs to internal SDK port handles.
For native 4-lane 100G ports (QSFP in default mode), this mapping works.
For breakout sub-ports (1-lane 25G or 10G from a broken-out QSFP), SAI's
`get_port_stats()` returns `SAI_STATUS_FAILURE` — the SDK port handle lookup
doesn't correctly resolve the sub-port.

**The hardware counters exist and are correct.** `bcmcmd show counters` reads
them directly from memory-mapped registers. The problem is exclusively in
SAI's translation layer.

## What FlexCounter Does When SAI Fails

```
For COUNTER_TYPE_PORT (the normal port stats group):
  use_sai_stats_ext = false   ← uses getStats, NOT getStatsExt
  stats_mode = STATS_MODE_READ

collectData() calls:
  vendorSai->getStats(SAI_OBJECT_TYPE_PORT, oid, n_stats, stat_ids, values)
  
If status != SAI_STATUS_SUCCESS:
  logs "Failed to get stats of PORT 0x<oid>: <status>"
  returns false → counter values NOT written to DB 2 this cycle
```

**Key finding:** When SAI fails, FlexCounter does NOT write zeros. It skips.
So the zeros in DB 2 are not from FlexCounter overwriting — they're the
*initial* values that were never updated.

**But wait:** After syncd restart, SAI may **succeed** for breakout ports
and return zeros (counter baseline resets to 0 on SDK re-init). In that case
FlexCounter DOES write zeros, and port_rates.lua computes 0 B/s from them.
Whether SAI fails or succeeds-with-zeros, the result for breakout ports is
the same: `show interfaces counters` shows 0.

## The OID Problem

SAI Object IDs (OIDs) are ephemeral. They are assigned by SAI at syncd
initialization and **change on every syncd restart**.

```
Before restart:  Ethernet66 = oid:0x10000000005ce
After restart:   Ethernet66 = oid:0x1000000000nnn  (new value)
```

**Who writes the OID map:**
- orchagent (C++, inside syncd container) creates SAI port objects at init
- Each `sai_create_port()` returns a new OID
- orchagent writes `COUNTERS_PORT_NAME_MAP` (DB 2): `Ethernet66 → oid:0x...`
- orchagent writes `FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:<oid>` (DB 5): tells FlexCounter to poll this OID

**The map is authoritative.** Any component that needs to translate between
Ethernet port names and OIDs must read `COUNTERS_PORT_NAME_MAP` from DB 2.

## How to Map PHY Port → OID

The chain:

```
CONFIG_DB (DB 4)
  PORT|Ethernet66 → lanes: "55,56"  (physical lanes from platform.json)
  
  first_lane = 55

BCM config (th-wedge100s-32x-flex.config.bcm)
  portmap_52.0=55:10000  → SDK port 52, physical lane 55, speed 10G
  
  SDK port = 52

bcmcmd ps
  xe38( 52)  → port name "xe38", SDK port 52
  
  port_name = "xe38"

bcmcmd show counters
  RBYT.xe38: 1,234,567  → hardware byte counter for xe38
  TBYT.xe38: 987,654

COUNTERS_PORT_NAME_MAP (DB 2)
  Ethernet66 → oid:0x10000000005ce
  
  oid = "oid:0x10000000005ce"
```

So the daemon builds: **oid ↔ bcm_port_name** by joining:
- CONFIG_DB lanes → BCM config lane→SDK port → bcmcmd ps SDK port→name
- COUNTERS_PORT_NAME_MAP Ethernet name→oid

## The Shim Approach (LD_PRELOAD)

```
Normal flow:
  FlexCounter → vendorSai->getStats() → libsai.so → BCM SDK → FAIL

With shim:
  FlexCounter → vendorSai->getStats() → libsai-stat-shim.so
    → calls real libsai get_port_stats()
    → if SUCCESS: passthrough (native ports, zero overhead)
    → if FAILURE: read binary cache file written by daemon
                  fill values buffer with real BCM counters
                  return SAI_STATUS_SUCCESS
```

### What Must Restart When the Shim Is Loaded

**syncd must restart.** The shim is an LD_PRELOAD library — it hooks function
pointers at process load time. You cannot inject it into a running process.

When syncd restarts:
1. All SAI port objects are destroyed and recreated → **new OIDs**
2. orchagent re-populates COUNTERS_PORT_NAME_MAP with new OIDs
3. orchagent re-populates FLEX_COUNTER_TABLE with new OIDs
4. FlexCounter starts polling with new OIDs
5. port_rates.lua state machine resets (INIT_DONE cleared)

The daemon must detect this: re-read COUNTERS_PORT_NAME_MAP each cycle
to pick up new OIDs, and re-write the binary cache with the new OIDs
so the shim maps correctly.

### The Shim Race Condition

Even if the shim works perfectly:
1. Daemon polls bcmcmd every 3s, writes cache
2. FlexCounter polls SAI every 1s
3. Cache file may be stale by up to 3s
4. port_rates.lua computes rates from (current - last) / delta
5. If cache values jump (because daemon missed a cycle), rates spike
6. EWMA smoothing (alpha) dampens spikes but doesn't eliminate them

## Can We Do It Without a Shim?

**Yes.** This is the recommended approach (Option A from prior analysis).

The insight: FlexCounter and port_rates.lua are just Redis writers. Nothing
prevents us from writing the same keys ourselves from a daemon. If we
**remove breakout ports from FlexCounter's poll list**, there is no race.

```
Without shim — daemon-only approach:

1. Daemon reads COUNTERS_PORT_NAME_MAP → gets oid for each breakout port
2. Daemon reads FLEX_COUNTER_TABLE in DB 5 → DELETES breakout port entries
   (FlexCounter stops polling them, stops calling SAI for them)
3. Daemon polls bcmcmd every 3s → gets real hardware counters
4. Daemon writes COUNTERS:<oid> to DB 2 → raw counter values
5. Daemon writes RATES:<oid> to DB 2 → computed RX_BPS, TX_BPS, etc.
6. Daemon writes RATES:<oid>:PORT state → INIT_DONE = DONE
7. `show interfaces counters` reads DB 2 → sees real values
```

**Advantages over shim:**
- No syncd restart required (daemon runs on host)
- No LD_PRELOAD complexity
- No binary cache file / mmap coordination
- No race condition between daemon writes and FlexCounter writes  
- Survives syncd restarts (daemon re-reads OIDs, re-removes from DB 5)
- Pure Python, easy to debug

**Disadvantages:**
- Rate computation is our responsibility (not delegated to port_rates.lua)
- FEC BER stats not computed for breakout ports (port_rates.lua does this)
- Poll interval is 3s (bcmcmd is slow), vs FlexCounter's 1s

## What the Daemon Must Write to DB 2

### COUNTERS:<oid>
Same fields FlexCounter would write. Key ones for `show interfaces counters`:

| Field | Source BCM counter |
|-------|-------------------|
| SAI_PORT_STAT_IF_IN_OCTETS | RBYT |
| SAI_PORT_STAT_IF_IN_UCAST_PKTS | RUCA |
| SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS | RMCA + RBCA |
| SAI_PORT_STAT_IF_IN_ERRORS | RFCS |
| SAI_PORT_STAT_IF_IN_DISCARDS | RIDR |
| SAI_PORT_STAT_IF_OUT_OCTETS | TBYT |
| SAI_PORT_STAT_IF_OUT_UCAST_PKTS | TUCA |
| SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS | TMCA + TBCA |
| SAI_PORT_STAT_IF_OUT_ERRORS | TERR |
| SAI_PORT_STAT_IF_OUT_DISCARDS | TDRP |

### RATES:<oid>
Rate fields read by `show interfaces counters`:

| Field | Computation |
|-------|-------------|
| RX_BPS | (in_octets - in_octets_last) * 1000 / delta_ms |
| TX_BPS | (out_octets - out_octets_last) * 1000 / delta_ms |
| RX_PPS | (in_ucast + in_non_ucast - last) * 1000 / delta_ms |
| TX_PPS | (out_ucast + out_non_ucast - last) * 1000 / delta_ms |
| SAI_PORT_STAT_IF_IN_OCTETS_last | current in_octets (for next cycle) |
| SAI_PORT_STAT_IF_OUT_OCTETS_last | current out_octets |
| SAI_PORT_STAT_IF_IN_UCAST_PKTS_last | current in_ucast |
| SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS_last | current in_non_ucast |
| SAI_PORT_STAT_IF_OUT_UCAST_PKTS_last | current out_ucast |
| SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS_last | current out_non_ucast |

### RATES:<oid>:PORT
State machine for port_rates.lua (we must set this ourselves since
port_rates.lua won't run for ports we removed from FlexCounter):

| Field | Value |
|-------|-------|
| INIT_DONE | "DONE" |

## What the Daemon Must Delete from DB 5

For each breakout port OID, delete the key:
```
FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:<oid>
```

This must be re-done after every syncd restart because orchagent
re-populates FLEX_COUNTER_TABLE with new OIDs on init.

**Detection:** Compare current COUNTERS_PORT_NAME_MAP OIDs against
previously-seen OIDs. If any breakout port's OID changed, syncd restarted
→ re-delete from DB 5.

## Service Startup Dependencies & Interlocks

### Container & Process Startup Order

```
systemd: sonic.target
  ├── database.service    → docker-database container (Redis)
  ├── syncd.service       → docker-syncd-brcm container
  │   ├── Requires: database, opennsl-modules, config-setup
  │   ├── Created by: /usr/bin/syncd.sh (docker create + env vars)
  │   ├── Inside container (supervisord):
  ��   │   ├── rsyslogd
  │   │   ├─�� start.sh → syncd_init_common.sh → config_syncd_bcm()
  │   │   ├── syncd (via dsserve wrapper)
  │   │   │   └── dsserve /usr/bin/syncd --diag -u -s ...
  │   │   │       ├── Creates sswsyncd.socket (domain socket for bcmcmd)
  │   │   │       ├── SAI init: sai_create_switch() → BCM SDK init
  │   │   │       ├── Diag shell thread: SAI_SWITCH_ATTR_SWITCH_SHELL_ENABLE
  │   │   │       └── FlexCounter: polls SAI, runs Lua plugins
  │   │   ├── ledinit (waits for syncd, uses bcmcmd)
  │   │   └── flex-counter-daemon (our C daemon — DISABLED for Python approach)
  │   └── Bind mounts:
  │       ├── /usr/share/sonic/device/<platform>/ → /usr/share/sonic/platform/
  │       ├── /usr/share/sonic/device/<platform>/<hwsku>/ → /usr/share/sonic/hwsku/
  │       └── /var/run/docker-syncd/ → /var/run/sswsyncd/
  └── swss.service        → docker-orchagent container
      ├── Requires: syncd
      ├── orchagent: creates SAI port objects, writes:
      │   ├── COUNTERS_PORT_NAME_MAP (DB 2) — Ethernet name → OID
      │   ├── FLEX_COUNTER_TABLE (DB 5) — tells FlexCounter what to poll
      │   └── PORT_TABLE (DB 0) — port config for rate computation
      └── On syncd failure: triggers syncd restart → new OIDs
```

### The SAI Switch Creation Race

SAI switch creation (`brcm_sai_xgs_create_switch`) has a **known intermittent
failure** on Wedge100S: `_brcm_sai_port_pfc_mac_addr_set: get local MAC
address failed with error -1`. This causes the entire switch creation to fail.

**Recovery pattern (verified from logs):**
1. First `processSwitches` → SAI creates switch → **may fail** (PFC MAC error)
2. syncd enters "restart query only" mode
3. swss/orchagent detects failure, triggers syncd restart
4. Second `processSwitches` → SAI creates switch → **usually succeeds**

This means syncd may need **two full startup cycles** before the BCM SDK is
functional and the diag shell responds.

**Impact on our daemon:** The Python daemon must tolerate syncd not being ready.
It retries bcmcmd connections every POLL_INTERVAL until the diag shell responds.

### The LD_PRELOAD Trap

Container environment variables (including LD_PRELOAD) are set in
`/usr/bin/syncd.sh` at container creation time via `--env` flags to `docker create`.
They persist across `systemctl restart syncd` because the container is reused
(stopped/started, not recreated). To change them:

1. Edit `/usr/bin/syncd.sh` on the host
2. `docker stop syncd && docker rm syncd`
3. `systemctl restart syncd` (recreates container with new env)

**Lesson learned:** The LD_PRELOAD shim approach added complexity without benefit.
The shim was loaded into syncd but didn't help because the fundamental issue
is SAI returning failures for breakout ports. The daemon-only approach (writing
directly to Redis, removing ports from FlexCounter) is simpler and more reliable.

### The `/usr/share/sonic/hwsku` Path Confusion

Inside the syncd container: `/usr/share/sonic/hwsku/` is a bind mount to the
actual hwsku directory on the host.

On the host: `/usr/share/sonic/hwsku` **does not exist**. The full path is:
`/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/`

**Impact:** The daemon runs on the host and must use the full host path for
the BCM config file. Auto-detection must glob both patterns.

### bcmcmd / Diag Shell Dependencies

The BCM diag shell (`drivshell>`) requires:
1. syncd process running with `--diag` flag (provided by dsserve wrapper)
2. SAI switch successfully created (not just SDK init)
3. `SAI_SWITCH_ATTR_SWITCH_SHELL_ENABLE` set to true (done by diagShellThreadProc)

The `sswsyncd.socket` file exists as soon as dsserve starts, but the socket
**does not respond to commands** until the diag shell thread is running.
Connecting to the socket before the shell is ready returns `\r\n` but no prompt.

`bcmcmd` (the CLI tool) expects to send `\n`, receive `drivshell>` prompt,
then send commands. It times out if the prompt never arrives.

### FlexCounter Interlock

FlexCounter (C++, inside syncd) is configured by two Redis tables in DB 5:

| Key | Purpose |
|-----|---------|
| `FLEX_COUNTER_GROUP_TABLE:PORT_STAT_COUNTER` | Global config: poll interval, stats mode, Lua plugins |
| `FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:<oid>` | Per-port: which stats to collect |

orchagent populates these tables at init. FlexCounter reads them and starts
polling SAI. **To stop FlexCounter from polling a port, delete its DB 5 key.**

After syncd restart: orchagent re-populates DB 5 with new OIDs. The daemon
must re-delete breakout port entries. Detection: compare OIDs from
`COUNTERS_PORT_NAME_MAP` against previously-seen values.

### `config reload` vs `systemctl restart`

| Command | Effect |
|---------|--------|
| `systemctl restart syncd` | Stops/starts syncd container (reuses existing container if not removed) |
| `systemctl restart swss` | Restarts swss + triggers syncd restart |
| `config reload -y -f` | Full clean restart: stops all services, reloads config, restarts everything |

**For recovering from SAI init failures:** `config reload -y -f` is the most
reliable because it ensures a clean ASIC_DB state. Simple `systemctl restart`
may leave stale VID mappings in ASIC_DB that cause the second switch creation
to fail differently.

## Current State (2026-04-06, verified on hardware)

- **Working.** Python daemon running on host, all breakout ports show real counters and rates.
- syncd running without LD_PRELOAD shim (removed from /usr/bin/syncd.sh)
- C daemon disabled in syncd container (supervisord config set to /bin/true)
- Python daemon auto-detects BCM config from host path
- 12 breakout ports removed from FLEX_COUNTER_TABLE (DB 5)
- `show interfaces counters` shows non-zero RX_OK, RX_BPS for all 6 up breakout ports
- EWMA-smoothed rates matching port_rates.lua behavior
- OID change detection ready for syncd restarts

### Verified output (2026-04-06 01:01 UTC):
```
  Ethernet0        U       38     7.16 B/s      0.00%    ...    0  1008.28 B/s
  Ethernet1        U       38     7.16 B/s      0.00%    ...    0  1017.66 B/s
 Ethernet66        U       38     7.18 B/s      0.00%    ...
 Ethernet67        U       38     7.21 B/s      0.00%    ...
 Ethernet80        U       38     7.13 B/s      0.00%    ...
 Ethernet81        U       38     7.18 B/s      0.00%    ...
```

## Stage 2: C Port for Performance

Once Python daemon is stable across restarts:
- Port rate computation and FlexCounter removal to daemon.c
- daemon.c framework already exists with bcmcmd_client and stat_map
- Add rate computation, DB 5 deletion, OID tracking
- Binary: runs inside syncd container (has /usr/share/sonic/hwsku path)

## Bugs Found During Implementation

1. **BCM config path:** `/usr/share/sonic/hwsku` doesn't exist on host, only in container.
   Daemon must glob host path `/usr/share/sonic/device/*/Accton-WEDGE100S*/*.config.bcm`.

2. **`diag_shell=1` is NOT a BCM config key.** Adding it to the .bcm file does not enable
   the diag shell and may cause SAI init to behave differently. The diag shell is enabled
   by the `--diag` flag to syncd (passed by dsserve wrapper).

3. **SAI PFC MAC init race:** First switch creation often fails with "get local MAC address
   failed". Second attempt (after swss-triggered restart) usually succeeds. Recovery requires
   `config reload -y -f` if simple restart doesn't recover.

4. **`show counters` only returns ports with non-zero counters.** Down breakout ports
   won't appear in bcmcmd output, so their COUNTERS_DB values stay at 0 (which is correct).
