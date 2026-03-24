# NF-09 — LLDP: IMPLEMENTATION

## What Was Built

No platform-specific files were created. LLDP is standard SONiC infrastructure.

## LLDP Infrastructure

The `lldp` container runs `lldpd` with the following SONiC integration:
- Port names are mapped from Linux netdev names to SONiC aliases
- lldpsyncd writes neighbor information to LLDP_ENTRY_TABLE in APP_DB
- `show lldp neighbors` reads from APP_DB

No Wedge 100S-32X customization was required. The standard SONiC lldp container
works once front-panel links are up.

## Observed Topology (verified on hardware 2026-03-02)

### Management Interface (eth0)

- Neighbor: `turtle-lorax` on `swp23` (TTL=120)
- This is the management switch / OOB network
- Appears in `show lldp table` (not `show lldp neighbors` which shows front-panel only)

### Front-Panel Interfaces

After RS-FEC was configured and links came up on 4 ports:

| SONiC Interface | EOS Neighbor | EOS Port | LLDP Chassis |
|---|---|---|---|
| Ethernet16 | rabbit-lorax | Ethernet13/1 | rabbit-lorax |
| Ethernet32 | rabbit-lorax | Ethernet14/1 | rabbit-lorax |
| Ethernet48 | rabbit-lorax | Ethernet15/1 | rabbit-lorax |
| Ethernet112 | rabbit-lorax | Ethernet16/1 | rabbit-lorax |

TTL: 120 seconds (Arista default LLDP hold time).

### LAG Member Ports Note

Ethernet16 and Ethernet32 are PortChannel1 members. LLDP on these ports was confirmed
working (neighbors visible) despite being LAG members. SONiC lldpd is configured to
transmit/receive LLDP on physical interfaces, not on the bond/portchannel netdev.

## LLDP Parse Note (test bug fixed 2026-03-02)

The `show lldp neighbors` output for a single interface looks like:
```
Interface:    Ethernet16,
  ChassisID:  rabbit-lorax
  PortID:     Ethernet13/1
  TTL:        120
```

The interface line has a trailing comma: `Ethernet16,`. An earlier regex
`(\S+)` captured `Ethernet16,` (with comma), causing test failures. Fixed to
`(\S+?)(?:,|\s)` — the non-greedy capture followed by comma-or-whitespace.

## Hardware-Verified Facts

- verified on hardware 2026-03-02: lldp container functional from initial SONiC install
- verified on hardware 2026-03-02: LLDP neighbors on eth0 (turtle-lorax)
- verified on hardware 2026-03-02: no front-panel LLDP neighbors before links came up (expected)
- verified on hardware 2026-03-02: all 4 front-panel LLDP neighbors discovered after link-up
- verified on hardware 2026-03-02: rabbit-lorax identified as neighbor on Ethernet16/32/48/112
- verified on hardware 2026-03-02: peer port IDs Et13/1, Et14/1, Et15/1, Et16/1 correct
- verified on hardware 2026-03-02: LLDP works on LAG member ports (Ethernet16, Ethernet32)

## Remaining Known Gaps

- **No dedicated stage_18_lldp test file**: LLDP tests live in `stage_13_link/test_link.py`
  as `test_lldp_neighbors_on_connected_ports` and `test_lldp_neighbor_port_mapping`.
  The NF-09 test plan below defines what a standalone stage would look like.
- **Ethernet104/108 CWDM4 ports**: No LLDP neighbors (links down — physical issue).
- **LLDP on PortChannel interface**: SONiC does not run LLDP on the portchannel netdev
  itself, only on physical member ports. `show lldp neighbors PortChannel1` returns nothing.
