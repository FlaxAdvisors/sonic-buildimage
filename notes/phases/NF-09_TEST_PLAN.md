# NF-09 — LLDP: TEST PLAN

## Mapping

Current: tests are embedded in `tests/stage_13_link/test_link.py` as
`test_lldp_neighbors_on_connected_ports` and `test_lldp_neighbor_port_mapping`.

Proposed dedicated stage (not yet created): `tests/stage_18_lldp/test_lldp.py`

## Dependencies

- **NF-04 (Link Status)**: LLDP requires oper-up ports. At least Ethernet48 and Ethernet112
  must be up (they are standalone — not in PortChannel1 — so they are simpler to verify).
- **NF-08 (PortChannel)**: Ethernet16 and Ethernet32 are LAG members. LLDP on these
  ports requires PortChannel1 to be operational.

## Required Hardware State

- lldp container running
- At least 2 front-panel ports oper=up with LLDP-capable peer (rabbit-lorax)
- RS-FEC configured on connected ports (from NF-04)

## Step-by-Step Test Actions

### 1. Verify lldp container is running

```bash
docker ps --format '{{.Names}}' --filter name=lldp
```

**Pass**: output contains `lldp`

### 2. show lldp neighbors exits 0

```bash
show lldp neighbors
```

**Pass**: rc=0, non-empty output

### 3. Verify rabbit-lorax appears as neighbor

```bash
show lldp neighbors | grep -i rabbit-lorax
```

**Pass**: at least one line matching `rabbit-lorax`

### 4. Verify all 4 connected ports have LLDP neighbors

For each port in {Ethernet16, Ethernet32, Ethernet48, Ethernet112}:

Check that the port name appears in `show lldp neighbors` output.

**Pass**: all 4 ports have LLDP entries
**Skip condition**: port not oper=up (test skips that port, does not fail)

### 5. Verify peer port IDs match expected topology

Expected mapping:
| SONiC port | Expected EOS port ID |
|---|---|
| Ethernet16 | Ethernet13/1 |
| Ethernet32 | Ethernet14/1 |
| Ethernet48 | Ethernet15/1 |
| Ethernet112 | Ethernet16/1 |

Parse LLDP output by interface section. For each present port, check that the expected
EOS port ID string appears in that interface's section.

**Pass**: expected port ID found in section for each present port

### 6. Verify LLDP TTL is reasonable

For any neighbor entry, TTL > 0 and TTL <= 120 (Arista default is 120s).

**Pass**: at least one neighbor shows TTL in range [1, 120]

### 7. Verify lldp container has not restarted recently

```bash
docker ps --format '{{.Status}}' --filter name=lldp
```

**Pass**: Status shows uptime in days or hours (not seconds)

## Pass/Fail Criteria — Summary

| Check | Expected |
|---|---|
| lldp container running | yes |
| `show lldp neighbors` rc | 0 |
| `rabbit-lorax` in output | yes |
| Ethernet48 has LLDP neighbor | yes (standalone port, simpler) |
| Ethernet112 has LLDP neighbor | yes (standalone port) |
| Ethernet16/32 have LLDP neighbor | yes (LAG members — LLDP on physical port) |
| Et13/1 in Ethernet16 section | yes |
| Et14/1 in Ethernet32 section | yes |
| Et15/1 in Ethernet48 section | yes |
| Et16/1 in Ethernet112 section | yes |
| Neighbor TTL | in [1, 120] |

## State Changes and Restoration

No state changes. LLDP is passive discovery — reading `show lldp neighbors` makes no
modifications. No teardown required.

## Notes on LLDP with LAG Members

SONiC runs lldpd on physical interfaces. Ethernet16 and Ethernet32 are physical ports
that happen to also be PortChannel1 members. LLDP frames are sent/received on the
physical netdev (Ethernet16) independently of the bond (PortChannel1 netdev).

Verified on hardware: LLDP neighbors visible on Ethernet16 and Ethernet32 even while
they are active PortChannel1 members.

## Notes on Output Parsing

`show lldp neighbors` output format:
```
Interface:    Ethernet16,      ← trailing comma on interface line
  ChassisID:  rabbit-lorax
  PortID:     Ethernet13/1
  TTL:        120
```

Regex for interface match: `r"Interface:\s+(\S+?)(?:,|\s)"` (non-greedy, strips comma).
