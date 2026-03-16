# NF-01 — BCM Config: PLAN

## Problem Statement

The BCM56960 (Tomahawk) ASIC requires a `.config.bcm` file to boot correctly. This file
defines every active port's physical-to-logical lane mapping (`portmap_N`), serdes
pre-emphasis, lane polarity/inversion maps, and switch-wide parameters (MMU, oversubscription,
autoneg mode). Without it, syncd cannot initialize any ports.

Two configs are needed:
- **Fixed 100G** — 32 active 100G ports, no sub-port allocations. Simple, no DPB support.
- **Flex** — same 32 ports, but each has three inactive sub-port records (`:i` suffix) that
  allow SAI to carve 4×25G or 2×50G breakouts without restarting syncd from scratch.

`sai.profile` selects which config file SAI loads at syncd init.

## Proposed Approach

1. Place both BCM config files in the HWSKU directory:
   `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/`

2. Derive `portmap_N` entries from Arista EOS's `show platform trident interface map`
   output on the same hardware. This gives authoritative physical lane IDs.

3. Derive lane polarity maps (`xgxs_rx_lane_map_N`, `xgxs_tx_lane_map_N`) from the same
   EOS source (EOS writes identical BCM SDK config for this SKU).

4. Copy `serdes_preemphasis_N` values from the ONL wedge100s-32x port (sfpi.c references).

5. Set `sai.profile` to point at the flex config so DPB is always available.

## Files to Change

| File | Action |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` | Create (primary) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile` | Create — point at flex config |

## Acceptance Criteria

- `syncd` initializes without errors in `docker logs syncd`
- `show interfaces status` lists all 32 Ethernet ports
- All admin-up ports show speed=100G in `show interfaces status`
- BCM `ps` command (via `docker exec syncd bcmcmd "ps"`) shows 100G, FD, KR4 for active ports
- Ports connected to peer with RS-FEC configured come up (oper=up)

## Risks and Watch-Outs

- **portmap ordering matters**: The BCM port number (left side of `portmap_N`) must be
  consecutive within each 4-lane group, or SAI will reject port creation.
- **Gap at logical port 33 and 66–67**: Tomahawk reserves these for internal CPU ports.
  The portmap must skip them (no `portmap_33` entry).
- **serdes_preemphasis tuning**: Wrong values cause CDR not to lock on DAC cables. Pre-emphasis
  of `0x205000` (main=80, pre=0, post=0) works for most ports; copper pigtail ports may need
  `0x284800` (slightly more pre-cursor). Wrong values cause link to stay down even after
  RS-FEC is configured.
- **phy_an_c73=0x0**: CL73 autoneg is explicitly disabled. This means FEC must be configured
  statically; it is not auto-negotiated. Do not enable CL73 without extensive testing.
- **flex config required for DPB**: The fixed 100G config (`portmap_N=lane:100` with no
  sub-ports) causes orchagent SIGABRT when DPB is attempted. Always use flex config.
- **pbmp_xport_xe bitmask**: Must cover all logical port numbers including sub-ports up to 133.
  Incorrect bitmask causes SAI to ignore some ports entirely.
