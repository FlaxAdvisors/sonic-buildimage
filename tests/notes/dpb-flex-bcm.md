# Dynamic Port Breakout — Flex BCM Config Fix

## Problem (2026-03-03)

Attempting 4x25G breakout on Ethernet64 (port 17) and Ethernet80 (port 21) failed:
1. `config interface breakout` CLI failed — missing `/etc/sonic/port_breakout_config_db.json`
2. Manually editing config_db.json + `config reload` crashed orchagent (SIGABRT)
3. Even a full reboot with the breakout config crashed orchagent

**Root cause**: The BCM config (`th-wedge100s-32x100G.config.bcm`) only defined 32x 100G
ports (`portmap_50=53:100`). SAI initialized with 4-lane 100G ports and rejected
`create_port` calls for 1-lane 25G sub-ports (`SAI_STATUS_INVALID_PARAMETER`).

## Solution: Flex BCM Config

Created `th-wedge100s-32x-flex.config.bcm` following the pattern from Arista 7060CX-32S
and Dell Z9100 platforms. For each 4-lane 100G port, the flex config pre-allocates
sub-ports:

```
portmap_50.0=53:100        # Parent port (100G default, 4 lanes)
portmap_51.0=54:25:i       # Sub-port 1 (25G, inactive by default)
portmap_52.0=55:25:50:i    # Sub-port 2 (25G or 50G, inactive)
portmap_53.0=56:25:i       # Sub-port 3 (25G, inactive)
```

Key changes:
- `.0` suffix on all per-port settings (portmap, serdes_preemphasis, xgxs_*_lane_map)
- 3 sub-port entries per parent with `:i` (inactive) flag
- `:50` on lane 2 sub-ports to support 2x50G mode
- Extended `pbmp_xport_xe` to cover logical ports up to 133

## Files Created/Modified

| File | Location | Purpose |
|---|---|---|
| `th-wedge100s-32x-flex.config.bcm` | hwsku dir | Flex BCM config with sub-port allocations |
| `sai.profile` | hwsku dir | Updated to reference flex config |
| `port_breakout_config_db.json` | `/etc/sonic/` | Default PORT config for breakout sub-ports |

## Verification (verified on hardware 2026-03-03)

- All 100G ports continue to work (Ethernet16, 32, 48, 112 all up)
- PortChannel1 (Ethernet16 + Ethernet32) remains up
- Live DPB via `config interface breakout` works without reboot:
  ```
  sudo config interface breakout Ethernet64 '4x25G[10G]' -y -f -l
  sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l
  ```
- Port 21 (Ethernet80-83): 4x25G, Ethernet80/81 link up, transceiver present
- Port 17 (Ethernet64-67): 4x25G, all admin up but transceiver not detected
- `config save` persists breakout config

## Port 17 Transceiver Issue

Ethernet64 (port 17) shows "Not present" for transceiver — needs separate investigation.
This was already the case before breakout (at 100G too), so it's likely a physical
seating issue with the QSFP, not a software problem.

## Reverting Breakout

To revert a port back to 100G:
```
sudo config interface breakout Ethernet64 '1x100G[40G]' -y -f -l
sudo config save
```
