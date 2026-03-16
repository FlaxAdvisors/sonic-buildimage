# NF-09 — LLDP: PLAN

## Problem Statement

SONiC's LLDP service (`lldpd` inside the `lldp` container) advertises local port and
chassis information and collects neighbor information from peers. This is used for:
- Topology discovery and verification
- Port-level peer identification without manual cable documentation
- Debugging link issues (peer can see our chassis/port even if link is up/down)

For the Wedge 100S-32X, LLDP requires no platform-specific code. The `lldp` container
runs standard `lldpd` with SONiC's configuration. The only hardware requirement is
working front-panel links.

This phase documents the expected LLDP topology and defines automated verification.

## Proposed Approach

No implementation required. Verification only:
1. `lldp` container is running
2. `show lldp neighbors` shows rabbit-lorax as neighbor on connected ports
3. LLDP port IDs match the expected Arista Et13–Et16 mapping

## Files to Change

None. LLDP is standard SONiC infrastructure.

## Acceptance Criteria

- `lldp` container is running (`docker ps`)
- `show lldp neighbors` exits 0
- rabbit-lorax appears as neighbor on Ethernet16, Ethernet32, Ethernet48, Ethernet112
- Neighbor port IDs: Et13/1, Et14/1, Et15/1, Et16/1 respectively
- Chassis ID matches rabbit-lorax's MAC address or system name

## Risks and Watch-Outs

- **Dependency on NF-04 (Link Status)**: LLDP can only exchange frames on oper-up ports.
  If Ethernet48 or Ethernet112 links are down, LLDP will not show neighbors on those ports.
- **Dependency on NF-08 (PortChannel)**: Ethernet16 and Ethernet32 are LAG members.
  LLDP on LAG members is suppressed by SONiC by default (lldpd bond handling). LLDP
  neighbors on PortChannel member ports may NOT appear unless lldpd is configured to
  allow it on individual members. Check what `show lldp neighbors` actually returns for
  LAG member ports vs. the PortChannel interface itself.
- **No dedicated test stage yet**: NF-09 tests are currently integrated into `stage_13_link/`
  as `test_lldp_neighbors_on_connected_ports` and `test_lldp_neighbor_port_mapping`.
  A separate `stage_18_lldp/` could be created if more comprehensive testing is needed.
- **rabbit-lorax LLDP configuration**: Arista EOS runs LLDP by default. No special EOS
  config should be needed, but if Et13-16 have `no lldp receive` or `no lldp transmit`,
  neighbors will not appear.
