# NF-05 — Speed Change: TEST PLAN

## Mapping

There is no dedicated stage for speed change. Tests are embedded in or adjacent to
`tests/stage_14_breakout/` as speed change is a prerequisite for DPB (NF-06).
The exact test file may be `tests/stage_14_breakout/test_speed_change.py` (if created)
or included inline.

## Required Hardware State

- Ethernet0 admin-up (or admin-down is acceptable — no peer needed)
- Ethernet0 NOT in a PortChannel (it is not a LAG member in the current config)
- syncd stable

**Do not run on**: Ethernet16, Ethernet32 (LAG members in PortChannel1)

## Step-by-Step Test Actions

### 1. Record baseline speed

```bash
redis-cli -n 4 hget 'PORT|Ethernet0' speed
```

**Expected baseline**: `100000`

### 2. Change speed to 40G

```bash
sudo config interface speed Ethernet0 40000
```

**Pass**: rc=0, no error output
**Fail**: Any non-zero rc or error mentioning "not supported" or "invalid"

### 3. Verify CONFIG_DB propagation

```bash
redis-cli -n 4 hget 'PORT|Ethernet0' speed
```

**Pass**: `40000`

### 4. Verify APP_DB propagation

```bash
redis-cli -n 0 hget 'PORT_TABLE:Ethernet0' speed
```

**Pass**: `40000`

### 5. Verify CLI display

```bash
show interfaces status Ethernet0
```

**Pass**: Speed column shows `40G`

### 6. Verify syncd still running

```bash
docker ps --filter name=syncd --format '{{.Status}}'
```

**Pass**: status starts with `Up`

### 7. Restore to 100G

```bash
sudo config interface speed Ethernet0 100000
```

**Pass**: rc=0

### 8. Verify restore in CONFIG_DB and CLI

```bash
redis-cli -n 4 hget 'PORT|Ethernet0' speed
show interfaces status Ethernet0
```

**Pass**: speed = `100000` in DB, `100G` in CLI

### 9. Verify no side effects on linked ports

```bash
show interfaces status Ethernet16 Ethernet32 Ethernet48 Ethernet112
```

**Pass**: all 4 still show oper=up (speed change on Ethernet0 must not affect other ports)

## Pass/Fail Criteria — Summary

| Check | Expected |
|---|---|
| `config interface speed Ethernet0 40000` | rc=0 |
| CONFIG_DB speed after 40G change | `40000` |
| APP_DB speed after 40G change | `40000` |
| `show interfaces status` speed | `40G` |
| syncd running | yes |
| `config interface speed Ethernet0 100000` | rc=0 |
| CONFIG_DB speed after restore | `100000` |
| Other ports unaffected | oper=up |

## State Changes and Restoration

| Change | Restoration |
|---|---|
| Speed Ethernet0 → 40000 | `sudo config interface speed Ethernet0 100000` |

The test always restores Ethernet0 to 100G in the finally block. No `config save` is
called during the test — changes are volatile and lost on `config reload` or reboot.

## Notes on BCM Hardware Behavior

After `config interface speed Ethernet0 40000`, BCM hardware (`bcmcmd "ps ce28"`) still
shows `100G`. This is expected — the static BCM config locks serdes at init time. The DB
propagation is the meaningful assertion. Actual hardware serdes reconfiguration would
require `config reload`.
