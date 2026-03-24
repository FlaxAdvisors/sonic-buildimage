# NF-07 — Autoneg & FEC: PLAN

## Problem Statement

SONiC provides CLI commands to configure port-level FEC and autoneg:
- `config interface fec <port> rs|fc|none`
- `config interface autoneg <port> enabled|disabled`

For the Wedge 100S-32X (Tomahawk BCM56960), these interact with the BCM SAI in
non-obvious ways that differ from other platforms:

**FEC:**
- RS-FEC (CL91): supported, required for 100GBASE-CR4 links to Arista EOS
- FC-FEC (CL74): rejected by SONiC CLI with `fec fc is not in ['none', 'rs']`
- No FEC: supported

**Autoneg:**
- CLI accepts `config interface autoneg enabled` (rc=0)
- CONFIG_DB and APP_DB propagate `autoneg=on`
- ASIC_DB `SAI_PORT_ATTR_AUTO_NEG_MODE` is written `true` by SAI
- But: hardware AN is not functional — BCM config has `phy_an_c73=0x0`
- SAI logs "autoneg speed workaround failed / Feature not initialized" internally
- The attribute also cannot be cleared back to `false` once set (SAI bug)

Operators need documentation of this behavior to avoid misconfiguring production ports.

## Proposed Approach

1. Validate FEC via CONFIG_DB, APP_DB, ASIC_DB for a disconnected test port
2. Validate autoneg CLI acceptance while documenting the ASIC limitation
3. Verify FC-FEC rejection with correct error message
4. Verify RS-FEC is present on connected ports (prerequisite for NF-04)

No platform code changes are needed — FEC and autoneg go through standard SONiC SAI path.

## Files to Change

None. FEC and autoneg are handled by portmgrd, orchagent, and the BCM SAI.

## Acceptance Criteria

- `config interface fec Ethernet0 rs` → rc=0, ASIC_DB = `SAI_PORT_FEC_MODE_RS`
- `config interface fec Ethernet0 none` → rc=0, ASIC_DB = `SAI_PORT_FEC_MODE_NONE`
- `config interface fec Ethernet0 fc` → rc!=0 or output contains "not in"
- `config interface autoneg Ethernet0 enabled` → rc=0, CONFIG_DB = `on`, ASIC_DB = `true`
- `show interfaces autoneg status Ethernet0` → shows `enabled`
- Connected ports (Ethernet16/32/48/112): fec=rs in CONFIG_DB and ASIC_DB

## Risks and Watch-Outs

- **RS-FEC mandatory for Arista DAC links**: Without `fec rs`, Arista reports FEC alignment
  failure and link stays down. Do not test on connected ports without saving RS-FEC first.
- **autoneg in ASIC_DB cannot be cleared**: Once `SAI_PORT_ATTR_AUTO_NEG_MODE=true` is set,
  setting autoneg=disabled does not clear it in ASIC_DB. This is a known SAI limitation.
  Tests for "autoneg not applied" should use a fresh port that has never had autoneg set.
- **adv_speeds and adv_interface_types are no-ops**: These only take effect when hardware
  AN is working. Since `phy_an_c73=0x0` disables CL73 AN at the BCM layer, advertised
  speeds are stored in DB but never transmitted to peers.
- **Do not change BCM config phy_an_c73**: Enabling CL73 would affect all 32 ports and
  change FEC negotiation behavior. Untested risk.
