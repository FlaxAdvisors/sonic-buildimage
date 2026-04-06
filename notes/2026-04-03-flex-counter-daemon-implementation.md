# Flex Counter Daemon Implementation — 2026-04-03

## What Was Done

Split the monolithic `sai-stat-shim` into two components:

1. **Fault-masker shim** (`libsai-stat-shim.so`) — ~70 lines. LD_PRELOAD into syncd.
   Intercepts `sai_api_query(SAI_API_PORT)`, patches `get_port_stats` to return
   zeros+SUCCESS when the real call fails. This keeps flex port keys alive in
   COUNTERS_DB so FlexCounter doesn't drop them.

2. **Flex counter daemon** (`wedge100s-flex-counter-daemon`) — standalone C binary.
   Runs inside syncd container via supervisor. Polls bcmcmd `show counters` every 3s,
   detects flex sub-ports by lane count (<4 lanes = flex), resolves OID→port via
   CONFIG_DB lanes + BCM config + ps map, writes real counter values to COUNTERS_DB
   via Redis HMSET.

## Commits

- `779e321b9` refactor(shim): strip to fault-masker only
- `bb22014ee` feat(flex-counter-daemon): add bcmcmd_client and stat_map (moved from shim)
- `0164a4cb1` feat(flex-counter-daemon): add daemon main loop with Redis I/O
- `792536cbc` feat(packaging): wire flex-counter-daemon into deb build and postinst
- `d94550ba7` fix(flex-counter-daemon): static hiredis link + lane-count flex detection
- `b4cdf21a5` fix(tests): update stage_25_shim for split shim+daemon architecture

## Key Fixes During Implementation

### 1. libhiredis version mismatch
Build slave (trixie) has hiredis 1.1.0; syncd container has hiredis 0.14.
Fix: Static link hiredis via `-Wl,-Bstatic -lhiredis -Wl,-Bdynamic`.
Binary grows from 27KB to 93KB.

### 2. Flex detection by key count broken by shim
Original plan: detect flex ports by COUNTERS_DB key count (<=2 keys = flex).
Problem: The fault-masker shim makes ALL ports return SUCCESS, so FlexCounter
writes 66+ keys for every port. All ports have 68 keys, breaking the detection.
Fix: Detect flex ports by CONFIG_DB lane count (<4 lanes = breakout sub-port).

### 3. docker exec heredoc needs -i flag
`docker exec syncd sh -c 'cat > file' <<'EOF'` silently writes empty file
without `-i` flag. Fix: `docker exec -i syncd ...`.

## Verified on Hardware (2026-04-03)

- Daemon starts and enters RUNNING state in syncd container (verified on hardware 2026-04-03)
- BCM config parsed (128 lane entries), ps map refreshed (128 ports) (verified on hardware 2026-04-03)
- Flex ports (Ethernet0-3) have 68 SAI stat keys with real counter values (verified on hardware 2026-04-03)
- `show interfaces status` shows correct oper up/down (port_state_change not broken) (verified on hardware 2026-04-03)
- Daemon survives syncd restart via supervisor autorestart (verified on hardware 2026-04-03)
- All 21 tests pass (stage_24_counters + stage_25_shim) (verified on hardware 2026-04-03)

## Known Limitations

### N/A in `show interfaces counters` for some flex ports
Ports like Ethernet66/67/80/81 show `N/A` for B/s and % columns.
Root cause: bcmcmd `show counters` only lists ports with non-zero counters.
Flex sub-ports with very low traffic don't appear in the output, so the daemon
has no data to write. FlexCounter (via shim) writes zeros. Rate calculation
gets zero delta → N/A.
Ports with active traffic (Ethernet0/1) show proper B/s rates.

### Pre-shim FlexCounter drops
Ports that got SAI errors before the shim loaded stay at <=2 COUNTERS_DB keys.
FlexCounter dropped them from its poll group and never re-adds them.
Fix: syncd restart (the shim is loaded before FlexCounter starts on fresh syncd).

## Architecture

```
syncd container:
  ├── syncd process (with LD_PRELOAD=libsai-stat-shim.so)
  │     └── FlexCounter polls get_port_stats → shim masks failures → zeros+SUCCESS
  └── flex-counter-daemon (supervisor)
        └── bcmcmd show counters → parse → Redis HMSET for flex ports (<4 lanes)
```
