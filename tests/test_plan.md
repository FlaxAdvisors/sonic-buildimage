# Wedge 100S-32X Test Plan

## Physical Port Population (as of 2026-03-17)

| SONiC Port | Physical Port | Connected To | Cable Type | Notes |
|------------|---------------|--------------|------------|-------|
| Ethernet0  | Port 1        | (empty)      | —          | No module |
| Ethernet16 | Port 5        | rabbit-lorax Et13/1 | DAC 100G | LAG member |
| Ethernet32 | Port 9        | rabbit-lorax Et14/1 | DAC 100G | LAG member |
| Ethernet48 | Port 13       | rabbit-lorax Et15/1 | DAC 100G | Standalone |
| Ethernet64 | Port 17       | (empty)      | QSFP28 present | No link |
| Ethernet80 | Port 21       | (empty)      | QSFP28 present | No link |
| Ethernet104 | Port 27      | (empty)      | CWDM4 optical | Blocked (§9) |
| Ethernet108 | Port 28      | (empty)      | CWDM4 optical | Blocked (§9) |
| Ethernet112 | Port 29      | rabbit-lorax Et16/1 | DAC 100G | Standalone |

## Peer Node: rabbit-lorax (Arista EOS Wedge 100S)

- Access: jump via hare-lorax (192.168.88.12) → 192.168.88.14
- PortChannel1: Et13/1 + Et14/1 in LACP active, IP 10.0.1.0/31
- Et15/1: standalone, IP 10.0.0.0/31
- Et16/1: standalone (no IP assigned)

## Per-Stage Requirements

| Stage | Requires | Configures | Unconfigures |
|-------|----------|------------|--------------|
| 01–10 | none | none | none |
| 11    | pmon running | none | none |
| 12    | syncd running | flex counter enable (if off) | restore |
| 13    | RS-FEC, link-up ports | RS-FEC on Et16/32/48/112 | remove FEC |
| 14    | breakout support | 4x25G on one port | restore 1x100G |
| 15    | FEC modes | RS-FEC then none on Et48 | restore FEC=none |
| 16    | LACP peer | PortChannel1, members, IP | remove PortChannel1 |
| 17    | restore done | none | none |
| 18    | pre-test ran | restore from snapshot | none |
| 19    | platform API | none | none |
| 20    | PortChannel1 up, traffic path | none (uses stage_16 state) | none |
