# NF-05 — Speed Change: PLAN

## Problem Statement

SONiC operators must be able to change port speed via `config interface speed`. This is
a basic operational requirement and a prerequisite for DPB (NF-06). The change must
propagate through CONFIG_DB → APP_DB → ASIC_DB without crashing syncd.

On Tomahawk with the static BCM config (`th-wedge100s-32x-flex.config.bcm`), all serdes
lanes are initialized at 100G at boot time. A speed change via SAI tells the BCM SDK to
reconfigure serdes at runtime. Whether this results in an actual hardware speed change
depends on whether the new speed is supported by the active BCM port configuration.

## Proposed Approach

1. Verify `platform.json` defines valid speeds per port (used by `config interface speed`
   to validate input).
2. Test speed change on a non-production port (Ethernet0, which has no active peer).
3. Verify DB pipeline propagation: CONFIG_DB, APP_DB, `show interfaces status`.
4. Restore to 100G.
5. Avoid changing LAG-member ports (Ethernet16, Ethernet32) during this test — LACP
   will deselect them if their speed changes.

## Files to Change

| File | Action |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/platform.json` | Create — defines valid speeds per port for SONiC CLI validation |

## Acceptance Criteria

- `config interface speed Ethernet0 40000` completes with rc=0
- CONFIG_DB `PORT|Ethernet0` speed = `40000`
- APP_DB `PORT_TABLE:Ethernet0` speed = `40000`
- `show interfaces status Ethernet0` shows speed column = `40G`
- `config interface speed Ethernet0 100000` restores to 100G
- syncd does not crash during either change

## Risks and Watch-Outs

- **BCM hardware does not dynamically reconfigure serdes**: The static BCM config locks
  serdes at 100G. `config interface speed 40000` is accepted by SAI and propagated to DB,
  but BCM `ps` shows the port still at 100G in hardware. The speed change is a "soft" change
  only — actual link speed is determined at syncd init from the BCM config.
- **Do not test on LAG members**: Ethernet16 and Ethernet32 are in PortChannel1. Speed change
  on a LAG member while LACP is active will deselect it, causing brief traffic interruption.
- **40G is the only valid alternative speed**: platform.json defines `1x100G[40G]` as the
  default breakout mode. Supported speeds: 40000 and 100000. Speed 25000 is only valid on
  sub-ports after 4x25G breakout.
- **platform.json required for CLI validation**: Without platform.json, `config interface speed`
  may accept invalid speeds or fail entirely.
