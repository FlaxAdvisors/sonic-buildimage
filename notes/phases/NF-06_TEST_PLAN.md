# NF-06 — DPB (Dynamic Port Breakout): TEST PLAN

## Mapping

Test stage: `tests/stage_14_breakout/` (18 tests, verified passing 2026-03-06)

## Required Hardware State

- Flex BCM config deployed (`sai.profile` points to `th-wedge100s-32x-flex.config.bcm`)
- pmon running (xcvrd must not interfere with breakout)
- `/etc/sonic/port_breakout_config_db.json` present (postinst installs it)
- BREAKOUT_CFG table seeded in CONFIG_DB for all 32 ports
- **Test port**: Ethernet80 (Port 21) — has breakout cable, Ethernet80/81 link up
- **Do not break out**: Ethernet16, Ethernet32 (PortChannel1 LAG members)

## Step-by-Step Test Actions

### Phase 1 — Pre-test verification

#### 1a. platform.json exists at correct path

```bash
ls /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json
```

**Pass**: file exists

#### 1b. hwsku.json exists

```bash
ls /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json
```

**Pass**: file exists

#### 1c. show interfaces breakout lists all 32 ports with 3 modes

```bash
show interfaces breakout
```

**Pass**: output contains `1x100G[40G]`, `2x50G`, `4x25G[10G]` for each port

#### 1d. port_breakout_config_db.json present in /etc/sonic/

```bash
ls /etc/sonic/port_breakout_config_db.json
```

**Pass**: file exists; size > 1000 bytes

### Phase 2 — Execute breakout

#### 2a. Break out Ethernet80 to 4x25G

```bash
sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l
```

**Pass**: rc=0; output contains `Breakout process got successfully completed`
**Fail**: rc!=0 or any `ERROR` in output

#### 2b. Verify sub-ports in show interfaces status

```bash
show interfaces status Ethernet80 Ethernet81 Ethernet82 Ethernet83
```

**Pass**: all 4 ports appear with speed=25G

#### 2c. Verify sub-port FEC = none

```bash
redis-cli -n 4 hget 'PORT|Ethernet80' fec
```

**Pass**: `none` (SAI rejects `rs` for 25G; `fc` brings link down on DAC)

#### 2d. Verify sub-port admin_status = up

```bash
redis-cli -n 4 hget 'PORT|Ethernet80' admin_status
```

**Pass**: `up`

#### 2e. Verify at least one sub-port link-up (Ethernet80 or 81 with breakout cable)

```bash
show interfaces status Ethernet80
```

**Pass**: oper=up (breakout cable to compute node is present on Port 21)

#### 2f. Verify parent Ethernet80 no longer in show interfaces status

```bash
show interfaces status | grep -E "Ethernet80\b"
```

**Pass**: no 100G entry for Ethernet80 (it has been split)

### Phase 3 — Restore to 1x100G

#### 3a. Revert breakout

```bash
sudo config interface breakout Ethernet80 '1x100G[40G]' -y -f -l
```

**Pass**: rc=0; `Breakout process got successfully completed`

#### 3b. Verify Ethernet80 restored as 100G port

```bash
show interfaces status Ethernet80
```

**Pass**: single port at 100G, admin=up

#### 3c. Verify sub-ports Ethernet81/82/83 are gone

```bash
show interfaces status | grep "Ethernet8[123]"
```

**Pass**: no output (sub-ports deleted)

#### 3d. Save config

```bash
sudo config save -y
```

### Phase 4 — Verify PortChannel1 unaffected

```bash
show interfaces portchannel
```

**Pass**: PortChannel1 LACP(A)(Up), Ethernet32(S) Ethernet16(S) — breakout on Ethernet80 must not affect the LAG

## Pass/Fail Criteria — Summary

| Check | Expected |
|---|---|
| platform.json present | yes |
| hwsku.json present | yes |
| `show interfaces breakout` modes | `1x100G[40G]`, `2x50G`, `4x25G[10G]` per port |
| `/etc/sonic/port_breakout_config_db.json` | present |
| Breakout command rc | 0 |
| Sub-ports appear in status | Ethernet80-83 at 25G |
| Sub-port FEC | `none` |
| Sub-port admin | `up` |
| Ethernet80/81 oper | `up` (breakout cable connected) |
| Revert command rc | 0 |
| Ethernet80 restored | 100G single port, admin=up |
| Sub-ports gone after revert | yes |
| PortChannel1 unaffected | LACP(A)(Up) |

## State Changes and Restoration

| Change | Restoration |
|---|---|
| Ethernet80 broken to 4x25G | `config interface breakout Ethernet80 '1x100G[40G]' -y -f -l` |

The test always restores Ethernet80 in a `finally` equivalent block (`-f` flag forces removal
of dependent config, `-l` waits for completion). `config save` is called after restore to
persist the 1x100G state.
