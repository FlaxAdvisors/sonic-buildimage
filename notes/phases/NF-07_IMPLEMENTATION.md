# NF-07 — Autoneg & FEC: IMPLEMENTATION

## What Was Built

No platform-specific files were created for autoneg or FEC. These go through the standard
SONiC portmgrd → orchagent → BCM SAI chain.

The only platform-level artifact is the BCM config parameter `phy_an_c73=0x0` in
`th-wedge100s-32x-flex.config.bcm`, which is a deliberate design choice from the
original ONL/Arista implementation of this hardware.

## FEC Implementation — How It Flows

```
config interface fec Ethernet0 rs
  → CONFIG_DB PORT|Ethernet0  fec=rs
  ↓ portmgrd
  → APP_DB PORT_TABLE:Ethernet0  fec=rs
  ↓ orchagent
  → SAI create_attribute SAI_PORT_ATTR_FEC_MODE = SAI_PORT_FEC_MODE_RS
  ↓ syncd → BCM SDK
  → ASIC_DB ASIC_STATE:SAI_OBJECT_TYPE_PORT:oid:...  SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_RS
```

### FEC modes supported by this SAI (verified on hardware 2026-03-02):

| Mode | CLI value | ASIC_DB value | Notes |
|---|---|---|---|
| RS-FEC CL91 | `rs` | `SAI_PORT_FEC_MODE_RS` | Required for 100G-CR4 to Arista |
| No FEC | `none` | `SAI_PORT_FEC_MODE_NONE` | Loopback, testing |
| FC-FEC CL74 | `fc` | N/A | **Rejected** — not in `['none', 'rs']` |

FC-FEC rejection is enforced by SONiC CLI validation, not by SAI. The error message is:
`Error: Invalid value for 'fec': 'fc' is not one of 'none', 'rs'.`

## Autoneg Implementation — How It Flows

```
config interface autoneg Ethernet0 enabled
  → CONFIG_DB PORT|Ethernet0  autoneg=on
  ↓ portmgrd
  → APP_DB PORT_TABLE:Ethernet0  autoneg=on
  ↓ orchagent
  → SAI create_attribute SAI_PORT_ATTR_AUTO_NEG_MODE = true
  ↓ syncd → BCM SDK
  → ASIC_DB: SAI_PORT_ATTR_AUTO_NEG_MODE=true (SAI writes it)
     BUT: BCM SDK internally: "autoneg speed workaround failed / Feature not initialized"
     Because: phy_an_c73=0x0 prevents CL73 AN at the firmware layer
```

### Autoneg ASIC behavior (updated 2026-03-13):

Contrary to earlier testing, the SAI now DOES write `SAI_PORT_ATTR_AUTO_NEG_MODE=true` to
ASIC_DB when autoneg is enabled. However:
1. Hardware AN is still non-functional (BCM SDK logs the workaround failure)
2. The attribute CANNOT be cleared back to `false` when autoneg is disabled
3. Hardware BCM `ps` output shows `KR4` (static, no AN flag) — confirming no hardware AN

This means the test `test_autoneg_not_applied_in_asic_db` from the original phase-15 notes
is now `test_autoneg_programs_asic_db` — it checks that ASIC_DB IS written (not skipped)
but documents the hardware limitation.

## Supported Speeds (STATE_DB)

STATE_DB `PORT_TABLE|Ethernet0` `supported_speeds` = `40000,100000`

These are populated by SAI at init based on the BCM port configuration. 25G is not supported
as a speed on 100G parent ports (only on sub-ports after breakout).

## Comparison with Other Accton Platforms

| Platform | phy_an_c37 | phy_an_c73 | AN hardware status |
|---|---|---|---|
| Wedge 100S-32X | 0x3 (enabled) | 0x0 (disabled) | Non-functional |
| AS7712-32X | Not set | Not set | BCM defaults (may work) |
| AS7716-32X | Not set | Not set | BCM defaults |
| AS7312-54X | Not set | Not set | BCM defaults |

The explicit `phy_an_c73=0x0` is unique to the Wedge 100S-32X BCM config.

## Hardware-Verified Facts

- verified on hardware 2026-03-02: RS-FEC accepted, ASIC_DB = SAI_PORT_FEC_MODE_RS
- verified on hardware 2026-03-02: No-FEC accepted, ASIC_DB = SAI_PORT_FEC_MODE_NONE
- verified on hardware 2026-03-02: FC-FEC rejected by CLI with "not in" error
- verified on hardware 2026-03-02: autoneg CLI accepted, CONFIG_DB = on, APP_DB = on
- verified on hardware 2026-03-13: ASIC_DB SAI_PORT_ATTR_AUTO_NEG_MODE now written as `true`
- verified on hardware 2026-03-02: RS-FEC on Ethernet16/32/48/112 brings links UP immediately
- verified on hardware 2026-03-02: BCM `ps` shows KR4 (no AN) on all ports
- verified on hardware 2026-03-02: 18/18 stage 15 tests pass

## Remaining Known Gaps

- **autoneg ASIC_DB cannot be cleared**: Once set, ASIC_DB AUTO_NEG_MODE stays `true` even
  after `config interface autoneg disabled`. This is a SAI bug; workaround is to restart syncd.
- **FC-FEC on 25G sub-ports untested**: After 4x25G breakout, FC-FEC behavior on sub-ports
  is unknown. The SAI may accept it (FC-FEC CL74 is the standard for 25G), but
  `fc` on short DAC cables brought links down in limited testing.
- **adv_speeds no-op on hardware**: `config interface advertised-speeds` stores values in DB
  but they are never transmitted as CL73 AN advertisement. Documented in BEWARE_OPTICS.md.
