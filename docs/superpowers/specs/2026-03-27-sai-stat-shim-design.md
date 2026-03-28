# SAI Stat Shim — Design Spec
**Date:** 2026-03-27
**Platform:** Accton Wedge100S-32X (BCM56960 / Tomahawk)
**Branch:** `wedge100s`

---

## Problem Statement

12 flex sub-ports (Ethernet0-3, Ethernet64-67, Ethernet80-83) — and potentially all 128
ports in a fully broken-out configuration — populate only 2 keys in COUNTERS_DB
(`SAI_PORT_STAT_IN/OUT_DROPPED_PKTS`) instead of the expected 66. Non-breakout ports
work correctly (68 keys each).

**Root cause:** `libsaibcm.so 14.3.0.0.0.0.3.0` — `brcm_sai_get_port_stats()` returns
non-SUCCESS for all BCM logical ports in flex `.0` portmap entries (BCM ports 50-53,
68-71, 118-121). BCM hardware counters ARE working — `bcmcmd show counters xe36` returns
real RPKT/TPKT/RBYT/TBYT data. The failure is purely in the SAI translation layer.

**No alternative libsaibcm exists.** Investigated on 2026-03-27: only one version
(14.3.0.0.0.0.3.0) is available in apt, with a single monolithic `libsai.so.1.0` binary
covering all ASIC families. There is no TH-specific or flex-port-aware variant.

---

## Requirements

1. All 66 `PORT_STAT_COUNTER` SAI stat IDs populated for all flex sub-ports.
2. Full breakout (128 ports) must be supported — no per-port call overhead.
3. Non-breakout ports: pure passthrough, zero behavior change.
4. Dynamic flex detection — no hardcoded port list; works after `config interface breakout`.
5. Permanent production component — robust error handling, no syncd destabilization.
6. Ships inside the existing `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`.

---

## Architecture

An `LD_PRELOAD` shared library (`libsai-stat-shim.so`) injected into the syncd container
intercepts `sai_api_query(SAI_API_PORT)`. On that call it receives the real
`sai_port_api_t` struct from `libsai.so.1.0`, replaces `get_port_stats` and
`get_port_stats_ext` function pointers in-place with shim functions, then returns the
modified struct. All other SAI API queries pass through untouched.

`get_port_stats_ext` is replaced with an immediate passthrough — the drop-counter path
that currently works via ext is preserved; the replacement guards against future SAI
routing changes.

---

## Components

### 1. Port Classifier

- On first call for a given port OID, queries `SAI_PORT_ATTR_HW_LANE_LIST` via the real
  SAI function pointer.
- Maps lanes → BCM logical port numbers by parsing the BCM config file at init (path
  supplied via environment variable `WEDGE100S_BCM_CONFIG`, set by postinst in syncd
  supervisor config).
- Caches OID → BCM port number and OID → is-flex flag.
- Cache invalidated when `sai_api_query` is called again (syncd reinitializes on breakout
  change); rebuilt transparently on next `get_port_stats` call.

### 2. Counter Cache

- Holds last full `show counters` batch result: `bcm_port → {counter_name → uint64}`.
- Timestamped; stale after 500ms.
- On any flex port `get_port_stats` call with a stale cache: triggers a new batch fetch
  and refreshes all ports before returning. One fetch serves all 128 ports.
- Concurrent fetch deduplication: fetch-in-progress flag prevents duplicate socket I/O
  from parallel FlexCounter threads.

### 3. bcmcmd Client

- Connects to the BCM diag shell Unix socket. **Exact path TBD — verify on target before
  implementation:** `find /var/run /tmp -name '*.sock' 2>/dev/null` inside syncd container.
  Likely `/var/run/sswsyncd/sai-bcm-diag.sock` or similar.
- Sends `show counters\n`, reads until shell prompt, parses counter name → value pairs
  per port.
- Non-blocking connect with 50ms timeout.
- On unavailability or timeout: returns empty result (cache stays stale, callers get
  zeros + `SAI_STATUS_SUCCESS`).

### 4. SAI → bcmcmd Stat ID Map

- Static lookup table: `sai_port_stat_t → bcmcmd_counter_name` (string).
- Derived empirically pre-implementation: compare `show counters xe<N>` output vs.
  `redis-cli hgetall COUNTERS:<OID>` on a working non-breakout port to establish the
  exact mapping for libsai 14.3.0.0.0.0.3.0 on BCM56960.
- Baked into `stat_map.c` as a compile-time array. 66 entries covering all
  `PORT_STAT_COUNTER` IDs.

---

## Data Flow

```
syncd calls get_port_stats(oid, stat_ids[], count, values[])
  │
  ├─ port_classifier.is_flex(oid)?
  │     NO  → passthrough to real brcm_sai_get_port_stats() → return
  │     YES → bcm_port = classifier.bcm_port(oid)
  │
  ├─ counter_cache.is_stale()?
  │     NO  → skip
  │     YES → bcmcmd_client.fetch_all()
  │              send "show counters\n" to diag socket
  │              parse all xe/ce port rows → update cache
  │              on socket error → leave cache empty
  │
  └─ for each stat_id in stat_ids[]:
        name = stat_id_map[stat_id]        // static table
        values[i] = cache.get(bcm_port, name)  // 0 if absent
     return SAI_STATUS_SUCCESS
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| bcmcmd socket unavailable at startup | Empty result, cache stays stale, return zeros + SUCCESS. No log spam. Retry every poll cycle until socket ready. |
| bcmcmd connect timeout (50ms) | Same as unavailable. |
| Malformed `show counters` row | Skip that port's entries, log WARNING once per port. |
| Breakout config change | `sai_api_query` re-call invalidates classifier cache; rebuilt on next stat call. |
| Concurrent `get_port_stats` from multiple threads | Single mutex guards cache reads/writes. Socket I/O is outside the lock; fetch-in-progress flag serializes fetches. |

**Startup zeros are acceptable.** FlexCounter retains the port in its poll list on SUCCESS;
counters transition from 0 to real values once the bcmcmd socket becomes available
(typically within 5–10s of syncd start).

---

## Build & Packaging

### Source layout

```
platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/
  ├── Makefile            # builds libsai-stat-shim.so
  ├── shim.c              # sai_api_query intercept, port classifier, cache
  ├── bcmcmd_client.c     # Unix socket I/O, show counters parser
  └── stat_map.c          # static SAI→bcmcmd counter name table (66 entries)
```

### Build integration

- New `$(MAKE) -C sai-stat-shim` target added to the wedge100s module build in
  `platform-modules-accton.mk`.
- Output: `/usr/lib/libsai-stat-shim.so` installed via the platform .deb.
- Standard x86_64 userspace shared library; compiled with the build slave's gcc targeting
  Debian trixie libc (same target OS as the syncd container).

### postinst / prerm

`postinst` patches `/etc/supervisor/conf.d/syncd.conf` (idempotent):
```bash
# Adds to syncd environment line:
LD_PRELOAD=/usr/lib/libsai-stat-shim.so
WEDGE100S_BCM_CONFIG=/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm
```

`prerm` removes the additions on package removal.

---

## Testing

### Pre-implementation: empirical stat ID mapping derivation

On a working non-breakout port:
```bash
# BCM side
bcmcmd "show counters xe<N>"
# SAI side
redis-cli -n 2 hgetall COUNTERS:<OID>
```
Cross-reference to build the 66-entry `stat_map.c` table.

### Unit tests (off-target)

- **Port classifier:** mock SAI attribute calls, verify flex vs. non-flex detection for
  various OIDs.
- **bcmcmd parser:** feed captured `show counters` output fixtures, verify
  counter_name→value extraction per port.
- **Cache TTL:** verify 500ms staleness logic and concurrent fetch deduplication.

### On-target integration tests (new pytest stage `tests/stage_25_shim/`)

| Test | Pass criteria |
|---|---|
| Flex sub-port key count | All 12 (or 128 full-breakout) flex ports have exactly 66 keys in COUNTERS_DB |
| Non-breakout regression | All non-breakout ports still have 68 keys |
| Counter increment under traffic | Values increase during `test_throughput.py` run |
| Startup zeros | Poll COUNTERS_DB within 2s of syncd start — keys present (zeros), not missing |

### Regression

Existing `tests/stage_23_throughput/` and `tests/stage_24_counters/` run unmodified to
confirm passthrough leaves non-breakout ports untouched.

---

## File Locations

| Resource | Path |
|---|---|
| Shim source | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/` |
| Platform .mk | `platform/broadcom/platform-modules-accton.mk` |
| BCM config | `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` |
| Supervisor config (target) | `/etc/supervisor/conf.d/syncd.conf` |
| Test stage | `tests/stage_25_shim/` |
| This spec | `docs/superpowers/specs/2026-03-27-sai-stat-shim-design.md` |
