# Staged Phases — Wedge 100S-32X SONiC Port
*Consolidated from phases.md, INTERFACE_PLAN.md, and next-phase-plan.md.*
*Last updated: 2026-03-11. Refactoring phases R26–R31 added.*

---

## Completion Summary

| Phase | Description | Status | Verified |
|---|---|---|---|
| 0 | I2C topology discovery | Done | 2025 |
| 1 | Platform init service (mux tree) | Done | 2025 |
| 2 | BMC TTY helper (bmc.py) | Done | 2025-02-25 |
| 3 | Thermal (8 sensors) | Done | 2025-02-25 |
| 4 | Fan (5 trays, FanDrawer) | Done | 2025-02-25 |
| 5 | PSU (CPLD presence + BMC PMBus) | Done | 2025-02-25 |
| 6 | QSFP/SFP (optoe1, PCA9535) | Done | 2025-02-25 |
| 7 | System EEPROM | Done | 2025-02-25 |
| 8 | BCM config verification | Done | 2025-02-25 |
| 9 | LED control | Done | 2025-02-25 |
| 10 | Build integration + postinst | Done | 2025-02-25 |
| 11 | Transceiver Info & DOM | Done | 2026-03-02 |
| 12 | Interface Counters & Statistics | Done | 2026-03-02 |
| 13 | Link Status & Basic Connectivity | Done | 2026-03-02 |
| 14a | Speed Change | Done | 2026-03-03 |
| 14b | DPB (platform.json, hwsku.json) | Done | 2026-03-03 |
| 15 | Auto-Negotiation & FEC | Done | 2026-03-02 |
| 16 | Media Settings | Pending (low) | — |
| 17 | Port Channel / LAG | Done | 2026-03-02 |
| 18 | LLDP Verification | Done | 2026-03-02 |
| 19 | Streaming Telemetry | Pending (low) | — |
| 20 | syseepromd fix | Pending (high) | — |
| 21 | Chassis set_status_led() | Pending (medium) | — |
| 22 | PSU telemetry investigation | Pending (medium) | — |
| 23 | BGP / L3 routing enablement | Pending (medium) | — |
| 24 | Breakout testing completion | Pending (low) | — |
| 25 | Active optics / media settings | Pending (low) | — |
| **R26** | **CPLD kernel driver (wedge100s_cpld.c)** | **Done** | 2026-03-11 |
| **R27** | **Pre-register all 32 optoe1 QSFP at boot** | **Done** | pending hardware test |
| **R28** | **Compiled BMC polling daemon (C)** | **Done** | pending hardware test |
| **R29** | **Python API → sysfs/daemon files** | **Done** | 2026-03-11 |
| **R30** | **GRUB kernel args + BCM IRQ affinity** | **Done** | 2026-03-11 |
| **R31** | **IPMI/REST investigation** | **Exploratory** | — |

**Pytest:** 82/82 passing across stages 7, 11–15, 17 (as of 2026-03-03).

---

## Completed Phase Details

### Phase 3 — Thermal
- `thermal.py`: 8 sensors (index 0=CPU Core via `coretemp.0/hwmon/hwmon*/temp*_input`, 1–7=TMP75 via BMC)
- CPU coretemp path uses hwmon wildcard — NOT `coretemp.0/temp*`
- TMP75 BMC paths: `devices/<bus>/hwmon/*/temp1_input` — NOT `drivers/lm75/` (wrong on OpenBMC)
- thermalctld poll cycle ~65s (60s interval + ~5s per BMC sensor × 7)
- Thresholds: 95/102 °C (CPU), 70/80 °C (TMP75)

### Phase 4 — Fan
- `fan.py`: Fan + FanDrawer; 5 trays via BMC TTY
- thermalctld iterates `chassis.get_all_fan_drawers()` → `drawer.get_all_fans()` — must populate `_fan_drawer_list` in Chassis, NOT `_fan_list`
- FanDrawer (1 per tray) contains 1 Fan (min of front/rear rotor RPM)
- `fan<2*fid-1>_input`=front (~7500 RPM), `fan<2*fid>_input`=rear (~4950 RPM)
- `_target_speed_pct=None` initially → `get_target_speed()` raises NotImplementedError until `set_speed()` called — avoids false alarms
- Max RPM 15400; direction F2B (INTAKE), fixed per `fani.c`
- `set_speed(pct)` sends `set_fan_speed.sh <pct>` to BMC — global, no per-tray control

### Phase 5 — PSU
- `psu.py`: Presence/pgood from host CPLD `i2c-1/0x32 reg 0x10`; PMBus telemetry via BMC TTY
- PMBus mux select: `i2cset -f -y 7 0x70 0x{channel:02x}` (PCA9546, single-byte, NO register prefix)
- PSU1: channel 0x02 → PMBus 0x59; PSU2: channel 0x01 → PMBus 0x5a
- LINEAR11: bits[15:11]=5-bit twos-complement exponent, bits[10:0]=11-bit mantissa
- VOUT = POUT/IOUT (avoids LINEAR16, per `psui.c`)
- Telemetry cache 30s; MFR_MODEL not read (SMBus block-read not in bmc.py)

### Phase 6 — QSFP/SFP
- `sfp.py`: 32-port QSFP28; SfpOptoeBase + PCA9535 presence + lazy optoe1 registration
- `_sfp_list` is 1-indexed (index 0=None sentinel), index 1..32 = Sfp(0)..Sfp(31)
- Lazy optoe1: writes `optoe1 0x50` to `new_device` on first `get_eeprom_path()` call
- Presence: PCA9535 i2cget + `_bit_swap()` (replicating `sfpi.c`), 1s TTL cache
- LP_MODE and RESET pins on mux board — not host-accessible; return False

### Phase 7 — System EEPROM
- `eeprom.py`: SysEeprom at `/sys/bus/i2c/devices/40-0051/eeprom` with cache at `/var/run/platform_cache/syseeprom_cache`
- The EEPROM address situation is complex — see `tests/notes/eeprom-address-relocation-research.md`
- i2c-1/0x50 = EC chip (not writable); i2c-1/0x51 = AT24C02 holding our TlvInfo
- `accton_wedge100s_util.py` populates the cache at boot before pmon starts

### Phase 8 — BCM Config
- `th-wedge100s-32x100G.config.bcm` byte-identical to Facebook Wedge100 reference
- All 32 portmaps verified against `port_config.ini`
- `sai.profile` correct; `installer.conf` correct (ttyS0 @ 57600)
- Flex config added later (Phase 14b): `th-wedge100s-32x-flex.config.bcm` with sub-port pre-allocation for DPB

### Phase 9 — LED Control
- `plugins/led_control.py`: SYS1 (0x3e) = green on init; SYS2 (0x3f) = green when ≥1 port up
- Color encoding: 0=off, 1=red, 2=green, 4=blue; +8=blinking
- ledd monitors STATE_DB PORT_TABLE field `netdev_oper_status` (NOT APPL_DB, NOT `oper_status`)
- ledd must start AFTER CONFIG_DB has ports — restart via `supervisorctl restart ledd` inside pmon if needed

### Phase 10 — Build Integration
- `debian/control`: package stanza for `sonic-platform-accton-wedge100s-32x`
- `debian/rules`: `wedge100s-32x` in MODULE_DIRS; conditional udev cp; elif for `sonic_platform_setup.py`
- `.install`: ships `sonic_platform-1.0-py3-none-any.whl` to device dir
- `.postinst`: depmod, enable/start init service, patches pmon.sh for ttyACM0 passthrough, auto-removes stopped pmon container

### Phase 11 — Transceiver Info & DOM
- EEPROM read path working: `read_eeprom(0, 4)` returns 0x11 (QSFP28)
- xcvrd populates STATE_DB: TRANSCEIVER_INFO, TRANSCEIVER_DOM_SENSOR, TRANSCEIVER_STATUS
- DOM values all N/A — expected for passive DAC cables (no monitoring electronics)
- `get_xcvr_api()` returns None for 5/7 present ports — cheap DAC EEPROM reliability
- **Remaining**: test with active optics (SR4/LR4) for DOM; test hot-swap change events

### Phase 12 — Interface Counters
- Flex counter PORT_STAT enabled at 1000ms polling interval
- Full SAI_PORT_STAT_* entries (IF_IN_OCTETS, IF_IN_UCAST_PKTS, IF_IN_ERRORS, etc.)
- `show interfaces counters` works; `sonic-clear counters` verified
- Fully verified, no remaining items

### Phase 13 — Link Status & Connectivity
- 4 ports connected to rabbit-lorax (Arista EOS): Ethernet16, 32, 48, 112 via 100G DAC
- RS-FEC (CL91) **required** for 100GBASE-CR4 links to Arista — link stays down without it
- BCM `ps`: ce0, ce4, ce8, ce24 all `up 4 100G FD KR4`
- LLDP: 4 front-panel + 1 mgmt neighbor discovered
- swss restart loop fixed: patched swss.sh to check CONFIG_DB feature state for teamd
- **Remaining**: L3 ping test blocked by Arista peer in L2 bridge mode (resolved in Phase 17)

### Phase 14a — Speed Change
- `config interface speed` accepted by SAI, propagates to CONFIG_DB/APP_DB
- BCM ASIC stays at 100G — static `.config.bcm` doesn't reconfigure serdes dynamically
- True speed change requires syncd restart or flex BCM config

### Phase 14b — Dynamic Port Breakout
- `platform.json`: 32 ports, breakout modes `1x100G[40G]`, `2x50G`, `4x25G[10G]`
- `hwsku.json`: 32 ports, default `1x100G[40G]`
- Initially crashed orchagent (SAI doesn't support dynamic port creation)
- **Solved**: `th-wedge100s-32x-flex.config.bcm` pre-allocates sub-ports with `:i` (inactive) flag
- Live DPB now works: `config interface breakout Ethernet64 '4x25G[10G]' -y -f -l`
- Port 17 (Ethernet64) transceiver "Not present" — likely physical seating issue
- **Remaining**: test breakout via `config reload` path; BCM config Jinja2 template (future)

### Phase 15 — Auto-Negotiation & FEC
- RS-FEC (CL91): works end-to-end, required for 100GBASE-CR4
- FC-FEC (CL74): rejected for 100G ports (SAI only supports `rs` and `none`)
- Auto-negotiation: CLI accepts config but SAI does NOT program ASIC (`phy_an_c73=0x0` in BCM config)
- Do not change phy_an_c73 — risks instability without multi-vendor testing
- **Remaining**: test FC-FEC on 25G sub-ports after breakout

### Phase 17 — Port Channel / LAG
- PortChannel1 with Ethernet16 + Ethernet32, LACP active, IP 10.0.1.1/31
- Peer: rabbit-lorax Port-Channel1 (Et13/1 + Et14/1), IP 10.0.1.0/31
- L3 connectivity: bidirectional ping 0% loss, avg <0.3ms
- Failover verified: shutdown one member → LAG stays up, 0% loss; re-add → re-Selected in 8s
- teamd was disabled by default — needed `config feature state teamd enabled`

### Phase 18 — LLDP
- LLDP container running; 4 front-panel + 1 mgmt neighbor discovered
- Port mapping verified against rabbit-lorax (Arista)

---

## Pending Phase Details

### Phase 20: syseepromd Fix
**Priority**: High | **Effort**: Small

syseepromd crashes (FATAL) inside pmon — the EEPROM sysfs path `/sys/bus/i2c/devices/40-0051/eeprom` isn't visible inside the container.

**Tasks**:
1. Diagnose why sysfs path isn't visible inside pmon
2. Likely fix: make `eeprom.py` `read_eeprom()` read from the cache file only (already populated at boot by `accton_wedge100s_util.py`), bypassing sysfs
3. Test syseepromd runs successfully inside pmon

### Phase 21: Chassis set_status_led()
**Priority**: Medium | **Effort**: Small

system-health cannot drive SYS1 LED because `chassis.py` doesn't implement `set_status_led()`.

**Tasks**:
1. Implement `set_status_led(color)` in `chassis.py` using CPLD reg 0x3e
2. Map: `STATUS_LED_COLOR_GREEN` → 0x02, `AMBER/RED` → 0x01, `GREEN_BLINK` → 0x0a
3. Implement `get_status_led()` for read-back
4. Test with system-health restart

### Phase 22: PSU Telemetry Investigation
**Priority**: Medium | **Effort**: Medium

`psu.py` returns `None` for voltage, `0.0` for current/power. `get_temperature()` not implemented.

**Tasks**:
1. Read PMBus registers directly from BMC: VIN (0x88), VOUT (0x8b), IOUT (0x8c)
2. Verify LINEAR11 decode against raw values
3. Test PSU2 (has AC power) separately from PSU1 (no AC)
4. Fix decode or document as PSU model limitation
5. Optionally implement `get_temperature()` via READ_TEMPERATURE_1 (0x8d)

### Phase 23: BGP / L3 Routing Enablement
**Priority**: Medium | **Effort**: Medium

BGP service is masked and feature disabled. Only masked service worth unmasking.

**Tasks**:
1. `sudo systemctl unmask bgp.service`
2. `config feature state bgp enabled`
3. Configure ASN and neighbors in CONFIG_DB
4. Verify FRR starts and establishes adjacency
5. Write pytest stage for BGP verification

### Phase 24: Breakout Testing Completion
**Priority**: Low | **Effort**: Medium

Live DPB works with flex BCM config but needs fuller testing.

**Tasks**:
1. Test breakout on ports 17 and 21 (QSFP→4x25G cables)
2. Verify 25G link-up on breakout sub-ports
3. Test FC-FEC on 25G sub-ports
4. Investigate Port 17 transceiver detection issue
5. Evaluate BCM config Jinja2 template for multi-speed support

### Phase 25: Active Optics / Media Settings
**Priority**: Low | **Effort**: Medium

No `media_settings.json` exists; serdes pre-emphasis is baked into BCM config.

**Tasks**:
1. Acquire SR4 or LR4 QSFP28 optic
2. Test DOM population (temperature, voltage, TX/RX power)
3. If signal integrity issues → create `media_settings.json`

---

## Refactoring Phase Details (R26–R31)

*Full rationale and design in `tests/ARCHSPEC.md` Section 8.*
*These replace the Python I2C mess with kernel drivers and compiled code.*

### ONL Architecture Finding (2026-03-11)
ONL `platform_lib.c` ALSO uses `/dev/ttyACM0` for all BMC access — same as our
`bmc.py`.  ONL has ZERO custom kernel modules for this platform (`no-platform-modules.yml`).
We are already at parity with ONL for host I2C (smbus2 vs subprocess).
The gaps are: lazy optoe1 (→ DAC EEPROM fails), no CPLD sysfs driver, slow
per-call TTY (→ 65s poll cycles), missing kernel args.

### Phase R26 — CPLD Kernel Driver
**Priority**: Medium | **Effort**: Medium

**Tasks**:
1. Write `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c`
   - Bind to i2c-1/0x32; expose sysfs: `cpld_version`, `psu1_present`, `psu1_pgood`,
     `psu2_present`, `psu2_pgood`, `led_sys1`, `led_sys2`
   - Pattern: `as7712-32x/modules/accton_i2c_cpld.c` + `leds-accton_as7712_32x.c`
2. Add module to `debian/rules` kernel module build step
3. Add to `kos` list in `accton_wedge100s_util.py`: `modprobe wedge100s_cpld`
4. Add device registration in `mknod`: `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device`
5. Update `psu.py` and `plugins/psuutil.py` to read from sysfs
6. Update `plugins/led_control.py` to write to sysfs

### Phase R27 — Pre-register All 32 optoe1 QSFP Devices at Boot *(Critical)*
**Priority**: Critical | **Effort**: Small

**Root cause of DAC cable EEPROM failure**: optoe1 registered lazily → EEPROM
path not in sysfs until xcvrd first touches the port → diagnostic reads fail.
Arista EOS pre-registers all transceiver drivers at boot.

**Tasks**:
1. Extend `mknod` list in `accton_wedge100s_util.py` to register optoe1 on all
   32 QSFP buses after PCA9548 muxes are registered:
   ```python
   for bus in SFP_BUS_MAP:
       mknod_qsfp.append('echo optoe1 0x50 > /sys/bus/i2c/devices/i2c-{}/new_device'.format(bus))
   ```
2. Add corresponding `delete_device` entries to `device_uninstall()`
3. Remove `_register_device()` lazy-init from `sfp.py` — EEPROM path always exists
4. Test: `cat /sys/bus/i2c/devices/i2c-3/3-0050/eeprom | hexdump -C | head` before
   xcvrd starts — should return valid QSFP28 data for DAC cable on port 0

### Phase R28 — Compiled BMC Polling Daemon
**Priority**: High | **Effort**: Medium-Large

**Problem**: 7 thermal reads × ~8s TTY round-trip = ~65s poll cycle.
**Solution**: compiled C binary reads ALL sensors in one TTY session (~3s total).

**Tasks**:
1. Write `utils/wedge100s-bmc-daemon.c` based on ONL `platform_lib.c` TTY code:
   - Single TTY session reads: 7×thermal, 10×fan RPM, 5×fan presence, 2×4 PSU PMBus
   - Outputs to `/run/wedge100s/` (one file per value, plain integer format)
   - Invoked as one-shot: `wedge100s-bmc-daemon poll`
2. Write `service/wedge100s-bmc-poller.service` + `.timer` (10s interval)
3. Add compiled binary to `debian/rules`, install to `/usr/local/bin/`
4. Bind-mount `/run/wedge100s/` into pmon container (add to postinst)
5. Document PSU PMBus mux sequence (i2cset channel select, then i2cget word reads)

### Phase R29 — Python Platform API → sysfs/Daemon Files
**Priority**: Follows R26+R28 | **Effort**: Medium

**Tasks**:
1. `thermal.py`: replace `bmc.file_read_int()` with `open('/run/wedge100s/thermal_N').read()`
2. `fan.py`: replace `bmc.file_read_int()` with reads from `/run/wedge100s/fan_*`
3. `psu.py` PMBus: replace `bmc.i2cget_word()` with reads from `/run/wedge100s/psu_*`
4. `psu.py` presence: replace `platform_smbus.read_byte()` with CPLD sysfs reads (R26)
5. `plugins/led_control.py`: replace smbus2/i2cset with CPLD sysfs writes (R26)
6. Remove `bmc.i2cget_byte/word/set` functions (TTY-based BMC i2c still used only
   for `set_fan_speed.sh` write path)

### Phase R30 — GRUB Kernel Args + BCM IRQ Affinity
**Priority**: Medium | **Effort**: Small

**Context**: SSH interactive sluggishness is BCM56960 IRQ storm (~150 IRQ/s on
IRQ 16 `linux-kernel-bde`), saturating one CPU's HI softirq queue.
ONL uses `nopat intel_iommu=off noapic` for this platform.

**Tasks**:
1. Test `noapic` on kernel 6.1 — does it affect BCM IRQ behavior?
   `sudo sh -c 'echo noapic >> /proc/cmdline'` (runtime test only, needs reboot to persist)
2. If effective: add to `device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf`:
   `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"`
3. Alternative: pin IRQ 16 to CPU 0 in platform init service:
   `echo 1 > /proc/irq/16/smp_affinity`
   (leaves CPUs 1+ free for sshd/userspace)
4. Measure: `ssh` latency before/after with `time ssh admin@192.168.88.12 hostname`

### Phase R30 — GRUB Kernel Args + BCM IRQ Affinity (Done 2026-03-11)

**Root cause**: BCM56960 fires ~150 HW interrupts/sec on IRQ 16 (`linux-kernel-bde`).
With default SMP affinity (all CPUs), the HI softirq queue saturates one random CPU,
creating 15-30 s windows where sshd cannot accept new connections.

**Measured**: First cold SSH connect: **65 s** before fix, **0.25 s** after.

**Implementation** (two layers for belt-and-suspenders coverage):

1. `device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf`:
   Added `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"` — matches
   ONL platform YAML.  Effective for freshly installed images.

2. `platform/.../utils/accton_wedge100s_util.py` — `_pin_bcm_irq()`:
   `echo 1 > /proc/irq/16/smp_affinity` pinning IRQ 16 to CPU 0, called from
   `do_install()` during platform-init service startup.  Covers running systems
   regardless of kernel args (including upgrades that don't reinstall).

**Verified on hardware 2026-03-11**: `smp_affinity_list` changes 0-3 → 0 on
`sudo python3 accton_wedge100s_util.py install`.

### Phase R31 — IPMI / OpenBMC REST Investigation
**Priority**: Low (exploratory) | **Effort**: Small investigation

**Goal**: Determine if there's a better BMC communication channel than raw TTY.

**Tasks**:
1. Check for IPMI KCS: `modprobe ipmi_si && ls /dev/ipmi*` — if `/dev/ipmi0` exists,
   `ipmitool sdr list` would replace all BMC TTY sensor reads
2. Check for BMC USB CDC-ECM (network gadget): `ip link show | grep -i usb`
   If present, BMC REST API via `curl -k https://<bmc-ip>/redfish/v1/...` is available
3. Check for IPMI over LAN: `ipmitool -H 192.168.88.13 -U admin -P 0penBmc sdr list`
4. Document findings regardless — these are architectural decisions for future work

---

## Known Limitations (not phase-gated)

| Item | Severity | Notes |
|---|---|---|
| EEPROM at 0x51 (should be 0x50) | Accepted | Hardware may be damaged; cached workaround in place |
| PSU1 power FAIL | Lab only | No AC power to PSU1 in lab |
| PSU model/serial = N/A | Low | SMBus block read not in bmc.py |
| Auto-negotiation no-op at ASIC | Accepted | BCM config `phy_an_c73=0x0`; do not change |
| Speed change config-layer only | Known | Static BCM config doesn't reconfigure serdes |
| BMC cross-process lock | Low | Only pmon accesses TTY; add fcntl.flock if needed |
| No media_settings.json | Low | Works with current DAC cables |
| QSFP LP_MODE / RESET not accessible | N/A | Pins on mux board, not host-accessible |
| Fan per-tray speed control | N/A | Not supported in hardware; `set_fan_speed.sh` is global |

---

## Physical Topology

| Port | Ethernet | Connection | Status |
|---|---|---|---|
| 1 | Ethernet0 | Breakout cable, no peers | Present, no link |
| 5 | Ethernet16 | rabbit-lorax Et13/1 (100G DAC) | UP (RS-FEC) — PortChannel1 |
| 9 | Ethernet32 | rabbit-lorax Et14/1 (100G DAC) | UP (RS-FEC) — PortChannel1 |
| 13 | Ethernet48 | rabbit-lorax Et15/1 (100G DAC) | UP (RS-FEC) |
| 17 | Ethernet64 | QSFP→4x25G breakout | DPB active; transceiver issue |
| 21 | Ethernet80 | QSFP→4x25G breakout | DPB active; Ethernet80/81 link up |
| 29 | Ethernet112 | rabbit-lorax Et16/1 (100G DAC) | UP (RS-FEC) |

---

## Reference Documents

| Document | Path |
|---|---|
| Architecture spec | `tests/ARCHSPEC.md` |
| EEPROM research | `tests/notes/eeprom-address-relocation-research.md` |
| Flex BCM config notes | `tests/notes/dpb-flex-bcm.md` |
| Phase 11–13 notes | `tests/notes/phase-11-13-interface-verification.md` |
| Phase 14a notes | `tests/notes/phase-14a-speed-change.md` |
| Phase 14b notes | `tests/notes/phase-14b-dpb.md` |
| Phase 15 notes | `tests/notes/phase-15-autoneg-fec.md` |
| Phase 17 notes | `tests/notes/phase-17-portchannel.md` |
| EEPROM tutorial | `tests/HOWTO-EEPROM.md` |
