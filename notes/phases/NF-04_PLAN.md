# NF-04 — Link Status: PLAN

## Problem Statement

SONiC must correctly detect and propagate link state for all 32 ports. The pipeline is:
```
BCM SAI → syncd → portsyncd → APP_DB PORT_TABLE → orchagent → STATE_DB PORT_TABLE
```

For the Wedge 100S-32X, two additional requirements exist:
1. **RS-FEC must be configured explicitly**: The BCM config disables CL73 autoneg
   (`phy_an_c73=0x0`), so FEC is never auto-negotiated. Without `fec rs`, the
   Arista EOS peer reports `FEC alignment lock: unaligned` and the link stays down.
2. **SYS2 LED must reflect link state**: The `ledd` daemon in pmon subscribes to
   STATE_DB PORT_TABLE events and sets the SYS2 LED (via CPLD register) green when
   any port is oper=up.

The peer topology is fixed:
- Ethernet16 → rabbit-lorax Et13/1 (100G DAC)
- Ethernet32 → rabbit-lorax Et14/1 (100G DAC)
- Ethernet48 → rabbit-lorax Et15/1 (100G DAC)
- Ethernet112 → rabbit-lorax Et16/1 (100G DAC)

## Proposed Approach

1. Configure RS-FEC on all 4 connected ports:
   `sudo config interface fec Ethernet{16,32,48,112} rs`
2. Verify CONFIG_DB → APP_DB → ASIC_DB → STATE_DB propagation
3. Verify BCM ASIC state via `bcmcmd "ps"` shows link=up
4. Verify SYS2 LED = 0x02 (green)
5. Save config: `sudo config save -y`

## Files to Change

None — link state propagation is standard SONiC. The BCM config and platform code
from NF-01 and NF-02 are prerequisites. The only required action is CLI configuration.

For persistence, RS-FEC config should be in `config_db.json` (via `config save`).

## Acceptance Criteria

- `show interfaces status Ethernet{16,32,48,112}`: oper=up, admin=up, fec=rs
- `redis-cli -n 4 hget 'PORT|Ethernet16' fec` = `rs` (CONFIG_DB)
- `redis-cli -n 6 hget 'PORT_TABLE|Ethernet16' netdev_oper_status` = `up` (STATE_DB)
- `redis-cli -n 1 hget 'ASIC_STATE:SAI_OBJECT_TYPE_PORT:${OID}' SAI_PORT_ATTR_ADMIN_STATE` = `true`
- SYS2 LED reads 0x02 from CPLD sysfs (`/sys/bus/i2c/devices/1-0032/led_sys2`)

## Risks and Watch-Outs

- **swss restart loop**: The teamd restart loop (see NF-08) caused swss to restart every 90s,
  preventing stable link state. Must be fixed before link-up testing is reliable.
- **RS-FEC mandatory for Arista DAC links**: Even when BCM DSC shows `SD=1, LCK=1` (signal
  detected, CDR locked), without RS-FEC the Arista reports MAC Rx Local Fault. The link will
  not come up without explicit `fec rs`.
- **SAI_PORT_ATTR_OPER_STATUS absent from ASIC_DB**: On this BCM SAI version, oper_status
  is not stored in ASIC_DB. Tests must check STATE_DB, not ASIC_DB, for oper status.
- **SYS2 LED drift**: The CPLD register can reset to 0x00 if CPLD is power-cycled or ledd
  loses event tracking. The test must restart ledd and re-check before failing on a 0x00 read.
- **EOS peer not directly SSH-reachable from build host**: When PortChannel1 is up, direct
  SSH to 192.168.88.14 is blocked by LACP topology. Always use jump via 192.168.88.12.
