# Test Plan — Accton Wedge 100S-32X SONiC Port

*Last updated: 2026-03-15. Reflects EOS-P2 (hidraw daemon) architecture.*

---

## 1. Test Philosophy

### Dynamic Discovery
Tests retrieve live state from the target via SSH rather than checking static
configuration files. Fixtures connect once per session (`pytest_sessionstart`)
and share a single `SSHClient` across all stages. Each test either runs a
SONiC CLI command or executes a short Python script via `ssh.run_python()`.
This approach catches regressions that affect the running system but not the
source files.

### State-Restore Contract
No stage test is permitted to leave the switch in a different operational state
than it found it. Tests are read-only by default. The one exception is
`stage_14_breakout`, which applies and then removes a port breakout; that stage
is responsible for its own cleanup (see Section 4).

### Skip vs. Fail
A test uses `pytest.skip()` when the absence of a condition is expected for
this lab setup (e.g. no optical modules installed, PSU1 unpowered). A test
fails when a condition that should always be true on a running Wedge 100S is
false (e.g. i2c-1 missing, EEPROM cache corrupt, daemon not running). Skips
are documented in test docstrings with the reason.

### Hardware Dependencies
All stages target the live switch at `192.168.88.12`. Target connection
parameters live in `tests/target.cfg` (gitignored template at
`tests/target.cfg.example`). Tests that require a live EOS peer
(`192.168.88.14`) or populated transceivers annotate the requirement in
the docstring and skip gracefully when the condition is absent.

### Execution Model
```
cd tests
python3 run_tests.py                        # all stages
python3 run_tests.py stage_04_thermal       # single stage
python3 run_tests.py --report               # human-readable summary (no pass/fail)
python3 run_tests.py stage_04_thermal -- -x # stop on first failure
```
The runner invokes `pytest` under the hood, forwarding `--target-cfg` so all
fixtures resolve the SSH connection from the same config file.

---

## 2. Subsystem Coverage Table

| Subsystem | Stage | Status | Pass Criteria | Known Gaps |
|---|---|---|---|---|
| System EEPROM | `stage_01_eeprom` | Exists | TlvInfo magic; 5 required TLV codes; Product Name contains "wedge"; valid MAC; CRC-32 present; Python API dict matches | EEPROM physically at 0x51 (not 0x50); cached workaround masks this |
| Platform init / I2C | `stage_02_system` | Exists | wedge100s-platform-init.service active; CP2112 hidraw present; /run/wedge100s/ populated | — |
| I2C topology / CPLD / BMC | `stage_03_platform` | Exists | i2c-0 and i2c-1 exist; CP2112 in i2cdetect; CPLD at 1/0x32 readable; BMC TTY responds; bmc-poller timer active | i2c-2..41 intentionally absent (hidraw arch) |
| Thermal sensors | `stage_04_thermal` | Exists | 8 sensors returned; all temps 0–100 °C; thresholds present; show platform temperature ≥1 row | thermalctld poll ~65 s; slow but not a bug |
| Fan trays | `stage_05_fan` | Exists | 5 FanDrawers; each drawer has 1 Fan; RPM in valid range; direction INTAKE; set_speed() accepted | Per-tray speed not supported; global only |
| Power supplies | `stage_06_psu` | Exists | 2 PSUs enumerated; presence bits from CPLD; status/powergood populated; capacity 650 W | PSU1 unpowered in lab; PSU telemetry returns 0.0 (Phase 22 pending) |
| QSFP presence & EEPROM | `stage_07_qsfp` | Exists | 32 ports returned; presence cache files exist for all 32; EEPROM identifier byte non-zero for ≥1 present port; vendor bytes readable | Corrupted EEPROM on Ethernet16/32/112 (cheap DAC); skip not fail |
| LED control | `stage_08_led` | Exists | SYS1/SYS2 LED registers writable via CPLD sysfs; write/readback green succeeds | chassis.set_status_led() not implemented (Phase 21) |
| CPLD sysfs driver | `stage_09_cpld` | **Planned** | All wedge100s_cpld sysfs entries readable; values in valid range (version, psu*_present, psu*_pgood, led_sys1, led_sys2) | — |
| Daemon health | `stage_10_daemon` | **Planned** | wedge100s-bmc-poller.timer and wedge100s-i2c-poller.timer both active; all /run/wedge100s/ cache files exist and mtime < 30 s | — |
| Transceiver info & DOM | `stage_11_transceiver` | Exists | xcvrd populates TRANSCEIVER_INFO within 120 s; TRANSCEIVER_STATUS within 600 s; DOM N/A acceptable for passive DAC | DOM verification requires active optics; blocked by Phase 25 |
| Interface counters | `stage_12_counters` | Exists | flex counter enabled; show interfaces counters non-empty; sonic-clear counters exits 0 | — |
| Link status | `stage_13_link` | Exists | Ethernet16/32/48/112 operationally UP; show interface output has correct speed/FEC; LLDP neighbor present on each UP port | Ethernet104/108 DOWN (Phase 25 physical blocker) |
| Dynamic port breakout | `stage_14_breakout` | Exists | config interface breakout accepted; sub-ports appear; config reload path tested; breakout reversed on cleanup | Port 17 (Ethernet64) transceiver detection issue; Phase 24 pending |
| Auto-neg & FEC | `stage_15_autoneg_fec` | Exists | RS-FEC (CL91) enabled on 100G ports; link stays up with RS-FEC; FC-FEC rejected for 100G; autoneg CLI accepted | Auto-neg is CLI-only; SAI does not program ASIC |
| Port channel / LAG | `stage_16_portchannel` | Exists | PortChannel1 UP; L3 ping to EOS peer 10.0.1.0 succeeds; LAG failover 0% loss; member re-joins within 15 s | Requires EOS peer reachable |
| State restore audit | `stage_17_restore` | **Planned** | After full suite run: no config changes persisted; interface speeds unchanged; no unexpected entries in CONFIG_DB | Runs last; reads CONFIG_DB snapshot taken at suite start |
| Platform status report | `stage_18_report` | **Planned** | Generates `tests/reports/PLATFORM_STATUS_<date>.md`; sections for each subsystem; machine-readable JSON summary | Not yet implemented; requires lib/report.py extension |

**175 tests currently passing** (2026-03-15, stages 01–16 excluding gaps listed above).

---

## 3. Hardware Dependency Matrix

| Stage | EOS Peer Required | Populated Transceivers Required | PSU1 AC Power Required | Notes |
|---|---|---|---|---|
| stage_01_eeprom | No | No | No | Self-contained EEPROM read |
| stage_02_system | No | No | No | Platform init + daemon files |
| stage_03_platform | No | No | No | I2C topology + CPLD |
| stage_04_thermal | No | No | No | BMC reads via daemon |
| stage_05_fan | No | No | No | BMC reads via daemon |
| stage_06_psu | No | No | No | PSU1 absent/unpowered; tests skip gracefully |
| stage_07_qsfp | No | Partial (≥1 DAC) | No | 9 ports have DAC cables installed |
| stage_08_led | No | No | No | CPLD LED register writes |
| stage_09_cpld (planned) | No | No | No | CPLD sysfs reads only |
| stage_10_daemon (planned) | No | No | No | systemd unit status + file mtime |
| stage_11_transceiver | No | Yes (≥1 present) | No | xcvrd STATE_DB check |
| stage_12_counters | No | No | No | ASIC counter infrastructure |
| stage_13_link | No | Yes (DAC on ports 5/9/13/29) | No | LLDP neighbor check requires EOS peer UP |
| stage_14_breakout | No | No | No | DPB runs on port 21 (has 4x25G cable) |
| stage_15_autoneg_fec | No | Yes (DAC on linked ports) | No | FEC verification needs live link |
| stage_16_portchannel | **Yes** | Yes (DAC on Eth16/32) | No | L3 ping requires PortChannel1 to EOS |
| stage_17_restore (planned) | No | No | No | CONFIG_DB diff only |
| stage_18_report (planned) | No | No | No | Aggregate summary generation |

**EOS peer access**: SSH via jump host `admin@192.168.88.12` only; direct SSH is blocked when LACP links are up. Tests that need EOS state use `ssh.run()` with the jump proxy configured in `target.cfg`.

---

## 4. State-Restore Contract

The contract governs what each stage may modify and what it must restore before
exiting. Violation of the contract makes subsequent stage results unreliable.

| Stage | Allowed to Change | Must Restore |
|---|---|---|
| stage_01..03 | Nothing | — (read-only) |
| stage_04_thermal | Nothing | — (read-only; thermalctld continues as-is) |
| stage_05_fan | `set_speed()` call (global fan speed) | Restore original speed after test; if test fails mid-run, speed may be left at test value |
| stage_06_psu | Nothing | — |
| stage_07_qsfp | Nothing | — (optoe1 EEPROM reads; no register writes) |
| stage_08_led | LED registers 0x3E, 0x3F | Restore to green (0x02) after each write test |
| stage_09_cpld (planned) | Nothing | — (sysfs reads only) |
| stage_10_daemon (planned) | Nothing | — (systemd status + file stat) |
| stage_11_transceiver | Nothing | — (STATE_DB is read-only from test perspective) |
| stage_12_counters | Counter clear (sonic-clear counters) | Document in test output that counters were cleared; do not rely on counter values in later stages |
| stage_13_link | Nothing | — |
| stage_14_breakout | Port breakout config on Ethernet80 | Apply `config interface breakout Ethernet80 '1x100G[40G]' -y -f -l` in teardown; verify port count returns to 32 |
| stage_15_autoneg_fec | FEC config (test RS-FEC toggle) | Restore RS-FEC to CL91 on linked ports before exit |
| stage_16_portchannel | PortChannel1 membership (failover test shuts a member) | Re-enable the shut member; verify LACP Selected state restored |
| stage_17_restore (planned) | Nothing | — (audit only; records diff, does not apply changes) |
| stage_18_report (planned) | Creates `tests/reports/PLATFORM_STATUS_<date>.md` | File creation is the intended output; no switch state changes |

**Prohibited at all times**: `docker rm -f pmon` (hangs I2C bus, requires power cycle). Use `sudo systemctl stop pmon` for graceful shutdown.

---

## 5. SONiC Feature Coverage

This table records which SONiC platform features are relevant to Wedge 100S-32X,
which are verified by the test suite, and which are intentionally out of scope.

| SONiC Feature | Applicable | Test Coverage | Notes |
|---|---|---|---|
| sonic_platform Python API (chassis/thermal/fan/psu/sfp) | Yes | stages 04–08 | Full Python API verified |
| System EEPROM (decode-syseeprom / syseepromd) | Yes | stage_01 | syseepromd reads from /run/wedge100s/syseeprom cache (Phase 20 done) |
| pmon container (thermalctld, xcvrd, ledd, syseepromd) | Yes | stages 04, 05, 06, 08, 11 | pmon bind-mounts /run/wedge100s/ from host |
| xcvrd (transceiver manager) | Yes | stage_11 | TRANSCEIVER_INFO/STATUS in STATE_DB |
| Interface counters (syncd flex counter) | Yes | stage_12 | PORT_STAT at 1000 ms |
| LLDP | Yes | stage_13 | lldpd running; 4 front-panel + 1 mgmt neighbor |
| Dynamic port breakout (DPB) | Yes | stage_14 | Requires flex BCM config; sub-port pre-allocation |
| FEC configuration (RS-FEC CL91) | Yes | stage_15 | Required for 100GBASE-CR4 to Arista |
| Auto-negotiation | Partial | stage_15 | CLI accepted; SAI does not program ASIC (phy_an_c73=0x0) |
| Port channel / LAG (teamd) | Yes | stage_16 | PortChannel1 verified with failover |
| BGP / FRR | Partial | Not yet implemented | Phase 23 pending; service is masked |
| Streaming telemetry (gNMI) | Partial | Not implemented | Phase 19 pending (low priority) |
| Media settings (media_settings.json) | Partial | Not implemented | Phase 16 pending; no active optics in lab |
| system-health (chassis LED) | Partial | stage_08 (LED write) | chassis.set_status_led() not yet implemented (Phase 21) |
| IPMI / Redfish BMC management | No | — | BMC uses Facebook-OpenBMC REST; no IPMI KCS; TTY + C daemon is optimal |
| PDDF framework | No | — | PDDF listed as dependency in build but not used at runtime for this platform |
| I2C mux kernel drivers (i2c_mux_pca954x) | No | — | Intentionally absent (EOS-P2 hidraw arch); virtual buses i2c-2..41 do not exist |
| QSFP LP_MODE / RESET | No | — | Pins on mux board; not host-accessible |
| Per-tray fan speed control | No | — | Hardware supports only global fan speed via BMC shell command |
| Speed change (dynamic serdes) | No | — | Static BCM config; speed change is CONFIG_DB-layer only |

---

## 6. Test Report Format

`stage_18_report` (planned) will generate a Markdown file at:

```
tests/reports/PLATFORM_STATUS_<YYYY-MM-DD>.md
```

### Sections

1. **Header** — target hostname, kernel version, SONiC version string,
   timestamp of report generation.

2. **Pass/Fail Summary** — table of all test stages with pass count, fail
   count, skip count, and elapsed time.

3. **Subsystem State Snapshot** — one subsection per subsystem:
   - Thermal: all 8 sensor names, current temperatures, threshold values
   - Fan: all 5 tray names, RPM, direction, speed percentage
   - PSU: both PSUs, presence, power-good, voltage, current
   - Transceiver: 32-port presence table, identifier byte for present ports
   - Link: all ports with oper status, speed, FEC mode, neighbor (if LLDP)
   - CPLD: version register, LED register values

4. **Gap Summary** — features not covered by current test suite (auto-populated
   from the coverage table above), with phase references for planned work.

5. **Machine-Readable Annex** — JSON block containing the full raw data
   collected during the report run, suitable for diffing across runs.

### Implementation Notes

The report runner path already exists in `run_tests.py` (`--report` flag) and
dispatches to `lib/report.py` `REPORTERS` dict keyed by stage name. Current
reporters are defined for stages 01–16. `stage_18_report` will add a
`generate_report()` function that calls each reporter, aggregates the results,
and writes the Markdown file. The `--report` output currently goes to stdout
only; `stage_18_report` will add `--output-file` support.

---

## Appendix: File Locations

| Item | Path |
|---|---|
| Test stages | `tests/stage_NN_<name>/test_<name>.py` |
| Shared fixtures | `tests/conftest.py` |
| SSH client | `tests/lib/ssh_client.py` |
| Report helpers | `tests/lib/report.py` |
| Runner | `tests/run_tests.py` |
| Target config | `tests/target.cfg` (gitignored) |
| Config template | `tests/target.cfg.example` |
| Phase list | `tests/STAGED_PHASES.md` |
| Architecture spec | `tests/ARCHSPEC.md` |
| Investigation notes | `tests/notes/` |
| Generated reports | `tests/reports/` (planned; gitignored) |
