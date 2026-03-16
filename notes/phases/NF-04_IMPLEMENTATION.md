# NF-04 — Link Status: IMPLEMENTATION

## What Was Built

No new platform files were required. Link status is handled entirely by the BCM SAI,
syncd, and the standard SONiC portsyncd / orchagent pipeline.

The following configuration was applied and persisted via `config save`:

### RS-FEC Configuration (required for link-up with Arista EOS)

```bash
sudo config interface fec Ethernet16 rs
sudo config interface fec Ethernet32 rs
sudo config interface fec Ethernet48 rs
sudo config interface fec Ethernet112 rs
sudo config save -y
```

These are now in `config_db.json` and survive reboots.

### swss Restart Loop Fix (prerequisite)

The swss container was restarting every ~90s due to teamd masking. Fix applied to
`/usr/local/bin/swss.sh` on the running switch. Details in NF-08 implementation.
This fix was required before stable link-up testing was possible.

## Root Cause of Initial Link Failure

- **BCM DSC state before RS-FEC**: `SD=1, LCK=1` (signal detect on, CDR locked) — serdes
  was healthy. The electrical layer was working.
- **Arista EOS diagnosis**: `Forward Error Correction: Reed-Solomon` (EOS using RS-FEC),
  `FEC alignment lock: unaligned`, `MAC Rx Local Fault: true`.
- **Root cause**: SONiC BCM config (`phy_an_c73=0x0`) disables CL73 AN, so FEC mode
  is never negotiated. BCM SAI defaults to no-FEC or FC-FEC for 100G CR4. Arista expects
  RS-FEC (CL91). The FEC mismatch causes framing errors at the MAC layer.
- **Fix**: Static RS-FEC via `config interface fec <port> rs`. All 4 links came UP immediately.

## Port State Pipeline (verified on hardware 2026-03-02)

```
CONFIG_DB PORT|EthernetN
  admin_status=up, fec=rs
  ↓ portmgrd
APP_DB PORT_TABLE:EthernetN
  admin_status=up, oper_status=up, fec=rs
  ↓ orchagent → syncd
ASIC_DB ASIC_STATE:SAI_OBJECT_TYPE_PORT:oid:...
  SAI_PORT_ATTR_ADMIN_STATE=true
  SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_RS
  (no SAI_PORT_ATTR_OPER_STATUS — not stored in this SAI version)
  ↓ portsyncd
STATE_DB PORT_TABLE|EthernetN
  netdev_oper_status=up
  ↓ ledd
CPLD register 0x3f (led_sys2) = 0x02 (GREEN)
```

## BCM Port State (post-RS-FEC)

```
docker exec syncd bcmcmd "ps"
  ce0(1)    up 4 100G FD SW No Forward Untag FA KR4 9122   (Ethernet16)
  ce4(17)   up 4 100G FD SW No Forward Untag FA KR4 9122   (Ethernet32)
  ce8(34)   up 4 100G FD SW No Forward Untag FA KR4 9122   (Ethernet48)
  ce24(102) up 4 100G FD SW No Forward Untag FA KR4 9122   (Ethernet112)
```

All: 100G, Full Duplex, KR4 (static, no AN), 9122 MTU.

## SYS2 LED

After first link-up event: `/sys/bus/i2c/devices/1-0032/led_sys2` = `0x02` (GREEN).
LED is updated by ledd which subscribes to STATE_DB PORT_TABLE `netdev_oper_status`.

Known issue: ledd can lose track of port states if the CPLD register resets or if ledd
restarts without re-querying STATE_DB. The test handles this by restarting ledd and
re-checking if the initial read is not 0x02.

## Connected Port Topology

| SONiC Port | EOS Port | BCM Port | Physical Lanes | RS-FEC Required |
|---|---|---|---|---|
| Ethernet16 | rabbit-lorax Et13/1 | ce0(1) | 5,6,7,8 | yes |
| Ethernet32 | rabbit-lorax Et14/1 | ce4(17) | 21,22,23,24 | yes |
| Ethernet48 | rabbit-lorax Et15/1 | ce8(34) | 37,38,39,40 | yes |
| Ethernet112 | rabbit-lorax Et16/1 | ce24(102) | 101,102,103,104 | yes |

## Hardware-Verified Facts

- verified on hardware 2026-03-02: all 4 DAC-connected ports come up after RS-FEC configured
- verified on hardware 2026-03-02: BCM ce0/ce4/ce8/ce24 show `up 4 100G FD KR4`
- verified on hardware 2026-03-02: SYS2 LED = 0x02 immediately on first link-up
- verified on hardware 2026-03-02: CONFIG_DB → APP_DB → STATE_DB pipeline works correctly
- verified on hardware 2026-03-02: SAI_PORT_ATTR_OPER_STATUS absent from ASIC_DB (normal)

## Remaining Known Gaps

- **Ethernet104/108 DOWN**: CWDM4 optical modules with physical fiber break. Not a software issue.
  See `notes/BEWARE_OPTICS.md`.
- **L3 connectivity not tested over individual ports**: Arista Et13-16 are in a bridge (no IP)
  before PortChannel1 was configured. L3 test deferred to NF-08.
- **LACP-member ports Ethernet16/32 absorbed into PortChannel1**: These are no longer usable
  as standalone L3 interfaces. See NF-08.
