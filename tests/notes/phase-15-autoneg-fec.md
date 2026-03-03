# Phase 15 — Auto-Negotiation & FEC Configuration

*Hardware-verified 2026-03-02 on hare-lorax (Wedge 100S-32X, SONiC).*

## Summary

FEC configuration works end-to-end through ASIC_DB. Auto-negotiation
configuration is accepted by CONFIG_DB/APP_DB but the Broadcom SAI on
this Tomahawk does not actually program it into hardware.

## FEC Findings

- **RS-FEC (CL91)**: `config interface fec <port> rs` — works (verified on hardware 2026-03-02)
  - CONFIG_DB: `fec=rs` ✅
  - APP_DB: `fec=rs` ✅
  - ASIC_DB: `SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_RS` ✅
  - Required for 100GBASE-CR4 DAC links to Arista EOS

- **No FEC**: `config interface fec <port> none` — works
  - ASIC_DB: `SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_NONE` ✅

- **FC-FEC (CL74)**: `config interface fec <port> fc` — **REJECTED**
  - Error: `fec fc is not in ['none', 'rs']`
  - FC-FEC (FireCode) is for 25G/10G SerDes — not applicable in 100G-only config
  - The Tomahawk SAI restricts FEC to `rs` and `none` for 100G ports

## Auto-Negotiation Findings

- **CLI acceptance**: `config interface autoneg <port> enabled` → rc=0 ✅
- **CONFIG_DB propagation**: `autoneg=on` ✅
- **APP_DB propagation**: `autoneg=on` ✅
- **ASIC_DB**: `SAI_PORT_ATTR_AUTO_NEG_MODE` stays `false` ⚠️
  - SAI accepts the config but does NOT program AN into hardware
  - No errors in syncd or swss logs — silently ignored

- **`config interface autoneg <port> disabled`** → rc=0, CONFIG_DB `autoneg=off` ✅

- **`show interfaces autoneg status`** → correctly shows `enabled`/`disabled`/`N/A` ✅

### Why AN Doesn't Work at ASIC Level

1. BCM config has `phy_an_c73=0x0` (Clause 73 AN disabled at firmware level)
2. `phy_an_c37=0x3` (Clause 37 enabled — but CL37 is for 1G Ethernet, irrelevant for QSFP28)
3. The Broadcom SAI returns success for `SAI_PORT_ATTR_AUTO_NEG_MODE=true` but
   doesn't change hardware behavior when CL73 is disabled in the BCM config
4. This is the same behavior across all Accton Tomahawk platforms — AS7712/AS7716
   don't even set `phy_an_c73` (they use BCM SDK defaults)

### Comparison with Other Platforms

| Platform | phy_an_c37 | phy_an_c73 | Notes |
|---|---|---|---|
| Wedge 100S-32X | 0x3 (enabled) | 0x0 (disabled) | Explicit C73 disable |
| AS7712-32X | Not set | Not set | Uses BCM defaults |
| AS7716-32X | Not set | Not set | Uses BCM defaults |
| AS7312-54X | Not set | Not set | Uses BCM defaults |
| Celestica SeaStone | Not set | Not set | Uses phy_an_lt_msft=1 instead |

## Advertised Speeds

- **Supported speeds** (STATE_DB): `40000,100000` (40G and 100G)
- `config interface advertised-speeds <port> 40000,100000` → accepted ✅
  - CONFIG_DB: `adv_speeds=40000,100000` ✅
  - APP_DB: `adv_speeds=40000,100000` ✅
  - `show interfaces autoneg status` shows `40G,100G` ✅
- `config interface advertised-types <port> CR4` → accepted ✅
  - CONFIG_DB: `adv_interface_types=CR4` ✅

Note: adv_speeds and adv_interface_types only take effect when autoneg=on,
and since AN is not applied at ASIC level, these are effectively no-ops
on this platform.

## Pytest Results

```
stage_15_autoneg_fec/test_autoneg_fec.py — 18/18 passing (54.81s)

  TestFecConnectedPorts:
    test_connected_ports_fec_rs_in_config_db   PASSED
    test_connected_ports_fec_rs_in_asic_db     PASSED

  TestFecConfig:
    test_fec_rs_accepted                       PASSED
    test_fec_none_accepted                     PASSED
    test_fec_fc_rejected                       PASSED

  TestAutonegConfig:
    test_autoneg_enable_accepted               PASSED
    test_autoneg_enable_propagates_to_config_db PASSED
    test_autoneg_enable_propagates_to_app_db   PASSED
    test_autoneg_not_applied_in_asic_db        PASSED
    test_autoneg_disable_accepted              PASSED
    test_show_autoneg_status                   PASSED

  TestAdvertisedSpeeds:
    test_supported_speeds_in_state_db          PASSED
    test_advertised_speeds_accepted            PASSED
    test_advertised_speeds_shown_in_cli        PASSED
    test_advertised_types_accepted             PASSED

  TestDefaultState:
    test_default_autoneg_is_not_set            PASSED
    test_default_asic_autoneg_false            PASSED
    test_connected_ports_autoneg_status        PASSED
```

## Recommendations

1. **Do NOT enable autoneg on connected ports** — with SAI not programming it,
   it adds CONFIG_DB fields that serve no purpose and may confuse operators

2. **Keep RS-FEC explicit** — since AN doesn't negotiate FEC, `fec=rs` must be
   manually configured on all 100GBASE-CR4 links to Arista

3. **No BCM config change needed** — changing `phy_an_c73` to enable CL73 at
   the firmware level is risky without extensive testing with different peer
   equipment and optic types. The current static configuration is stable.

4. **FC-FEC for breakout ports** — if 4x25G breakout is deployed (Phase 14b),
   FC-FEC (CL74) may be needed for 25G links. This will require testing
   whether the SAI allows `fc` on 25G sub-ports (it may only be blocked
   on 100G parent ports).
