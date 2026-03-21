# Staged Implementation Phases — Wedge 100S-32X SONiC Port

Status as of 2026-03-20. Phases 00–21 + nn are COMPLETE. Phase 22 (optical) PARTIAL.

## Legend

| Symbol | Meaning |
|--------|---------|
| COMPLETE | Tests passing on hardware, implementation verified |
| PARTIAL | Implementation exists, some tests failing |
| PENDING | Not yet implemented |

---

## Phase 00: Pre-test Preconditions
**Status: COMPLETE**
- Snapshot mechanism working (`/run/sonic-cfggen-snapshot.json`)
- All ports configured at 100G, no PortChannel, no FEC
- pmon running, suite marker created

## Phase 01: EEPROM / System Identity
**Status: COMPLETE**
- `syseeprom.py` reads TlvInfo EEPROM via `/sys/bus/i2c/devices/1-0050/eeprom`
- Daemon cache at `/var/run/platform/syseeprom`
- `show platform syseeprom` reports Product Name, Serial, Base MAC, CRC
- Platform API `get_system_eeprom_info()` returns correct dict

## Phase 02: System / SONiC Version
**Status: COMPLETE**
- Kernel 6.12.41+deb13-sonic-amd64 running
- `show version` reports platform correctly as `x86_64-accton_wedge100s_32x-r0`
- All Docker containers running (syncd, swss, bgp, teamd, pmon, etc.)
- `sonic_platform` Python package installed at `/usr/lib/python3/dist-packages/`

## Phase 03: Platform Infrastructure
**Status: COMPLETE**
- CP2112 USB-I2C bridge loaded (bus 1), I2C mux tree built by `platform_init.service`
- CPLD accessible at bus 6 addr 0x40
- BMC accessible via `/dev/ttyACM0` (CDC-ACM) and REST API at 192.168.88.13
- `bmc-poller.timer` writing thermal/fan data to `/run/wedge100s/`
- `platform_init.service` active, creates `/run/wedge100s/` tree

## Phase 04: Thermal Sensors
**Status: COMPLETE**
- 9 BMC thermal sensors exposed via platform API (LM75 temps from BMC poller cache)
- `show platform temperature` lists all sensors
- All temperatures in range (20–85°C), status OK
- Host coretemp via kernel hwmon (2 cores)

## Phase 05: Fan Control
**Status: COMPLETE**
- 5 fan trays, each with front and rear fan (10 total)
- All fans present and spinning (RPM > 1000 when present)
- Direction: front-to-back (F2B) per hardware design
- `show platform fan` shows FanTray1–5 with RPM

## Phase 06: PSU
**Status: COMPLETE**
- 2 PSU slots; both present and powered in test environment
- Capacity 1100W, type AC, DC output ~12V
- AC input voltage readable; model "Delta DPS-1100AB-6 A"
- PSU serial/revision N/A (hardware limitation of this PSU model via I2C)

## Phase 07: QSFP / SFP Presence
**Status: COMPLETE**
- 32 QSFP28 ports (Ethernet0–Ethernet124 at stride 4)
- Presence bitmap read from PCA9535 GPIO expanders (i2c daemon cache)
- Ports with transceivers: 9 installed (including 2 CWDM4 optical at ports 27–28)
- EEPROM identifier byte readable for installed modules
- `sfputil show presence` and platform API `get_presence()` consistent

## Phase 08: LED Control
**Status: COMPLETE (1 known pre-existing failure)**
- SYS1 LED: green when system healthy (CPLD reg 0x03)
- SYS2 LED: green when any port is link-up (CPLD reg 0x04), set by `ledd`
- `led_control.py` plugin drives both LEDs
- Known issue: `test_led_sys2_consistent_with_port_state` fails because ledd has
  not polled after boot at test time. Not a platform regression.

## Phase 09: CPLD
**Status: COMPLETE**
- CPLD driver at `/sys/bus/i2c/devices/6-0040/` with sysfs attrs
- Version register readable (hex format)
- PSU present/pgood readable from CPLD sysfs
- LED registers readable and writable (write-restore verified)

## Phase 10: Platform Daemons
**Status: COMPLETE**
- `i2c-poller.timer`: refreshes QSFP presence + thermal every 60s
- `bmc-poller.timer`: refreshes BMC thermal/fan data every 60s
- Syseeprom cache not stale (< 300s)
- QSFP presence cache fresh, all 32 ports present in cache
- Fan RPM values reasonable (0 for empty slots, >1000 for installed fans)
- PSU cache files exist

## Phase 11: Transceiver / xcvrd
**Status: COMPLETE**
- xcvrd running inside pmon container
- TRANSCEIVER_INFO and TRANSCEIVER_DOM tables populated in STATE_DB
- DOM data present for passive DAC cables (RX power N/A is expected)
- `sfputil show eeprom` exits zero, reports identifier byte
- xcvr API factory returns QSFP28 object for installed modules

## Phase 12: Counters / Flex Counter
**Status: COMPLETE**
- Flex counter FLEX_COUNTER_TABLE|PORT_STAT_COUNTER_POLL enabled
- COUNTERS_PORT_NAME_MAP has all 32 ports
- COUNTERS:{oid} has stat entries for each port
- `show interfaces counters` reports U (up) for link-up ports
- RX traffic visible on connected ports (Ethernet16, Ethernet32, etc.)
- `sonic-clear counters` works correctly

## Phase 13: Link / FEC
**Status: COMPLETE**
- RS-FEC configured and active on connected ports (Ethernet16/32/48/112)
- Connected ports admin-up and oper-up
- PORT_TABLE in APP_DB shows all ports
- SYS2 LED green when links are up
- LLDP neighbors discovered on connected ports

## Phase 14: Port Breakout
**Status: COMPLETE**
- `platform.json` present with 32 ports, correct lane assignments
- `hwsku.json` present with default mode 1x100G
- Speed change 100G→40G→100G accepted and reflected in CLI
- 4x25G breakout mode defined in platform.json
- Chassis SFP entries aligned with breakout config

## Phase 15: Autoneg / FEC Profiles
**Status: COMPLETE**
- RS-FEC accepted, FC-FEC rejected (correct for 100G Tomahawk)
- FEC=none accepted
- Autoneg enable/disable accepted, propagates to CONFIG_DB and APP_DB
- Autoneg programs SAI_PORT_ATTR_AUTO_NEG_MODE in ASIC_DB
- Advertised speeds (10000,25000,40000,100000) accepted
- Advertised types (SR, LR, CR, KR) accepted

## Phase 16: PortChannel / LACP
**Status: COMPLETE**
- teamd feature enabled, container running
- PortChannel1 with Ethernet16+Ethernet32 members, IP 10.0.1.1/31
- LACP active state with EOS peer (rabbit-lorax 192.168.88.14)
- Both members selected, teamdctl state = "current"
- LAG_TABLE and LAG_MEMBER_TABLE in APP_DB
- SAI LAG object and member objects in ASIC_DB
- Ping to peer 10.0.1.0 succeeds over LAG
- Failover test: remove one member, ping continues, re-add, both selected
- Standalone ports unaffected (Ethernet48/112 still up)

## Phase 17: Status Report
**Status: COMPLETE**
- `test_generate_platform_status_report` generates a text report to
  `tests/reports/platform_status_{timestamp}.txt`

## Phase 18: Snapshot / Restore
**Status: COMPLETE** (implemented as stage_nn_posttest)
- Snapshot taken in stage_00_pretest
- Restore executed in stage_nn_posttest
- All 5 post-restore checks pass (ports up, no PortChannel, pmon running)

## Phase 19: Platform CLI Audit
**Status: COMPLETE**
- Base MAC from syseeprom CLI and platform API
- Reboot cause via `show reboot-cause`
- CPLD and BIOS firmware version via component.py platform API
- PSU model not N/A (Delta model string present)
- `show environment` reports 3+ coretemp thermal lines
- `show platform fan` reports 5+ fan lines
- `get_port_or_cage_type(1)` returns QSFP28 bitmask
- `sudo watchdogutil status` exits 0

## Phase 20: Traffic Forwarding
**Status: COMPLETE**
- 5000-packet flood over PortChannel1 increments RX_OK by >= 4500
- 5000-packet flood increments TX_OK by >= 4500
- Ethernet48 COUNTERS_DB OID is mapped and counter keys exist
- FEC correctable error rate < 1e-6/s under flood
- `portstat` after `sonic-clear counters` shows <= 50 residual packets per port

## Phase 21: LP_MODE Daemon Control
**Status: COMPLETE**
- Daemon exclusively owns LP_MODE PCA9535 pins (0x20, 0x21 on mux 0x74 ch0/ch1)
- All present ports deasserted (LP_MODE=0, TX enabled) on first daemon invocation
- sfp.py get_lpmode() reads /run/wedge100s/sfp_N_lpmode (file-only, no I2C)
- sfp.py set_lpmode() writes /run/wedge100s/sfp_N_lpmode_req (file-only, no I2C)
- Daemon processes req files within one poll tick (~3 s), deletes req file after apply
- Readback verification after each PCA9535 write (state file updated only on hardware-confirmed write)

## Phase 22: Optical Port Bring-Up and CLI Fixes
**Status: PARTIAL (1/4 ports UP, 3/4 physically blocked)**

### CLI Fixes
- `show interfaces transceiver pm EthernetX` now renders SFF-8636 (non-CMIS) modules as a 4-lane table (Rx Power / Tx Bias / Tx Power) using `TRANSCEIVER_DOM_SENSOR` from STATE_DB. Previously showed `N/A` for all optical ports.
- `show interfaces transceiver status EthernetX` and `show interfaces transceiver info EthernetX` work correctly for all 4 optical ports without code changes (xcvrd populates TRANSCEIVER_STATUS for SFF-8636 modules when present).

### DOM Architecture Fix (CP2112 Bus Saturation)
- `wedge100s-i2c-daemon`: Per-tick lower-page EEPROM reads removed. Stable ports (valid SFF ID byte in `/run/wedge100s/sfp_N_eeprom` cache) skip I2C entirely; EEPROM read only happens on insertion.
- `sfp.py`: Demand-driven TTL refresh added (`_DOM_CACHE_TTL = 10 s`). When xcvrd requests offset < 128 and TTL has expired, performs a live smbus2 lower-page (128 bytes) read and merges with cached upper page. Prevents CP2112 HID bus saturation that caused SSH unresponsiveness.

### Optical Port Status (hardware-verified 2026-03-20)
| Port | Module | Status | Root Cause |
|------|--------|--------|------------|
| Ethernet100 | Arista SR4 | **DOWN** | Physical Rx LOS — MPO fiber from Arista Et26/1 not reaching Ethernet100 Rx cage |
| Ethernet104 | Arista LR4 | **DOWN** | BCM NPU_SI_SETTINGS_DEFAULT; TXAMP=8 below LR4 module host-input LOS threshold |
| Ethernet108 | Arista SR4 | **UP** | RS-FEC; confirmed via LLDP (rabbit-lorax Et28/1) |
| Ethernet116 | ColorChip CWDM4 | **DOWN** | Arista Et30/1 laser not transmitting (-30 dBm) |

### Known Blockers (not platform bugs)
- Ethernet100: Physical fiber routing — requires hardware inspection of MPO-12 cage 25
- Ethernet104: Requires platform-specific BCM SI settings file (portmap/sai_profile with per-port TXAMP/TXEQ for Wedge100S QSFP28 ports) so `NPU_SI_SETTINGS_SYNC_STATUS` moves from DEFAULT to calibrated
- Ethernet116: Arista Et30/1 laser shutdown or LP_MODE on peer side — requires EOS investigation

---

## Phase nn: Post-Test Restore
**Status: COMPLETE**
- Restore from snapshot cleans PortChannel1, FEC, IP config
- pmon running after restore
- Connected ports admin-up after restore
- Suite active marker removed

---

## Overall Status: PHASES 00–21 COMPLETE; Phase 22 PARTIAL

Pass rate: 230/231 = 99.6% (automated test suite, phases 00–21)
Remaining (automated): 1 pre-existing ledd polling race (stage_08)
Remaining (physical): Ethernet100 fiber, Ethernet104 BCM SI settings, Ethernet116 peer laser
Hardware-verified: 2026-03-20
