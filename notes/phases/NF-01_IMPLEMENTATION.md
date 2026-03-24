# NF-01 — BCM Config: IMPLEMENTATION

## What Was Built

### Files Created

| File (repo-relative) | Description |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` | Primary BCM config (flex, sub-port capable) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile` | SAI init profile |

No fixed-100G config file exists at `th-wedge100s-32x100G.config.bcm` in the current repo
(the path was referenced in earlier notes but only the flex config is present).

### sai.profile Contents

```
SAI_INIT_CONFIG_FILE=/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm
SAI_NUM_ECMP_MEMBERS=64
```

### BCM Flex Config — Key Parameters

**Switch-wide settings:**
- `ctr_evict_enable=0x0` — disable counter eviction
- `l2_mem_entries=0x8000` (32k), `l3_mem_entries=0x4000` (16k)
- `oversubscribe_mode=0x1` — oversubscription enabled
- `parity_enable=0x1`
- `os=unix`
- `phy_an_c73=0x0` — **CL73 autoneg disabled** (must configure FEC statically)
- `phy_an_c37=0x3` — CL37 enabled (irrelevant for QSFP28)
- `serdes_firmware_mode_xe=0x2`
- `serdes_automedium=0x0`, `serdes_fiber_pref=0x1`
- `stable_size=0x6000000`
- `pbmp_xport_xe=0x3ffff...e` — covers all 133 logical ports

**portmap format (flex):**
```
portmap_50.0=53:100        # Parent: BCM logical port 50, physical lane 53, 100G
portmap_51.0=54:25:i       # Sub-port: lane 54, 25G, inactive
portmap_52.0=55:25:50:i    # Sub-port: lane 55, 25G or 50G, inactive
portmap_53.0=56:25:i       # Sub-port: lane 56, 25G, inactive
```

The `.0` suffix on every key is required for flex mode. Without it SAI cannot allocate
sub-ports dynamically.

**Sample portmap entries (physical lane derivation from EOS):**
| BCM logical | Physical lane | SONiC port | EOS panel port |
|---|---|---|---|
| 1 | 5 | Ethernet16 | Et5/1 |
| 5 | 1 | Ethernet20 | Et6/1 |
| 29 | 25 | Ethernet44 | Et12/1 |
| 34 | 37 | Ethernet48 | Et13/1 |
| 102 | 101 | Ethernet112 | Et29/1 |
| 118 | 117 | Ethernet0 | Et1/1 |

**Lane polarities** (`xgxs_rx_lane_map_N`, `xgxs_tx_lane_map_N`):
- Values of `0x3210` = natural (lane 0→0, 1→1, 2→2, 3→3)
- Values of `0x1032` = swap lane-pairs (0↔1, 2↔3)
- Values of `0x2301`, `0x0123`, `0x213` — various other rotations for specific ports
- Each physical port group has independent RX and TX lane maps, reflecting board-level
  signal routing between QSFP cages and ASIC die.

**serdes_preemphasis encoding** (24-bit: `0xPPMMSS`):
- `PP` = post-cursor, `MM` = main, `SS` = pre-cursor
- `0x205000` (main=80, pre=0, post=0) — majority of front-panel ports (DAC/optical)
- `0x284800` (main=72, pre=0x28=40 post-cursor?) — select port groups (88–99 range)
- `0x284008` (main=64, post=8, pre=8) — first 16 ports (BCM logical 1–16)
- `0x2c4004`, `0x303c04` — rear quadrants (102–133 range)

## Key Decisions

1. **Flex config chosen as the only deployed config**: `sai.profile` points at flex.
   The fixed-100G variant was used temporarily during initial bring-up and abandoned once
   DPB was needed. Using flex config for all deployments avoids having to switch configs
   if DPB is ever attempted.

2. **Lane maps derived from EOS hardware**: Arista EOS on the same Wedge 100S-32X
   hardware runs an identical BCM SDK init sequence. The Physical IDs from
   `show platform trident interface map` are authoritative for this board revision.
   All 32 port lane assignments verified against port_config.ini (32/32 match,
   verified on hardware 2026-03-13).

3. **phy_an_c73=0x0 preserved**: Other Accton TH platforms (AS7712, AS7716) omit this
   key and rely on BCM SDK defaults. The Wedge 100S uses explicit disable to match ONL
   behavior. Changing it would require retesting all port types (DAC, AOC, SR4, CWDM4).

4. **SAI_NUM_ECMP_MEMBERS=64**: Standard value across all Accton broadcom platforms.

## Hardware-Verified Facts

- verified on hardware 2026-03-02: syncd initializes all 32 ports without errors
- verified on hardware 2026-03-02: BCM `ps` shows `100G FD KR4` for admin-up ports
- verified on hardware 2026-03-02: ports ce0, ce4, ce8, ce24 come up after RS-FEC configured
- verified on hardware 2026-03-03: flex config supports live 4x25G breakout on Ethernet64/80
- verified on hardware 2026-03-06: all 100G parent ports continue to work after flex config deployed

## Remaining Known Gaps

- **No th-wedge100s-32x100G.config.bcm in repo**: Only flex config exists. Fine for now,
  but a purely fixed-100G config (slightly simpler, no sub-port overhead) is not available
  for diagnostic use.
- **Ethernet104/108 serdes_preemphasis**: Values `0x284800` are tuned for copper. These
  ports have CWDM4 optical modules with a physical fiber break. Pre-emphasis should be
  `n1=0, m=80, p1=0` for optical but this does not fix the physical issue (see BEWARE_OPTICS.md).
- **media_settings.json absent**: Per-port optical tuning is not implemented. All ports use
  the static pre-emphasis from the BCM config.
