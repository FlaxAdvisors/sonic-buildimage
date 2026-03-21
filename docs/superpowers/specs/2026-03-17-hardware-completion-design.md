# Wedge 100S-32X Hardware Support Completion — Design Spec

**Date:** 2026-03-17
**Branch:** `wedge100s`
**Objective:** Bring the platform port to production quality before advancing to L2/L3 network software phases.

---

## 1. Problem Statement

Stages 01–10 (hardware sensors, LEDs, CPLD, daemon infrastructure) are complete and passing.
Stages 11–16 (transceiver info/DOM, interface counters, link pipeline, speed/breakout, FEC/autoneg, portchannel) are fully written and pass 202/203 tests against hardware (baseline 2026-03-17).

Additional gaps exist in the platform Python API that prevent full production readiness:

- `chassis.get_base_mac()` — not implemented; affects MAC assignment at boot
- `chassis.get_reboot_cause()` — not implemented; `show platform reboot-cause` returns empty
- `chassis.get_port_or_cage_type()` — not implemented; xcvrd falls back to default port type
- PSU model/serial — returns N/A; `show environment power` is incomplete
- Thermal low thresholds — `get_low_threshold()` / `get_low_critical_threshold()` not set
- Component API — no CPLD/BIOS firmware version reporting
- Test infrastructure — stage 17 is a read-only audit, not a save/restore mechanism, which makes config-modifying tests (breakout, speed, LACP) unsafe to repeat on a clean baseline

---

## 2. Approach: Option C — Infrastructure-First, Then Parallel

### Phase ordering

```
Phase 0   Test infrastructure redesign (stage 00 / stage 18 rename)
Phase 1   Baseline run of stages 01–16 — identify failures        [COMPLETE]
Phase 2   Platform API production completions  |  bmc.py TTY fix  (parallel)
Phase 3   New test stages (platform CLI audit, traffic forwarding)
Phase 4   Full suite run, all 20 stages passing, report generated
```

The key insight driving this order: stages 14 (breakout), 15 (FEC/autoneg), and 16 (portchannel) modify CONFIG_DB. Without a reliable pre/post config save-restore mechanism, any failure mid-suite leaves the switch in an unknown state. Infrastructure must come first.

---

## 3. Test Infrastructure Redesign

### 3.1 Stage Renumbering

Current:
```
stage_17_restore   — read-only audit (insufficient; deleted)
stage_18_report    — platform status report (incomplete; renumbered)
```

New (stages stage_00 through stage_20 plus stage_nn_posttest):
```
stage_00_pretest       — NEW: save current config + apply clean-boot template
stage_01 … 16          — unchanged (all operate on clean-boot baseline; each configures/unconfigures its own deps)
stage_17_report        — report (was stage_18; runs on clean-boot state)
stage_19_platform_cli  — NEW: full platform CLI audit (§6.1)
stage_20_traffic       — NEW: traffic forwarding verification (§6.2)
stage_nn_posttest      — NEW: restore original config + verify restoration
```

`stage_nn_posttest` uses the letter prefix `nn` so that ASCII `n` (110) > ASCII `9` (57) — it always sorts after any digit-prefixed stage, without ever needing renaming when new numbered stages are added.

The old `stage_17_restore` is deleted; its useful checks migrate into `stage_nn_posttest`.

Execution order in `run_tests.py` relies on alphabetical directory sort. `stage_nn_posttest` naturally sorts last. No pytest ordering plugin is needed or assumed.

### 3.2 Stage 00 — Pre-Test (Save + Apply Clean-Boot Template)

**File:** `tests/stage_00_pretest/test_pretest.py`

```
Actions (in order):
  1. sudo config save /etc/sonic/pre_test_config.json   — persistent snapshot
  2. cp /etc/sonic/pre_test_config.json /tmp/            — in-memory backup
  3. sudo config reload /etc/sonic/clean_boot.json -y   — apply clean-boot template
  4. Wait for pmon daemons to reach RUNNING (up to 90 s after reload)
  5. Verify BREAKOUT_CFG seeded for all 32 ports
  6. Write /run/wedge100s/test_suite_active with ISO timestamp
```

Actions are implemented as a session-scoped fixture in stage 00 (not in `conftest.py` — the fixture belongs to the stage, not the global session). Any failure calls `pytest.exit()` from the fixture body to abort the suite before any test runs. `pytest.exit()` is called from the fixture, not from a test function, to ensure clean session teardown.

`config reload` is the correct command: it stops swss/teamd, flushes CONFIG_DB, then repopulates from the file — guaranteeing a complete replace rather than a merge. Service restart adds ~60–90 seconds to stage 00 but is unavoidable for a reliable clean state.

The `clean_boot.json` template is version-controlled at `tests/fixtures/clean_boot.json`.

### 3.3 Clean-Boot State Definition

The `clean_boot.json` template encodes the following target state:

| Item | Clean-Boot Value | Rationale |
|------|-----------------|-----------|
| All 32 QSFP ports | admin-up, 100G | Matches port_config.ini default |
| FEC — Ethernet16/32/48/112 (connected) | RS-FEC (`rs`) | Required for link to Arista peer |
| FEC — all other 28 ports | none (default) | Not connected; stage 15 tests both modes |
| Breakout modes — all 32 ports | `1x100G[40G]` | Stage 14 does its own breakout/restore |
| BREAKOUT_CFG — all 32 ports | `1x100G[40G]` | Required for `config interface breakout` |
| PortChannel1 | exists, Ethernet16+32 members, IP 10.0.1.1/31 | Stage 16 requires this pre-existing |
| teamd feature | enabled | Stage 16 requires it |
| Ethernet48 | admin-up, 100G, RS-FEC, routed, IP 10.0.0.1/31 | Stage 13 standalone connected port |
| Ethernet112 | admin-up, 100G, RS-FEC, routed | Stage 13 standalone connected port |
| VLANs | none | Stages create their own if needed |
| BGP / routing | none | L3 is a later phase |
| pmon | running, all daemons RUNNING | Prerequisite for stages 11–13 |

### 3.4 stage_nn_posttest — Post-Test (Restore + Verify)

**File:** `tests/stage_nn_posttest/test_posttest.py`

Named with `nn` prefix so ASCII `n` (110) > ASCII `9` (57) — always sorts last after any digit-prefixed stage.

```
Actions (in order):
  1. sudo config reload /etc/sonic/pre_test_config.json -y   — full replace from snapshot
  2. sudo config save -y                                      — persist the restore
  3. Wait for pmon daemons to stabilize (up to 90 s)
  4. Verify PortChannel1 oper_status=up (if it was up in snapshot)
  5. Verify connected port link states match snapshot
  6. rm /run/wedge100s/test_suite_active
```

`config reload` (not `config load`) is mandatory here: `config load` calls `mod_config()` which merges rather than replaces CONFIG_DB — it cannot delete ghost keys created by test stages (e.g., VLAN entries, broken-out sub-port entries). `config reload` flushes and repopulates from the snapshot file, guaranteeing exact state recovery.

Failure in stage_nn_posttest is non-fatal for the test suite exit code (all test results already recorded) but the tests in this stage fail with clear error messages logging what did not restore.

### 3.5 stage_17_report — Report Generation

The report stage runs on the clean-boot baseline (before stage_nn_posttest restores user config). It captures the switch state during the test run. Any stage_nn_posttest restore failures are already logged when the report runs.

The existing `stage_18_report/test_report.py` is moved to `stage_17_report/` and expanded (§6.3).

---

## 4. Platform API Production Completions

### 4.1 `chassis.get_base_mac()`

**Current state:** not implemented (base class raises `NotImplementedError`).
**Impact:** SONiC `orchagent` / MAC assignment may fall back to a random MAC.
**Implementation:** Read TLV type `0x24` from `SysEeprom.get_eeprom()`. The dict key is `'0x24'` (confirmed by inspecting TlvInfo type-code formatting in `eeprom.py`). `SysEeprom` already decodes the MAC bytes into `XX:XX:XX:XX:XX:XX` string form.

```python
def get_base_mac(self):
    info = self._eeprom.get_eeprom()
    return info.get('0x24') or info.get('Base MAC Address')
```

**Test (stage 19):** `get_base_mac()` returns a string matching the value in `show platform syseeprom`.

### 4.2 `chassis.get_reboot_cause()`

**Current state:** not implemented.
**Impact:** `show platform reboot-cause` returns no data.

**Implementation — file-based path only (primary):**
SONiC's ordered shutdown script writes `/var/log/sonic/reboot-cause/previous-reboot-cause.txt` on graceful reboots. Read this file and return its first non-empty line as the cause description paired with `REBOOT_CAUSE_NON_HARDWARE` (for software-initiated reboots) or `REBOOT_CAUSE_POWER_LOSS` (for power-cycle events).

No BMC query is implemented for this method. The BMC's OpenASPEED instance does not expose a structured reboot-cause register accessible via the TTY interface, and parsing `journalctl` over a 57600-baud serial console is too slow and fragile for a synchronous `get_reboot_cause()` call. If the cause file is absent (post power-cycle or watchdog), return `(REBOOT_CAUSE_POWER_LOSS, "")` as the default — the most common non-software reboot type on this hardware.

**Test (stage 19):** `show platform reboot-cause` exits 0 and returns a non-empty string for the last cause.

### 4.3 `chassis.get_port_or_cage_type()`

**Current state:** not implemented (base returns `None`).
**Impact:** xcvrd may misidentify port type, affecting optical power alarm thresholds.
**Implementation:** All 32 ports are QSFP28. Return `SFP_PORT_TYPE_BIT_QSFP28` for indices 1–32.

```python
def get_port_or_cage_type(self, index):
    if 1 <= index <= NUM_SFPS:
        return self.SFP_PORT_TYPE_BIT_QSFP28
    return None
```

### 4.4 Thermal Low Thresholds

**Current state:** `get_low_threshold()` / `get_low_critical_threshold()` not implemented (return `None`).
**Impact:** `thermalctld` may log warnings about missing thresholds.
**Implementation:** Static defaults — operational range for this hardware is 0–40°C ambient.

| Sensor | Low Warning | Low Critical |
|--------|-------------|--------------|
| CPU core | 0°C | -10°C |
| TMP75 sensors (1–7) | 0°C | -10°C |

### 4.5 PSU Model and Serial

**Current state:** `get_model()` / `get_serial()` return N/A.
**Impact:** `show environment power` shows empty model column.

**Implementation — static string (primary):** PMBus MFR_MODEL (0x9A) requires an SMBus block-read transaction. The current `bmc.py` only exposes byte/word reads (`i2cget`). Extending it to block-read mode (`i2cget -f -y BUS ADDR REG i`) requires new code and hardware verification that is out of scope for this phase.

Primary implementation: return the known static string `"Delta DPS-1100AB-6 A"` for model and `"N/A"` for serial. This gives a meaningful `show environment power` output immediately. Block-read PMBus model/serial is deferred to a future enhancement.

### 4.6 Component API (CPLD and BIOS Firmware Versions)

**Current state:** no `Component` objects; `show platform firmware` returns nothing.
**Impact:** Cannot track firmware versions via SONiC CLI.

**Implementation:** New `sonic_platform/component.py` with two read-only instances:

| Component | Version Source | Confirmed Available |
|-----------|---------------|---------------------|
| CPLD | `/sys/bus/i2c/devices/1-0032/cpld_version` sysfs attr | Verify live before impl (see §4.6 note) |
| BIOS | `dmidecode -s bios-version` | Standard x86 tool, available on SONiC host |

`install_firmware()` returns `False` with explanation string. Populate `chassis._component_list` in `Chassis.__init__()`.

**Note:** Confirm `cpld_version` sysfs attribute exists on the running switch before implementing (`cat /sys/bus/i2c/devices/1-0032/cpld_version`). The `wedge100s_cpld` driver exposes it but must be verified live before the Component class commits to that path.

---

## 5. Baseline Run Results (2026-03-17)

Stages 01–16 run against hardware: **202 passed, 1 failed, 281 seconds.**

Full output: `tests/reports/baseline_stages_01_16_2026-03-17.txt`

### Failure

| Stage | Test | Error | Root Cause |
|-------|------|-------|------------|
| 03 | `test_bmc_uptime_contains_days_or_min` | TTY returned `'i2cget -f -y 7 0x59 0x88 wroot@hare-lorax-bmc:~#\n'` | `bmc.py` does not flush the TTY receive buffer before issuing the next command. A prior test's `i2cget` word-read response was still in the buffer when `uptime` was sent. |

### Fix

In `bmc.py` `send_command()`: drain the TTY receive buffer (non-blocking read / `select` with zero timeout) before writing the command string. 3–4 lines.

### Verified Passing (key results)

- Stage 11: xcvrd populates STATE_DB TRANSCEIVER_INFO for all 9 present modules; `get_xcvr_api()` returns valid Sff8636Api; DOM passive-DAC gracefully skipped.
- Stage 12: Flex counter enabled (1000 ms), 32 ports in COUNTERS_PORT_NAME_MAP, all SAI_PORT_STAT fields present, RX_OK increments on link-up ports, counter clear works.
- Stage 13: RS-FEC in ASIC_DB, oper=up on 4 connected ports, LLDP discovers rabbit-lorax with correct Et13/1–16/1 peer mapping.
- Stage 14: BREAKOUT_CFG already seeded; speed 40G↔100G round-trips; platform.json/hwsku.json fully validated.
- Stage 15: RS-FEC in ASIC_DB, FC-FEC rejected, autoneg writes ASIC_DB attribute, advertised speeds propagate.
- Stage 16: LACP(A)(Up), both members Selected, teamdctl state=current, LAG in APP_DB/STATE_DB/ASIC_DB, ping 0% loss, failover+recovery completes.

---

## 6. New Test Stages

### 6.1 Stage 19 — Platform CLI Audit

Verifies that all SONiC platform-facing CLI commands produce correct output backed by the platform Python API.

**File:** `tests/stage_19_platform_cli/test_platform_cli.py`

| Test | Command / API | Pass Criteria |
|------|--------------|---------------|
| `test_base_mac` | `show platform syseeprom` | Base MAC Address field present and formatted `XX:XX:XX:XX:XX:XX` |
| `test_base_mac_api` | Platform API `chassis.get_base_mac()` | Matches syseeprom output |
| `test_reboot_cause` | `show platform reboot-cause` | Exits 0, non-empty cause string |
| `test_firmware_cpld` | `show platform firmware` | CPLD entry present with version string |
| `test_firmware_bios` | `show platform firmware` | BIOS entry present with version string |
| `test_psu_model` | `show platform psustatus` | PSU model column non-empty (not "N/A") |
| `test_environment_temp` | `show environment` | All 8 thermal sensors listed with °C values |
| `test_environment_fan` | `show environment` | All 10 fans (5×F+R) listed with RPM |
| `test_environment_power` | `show environment` | PSU voltage/current/power populated |
| `test_transceiver_info` | `show interfaces transceiver` | TRANSCEIVER_INFO for all present modules |
| `test_watchdogutil_status` | `watchdogutil status` | Exits 0; output documents stub status |
| `test_port_cage_type` | Platform API `get_port_or_cage_type(1)` | Returns `SFP_PORT_TYPE_BIT_QSFP28` bitmask |

### 6.2 Stage 20 — Traffic Forwarding Verification

Verifies the ASIC forwards packets over connected links and that SAI counters accurately reflect the traffic. Scope is limited to 100G links (loopback-connected or peer-connected). 25G breakout port traffic testing is deferred until a loopback cable for the breakout segment is available.

**File:** `tests/stage_20_traffic/test_traffic.py`

**Topology:**
```
hare-lorax (SONiC)                rabbit-lorax (Arista EOS)
  PortChannel1 (Et16+32) ←DAC→   Port-Channel1 (Et13/1+14/1)   10.0.1.0/31
  Ethernet48             ←DAC→   Et15/1                         10.0.0.0/31
  Ethernet112            ←DAC→   Et16/1
```

| Test | Method | Pass Criteria |
|------|--------|---------------|
| `test_nonbreakout_rx_counters_increment` | ping flood `-f -c 5000` to peer over PortChannel1; read counter delta | RX_OK delta ≥ 4500 (≥ 90% of sent) |
| `test_nonbreakout_tx_counters_increment` | same flood; check TX on PortChannel members | TX_OK delta ≥ 4500 |
| `test_standalone_port_rx_tx` | ping flood to `10.0.0.0` via Ethernet48; check counter delta | Ethernet48 RX_OK and TX_OK increment |
| `test_fec_error_rate_100g` | read `SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES` before/after 5 s traffic | correctable FEC rate < 1e-6/s per port |
| `test_counter_clear_accuracy` | `sonic-clear counters`; wait 2 s; re-read | RX_OK for all connected ports ≤ 20 (LLDP only) |

No `iperf3` dependency — all tests use `ping -f` (available on SONiC host) and counter deltas read from COUNTERS_DB via `redis-cli`. The ping flood generates sufficient packet rate to verify ASIC forwarding without requiring a bandwidth tool.

### 6.3 Stage 17 Report — Expansions

The existing report is augmented with sections for:
- Component firmware versions (CPLD, BIOS)
- Base MAC address
- Last reboot cause
- Per-port DOM temperature/voltage/Rx-power for optical modules (N/A for passive DAC)
- FEC correctable/uncorrectable error counters for connected ports
- LLDP neighbor table

---

## 7. Implementation Plan

### Phase 2 — Parallel (Week 1–2)

**Track A: Test Infrastructure**

| Task | Files |
|------|-------|
| Create `tests/fixtures/clean_boot.json` template | new file |
| Stage 00 pretest: save + `config reload` clean template | `tests/stage_00_pretest/test_pretest.py` |
| Stage 18 posttest: `config reload` restore + verify | `tests/stage_nn_posttest/test_posttest.py` |
| Move + expand `stage_18_report` → `stage_17_report` | directory rename + `test_report.py` |
| Delete `stage_17_restore` | directory removed |

**Track B: Platform API**

| Task | Files |
|------|-------|
| `bmc.py` TTY buffer flush fix | `sonic_platform/bmc.py` |
| `chassis.get_base_mac()` | `sonic_platform/chassis.py` |
| `chassis.get_reboot_cause()` (file-based) | `sonic_platform/chassis.py` |
| `chassis.get_port_or_cage_type()` | `sonic_platform/chassis.py` |
| Thermal low thresholds | `sonic_platform/thermal.py` |
| PSU model static string | `sonic_platform/psu.py` |
| Component API (CPLD + BIOS) | `sonic_platform/component.py` (new), `sonic_platform/chassis.py` |
| Build + deploy new .deb | `make target/debs/trixie/…_1.1_amd64.deb` + scp + dpkg |

### Phase 3 — New Test Stages (Week 2–3)

| Task | Files |
|------|-------|
| Stage 19 platform CLI tests | `tests/stage_19_platform_cli/test_platform_cli.py` |
| Stage 20 traffic tests | `tests/stage_20_traffic/test_traffic.py` |
| Stage 17 report expansion | `tests/stage_17_report/test_report.py`, `tests/lib/report.py` |

### Phase 4 — Full Suite Validation (Week 3)

Full 20-stage run: `./run_tests.py` — all stages green.

---

## 8. Success Criteria

All must be true before advancing to Phase 2 (L2 networking):

- [ ] `./run_tests.py` runs all 20 stages (stage_00 through stage_20) with 0 failures, 0 errors
- [ ] Stage 00 applies clean-boot state via `config reload`; stage 18 restores pre-test state via `config reload`
- [ ] `show platform syseeprom` shows Base MAC Address
- [ ] `show platform reboot-cause` returns non-empty cause string
- [ ] `show platform firmware` lists CPLD and BIOS versions
- [ ] `show environment` shows all 8 thermals, 10 fans, 2 PSUs with model name
- [ ] `show interfaces transceiver` shows TRANSCEIVER_INFO for all present modules
- [ ] Stage 20: 100G connected ports pass 5000-packet flood with ≥ 90% counter increment
- [ ] Stage 20: FEC correctable error rate < 1e-6/s on connected ports
- [ ] No stale daemon cache files (syseeprom cache age < 60 s after pmon startup)
- [ ] Switch config is identical before and after a full test suite run (verified by stage 18)

---

## 9. Known Hardware Constraints (Fixed; Not Bugs)

| Constraint | Impact |
|-----------|--------|
| QSFP LP_MODE / RESET not wired to host CPU | `set_lpmode()` / `reset()` return False — correct |
| Hardware watchdog owned by BMC | `watchdogutil arm/disarm` returns -1 — stub is correct |
| FC-FEC (CL74) not in Tomahawk SAI | `config interface fec fc` rejected — correct behavior |
| Auto-negotiation (phy_an_c73=0) hardware-disabled | `autoneg` propagates to ASIC_DB but does not change physical layer |
| PSU serial not readable without SMBus block-read | `get_serial()` returns N/A — deferred enhancement |
| PSU1 AC input unplugged in lab | `get_powergood_status(1)` returns False — lab condition only |
| Ethernet104/108 CWDM4 links physically blocked | Not a software issue |
| 25G breakout traffic test deferred | No loopback cable available for breakout segment |
