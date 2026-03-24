# Port Config

## Hardware

- 32 × QSFP28 (100G) ports on a Broadcom BCM56960 (Tomahawk) ASIC
- All ports run at 100G with Reed-Solomon FEC (`fec rs`)
- Each port occupies 4 serdes lanes on the Tomahawk

**Port naming:**
- SONiC logical name: `Ethernet0`, `Ethernet4`, ... `Ethernet124` (4-lane step)
- EOS alias: `Ethernet1/1` through `Ethernet32/1`
- `port_config.ini` index: 1–32 (1-based, matches QSFP `Sfp` instantiation offset in `chassis.py`)

## Driver / Daemon

- `port_config.ini` is a static file read by `sonic-cfggen` and `portsyncd` at startup.
- No kernel driver or daemon is involved in port configuration itself.
- File location: `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini`

## Port Map

The lane assignments are non-sequential due to Tomahawk BCM56960 pipe-to-front-panel
routing. Ports alternate between two adjacent serdes groups (XOR-1 interleave pattern
also visible in presence bit mapping):

| SONiC Name | Lanes | Alias | Index | SFP bus |
|---|---|---|---|---|
| Ethernet0 | 117,118,119,120 | Ethernet1/1 | 1 | 3 |
| Ethernet4 | 113,114,115,116 | Ethernet2/1 | 2 | 2 |
| Ethernet8 | 125,126,127,128 | Ethernet3/1 | 3 | 5 |
| Ethernet12 | 121,122,123,124 | Ethernet4/1 | 4 | 4 |
| Ethernet16 | 5,6,7,8 | Ethernet5/1 | 5 | 7 |
| Ethernet20 | 1,2,3,4 | Ethernet6/1 | 6 | 6 |
| Ethernet24 | 13,14,15,16 | Ethernet7/1 | 7 | 9 |
| Ethernet28 | 9,10,11,12 | Ethernet8/1 | 8 | 8 |
| Ethernet32 | 21,22,23,24 | Ethernet9/1 | 9 | 11 |
| Ethernet36 | 17,18,19,20 | Ethernet10/1 | 10 | 10 |
| Ethernet40 | 29,30,31,32 | Ethernet11/1 | 11 | 13 |
| Ethernet44 | 25,26,27,28 | Ethernet12/1 | 12 | 12 |
| Ethernet48 | 37,38,39,40 | Ethernet13/1 | 13 | 15 |
| Ethernet52 | 33,34,35,36 | Ethernet14/1 | 14 | 14 |
| Ethernet56 | 45,46,47,48 | Ethernet15/1 | 15 | 17 |
| Ethernet60 | 41,42,43,44 | Ethernet16/1 | 16 | 16 |
| Ethernet64 | 53,54,55,56 | Ethernet17/1 | 17 | 19 |
| Ethernet68 | 49,50,51,52 | Ethernet18/1 | 18 | 18 |
| Ethernet72 | 61,62,63,64 | Ethernet19/1 | 19 | 21 |
| Ethernet76 | 57,58,59,60 | Ethernet20/1 | 20 | 20 |
| Ethernet80 | 69,70,71,72 | Ethernet21/1 | 21 | 23 |
| Ethernet84 | 65,66,67,68 | Ethernet22/1 | 22 | 22 |
| Ethernet88 | 77,78,79,80 | Ethernet23/1 | 23 | 25 |
| Ethernet92 | 73,74,75,76 | Ethernet24/1 | 24 | 24 |
| Ethernet96 | 85,86,87,88 | Ethernet25/1 | 25 | 27 |
| Ethernet100 | 81,82,83,84 | Ethernet26/1 | 26 | 26 |
| Ethernet104 | 93,94,95,96 | Ethernet27/1 | 27 | 29 |
| Ethernet108 | 89,90,91,92 | Ethernet28/1 | 28 | 28 |
| Ethernet112 | 101,102,103,104 | Ethernet29/1 | 29 | 31 |
| Ethernet116 | 97,98,99,100 | Ethernet30/1 | 30 | 30 |
| Ethernet120 | 109,110,111,112 | Ethernet31/1 | 31 | 33 |
| Ethernet124 | 105,106,107,108 | Ethernet32/1 | 32 | 32 |

**SFP bus** column shows the I2C bus number from `_SFP_BUS_MAP` in `sfp.py` (the
physical mux-tree bus used by the daemon and as optoe sysfs fallback).

**Index alignment:** `chassis.py` prepends a `None` sentinel at `_sfp_list[0]` so that
`get_sfp(index)` with a 1-based `index` from `port_config.ini` maps correctly to
`Sfp(index - 1)`.

## Python API

There is no runtime Python API class for port config. The file is consumed statically
by SONiC infrastructure (`sonic-cfggen`, `portsyncd`). The `index` column determines
how `xcvrd` and `chassis.get_sfp(index)` correlate ports to `Sfp` instances.

The XOR-1 interleave between `port_config.ini` index and I2C bus assignment is embedded
in `_SFP_BUS_MAP` in `sfp.py`:
```python
_SFP_BUS_MAP = [
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    ...
]
```

## Pass Criteria

- `show interfaces status` lists all 32 `EthernetN` ports (N = 0, 4, 8, ... 124)
- `show interfaces transceiver presence` lists index 1–32 correctly matched to
  physical QSFP ports
- For a port with a known transceiver inserted (e.g. index 1 / Ethernet0), the SFP
  bus used by the daemon matches `_SFP_BUS_MAP[0]` = bus 3
- `sonic-db-cli CONFIG_DB HGETALL 'PORT|Ethernet0'` returns lane assignment
  `117,118,119,120` and speed `100000`
- LLDP neighbour on EthernetN matches the physically connected peer port

## Known Gaps

- The PortChannel1 link (EthernetN + EthernetM, LACP to EOS peer) requires correct
  FEC and autoneg settings on both ends; these are not enforced in `port_config.ini`.
- `port_config.ini` has no breakout configuration; all 32 ports are fixed 100G×1.
  Breakout (4×25G etc.) is not supported on this platform in the current implementation.
- The `alias` column (`Ethernet1/1` format) is the EOS-compatible naming convention
  used for cross-platform LLDP correlation; SONiC itself uses `EthernetN` naming.
