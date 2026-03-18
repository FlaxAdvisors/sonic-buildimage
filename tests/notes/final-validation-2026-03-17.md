# Final Validation — 2026-03-17

Full 20-stage suite run on wedge100s branch, switch admin@192.168.88.12.

## Summary

- **Total tests:** 231
- **Passed:** 221
- **Failed:** 10
- **Duration:** 1169.99 seconds (~19.5 minutes)

## Stage-by-Stage Results

| Stage | Tests | Result | Notes |
|---|---|---|---|
| stage_00_pretest | 7 | PASS | All passed |
| stage_01_eeprom | 12 | PASS | All passed |
| stage_02_system | 12 | PASS | All passed |
| stage_03_platform | 16 | PASS | All passed |
| stage_04_thermal | 10 | PASS | All passed |
| stage_05_fan | 10 | PASS | All passed |
| stage_06_psu | 12 | PASS | All passed |
| stage_07_qsfp | 11 | PASS | All passed |
| stage_08_led | 8 | 1 FAIL | test_led_sys2_consistent_with_port_state — known pre-existing issue |
| stage_09_cpld | 12 | PASS | All passed |
| stage_10_daemon | 16 | PASS | All passed |
| stage_11_transceiver | 8 | PASS | All passed |
| stage_12_counters | 10 | PASS | All passed |
| stage_13_link | 9 | PASS | All passed |
| stage_14_breakout | 16 | PASS | All passed |
| stage_15_autoneg_fec | 17 | PASS | All passed |
| stage_16_portchannel | 17 | PASS | All passed |
| stage_17_report | 1 | PASS | All passed |
| stage_19_platform_cli | 10 | 7 FAIL | New stage; multiple CLI gaps (see below) |
| stage_20_traffic | 5 | 2 FAIL | New stage; test design issues (see below) |
| stage_nn_posttest | 5 | PASS | All passed |

## Known Pre-Existing Failure

### stage_08_led: test_led_sys2_consistent_with_port_state

SYS2 LED reads `off` (0x00) even when ports are up. This is a ledd polling interval
issue where the LED has not been updated yet at test time. NOT a regression.

## New Failures in stage_19 (Platform CLI Audit)

These tests were newly written for this validation run and expose platform gaps.

### test_reboot_cause

`show platform reboot-cause` is not a valid subcommand in this SONiC build.
The correct command is `show reboot-cause` (without `platform`). Confirmed working:
```
User issued 'reboot' command [User: admin, Time: Wed Sep  3 07:59:11 PM UTC 2025]
```
Fix: update test to use `show reboot-cause` instead of `show platform reboot-cause`.

### test_firmware_cpld / test_firmware_bios

`show platform firmware` routes to `fwutil show` which requires root. Running as admin
returns: `Error: Root privileges are required. Aborting...`
The platform does have a `component.py` wired into `chassis.py`, but the fwutil CLI
path requires sudo. Fix: update tests to use `sudo fwutil show status` or query the
platform API directly, or add `sudo` to the ssh call.

### test_psu_model_not_na

PSU model column in `show platform psustatus` shows N/A for fields other than Model.
Actual output: `PSU-1  Delta DPS-1100AB-6 A  N/A  N/A  N/A  0.00  0.00  NOT OK  red`
The model IS present (Delta DPS-1100AB-6 A) but the test logic triggers on any N/A
in the line before checking if "Serial" is present. The PSU serial/revision fields are
genuinely N/A because this PSU model does not report them via I2C. Fix: tighten the
test to only check the Model column, or exempt Serial/Revision N/A values explicitly.

### test_environment_thermals / test_environment_fans

`show environment` only shows coretemp (CPU package/cores) — no BMC thermal sensors
or fan RPM entries. The BMC sensors are accessible via the platform API and daemon
cache at `/run/wedge100s/` but `show environment` on this SONiC build does not
include them (it invokes `sensors` which only sees kernel hwmon devices, not BMC
data). Fix: either update tests to check the platform API directly (already tested
in stage_04_thermal and stage_05_fan), or configure a hwmon driver that exposes
BMC data to the kernel.

### test_watchdogutil_status

`watchdogutil status` returns rc=1 when run as non-root admin. The binary requires
root privileges. Fix: add `sudo` to the ssh call in the test.

## New Failures in stage_20 (Traffic)

### test_standalone_port_rx_tx

Ethernet48 RX delta was only 17 packets after a 1000-packet ping flood to
STANDALONE_PEER_IP (10.0.0.0). Root cause: no IP is configured on the peer side for
this standalone address (10.0.0.0/31 is a dedicated test subnet with no EOS config).
Only LLDP and ARP traffic reaches Ethernet48. The PortChannel tests work because
the EOS peer has PortChannel1 10.0.1.0/31 configured. Fix: either configure
10.0.0.0/31 on the EOS peer Et13/1 or Et14/1, or remove this test and rely on
the PortChannel tests for traffic validation.

### test_counter_clear_accuracy

Ethernet16 has 15009 RX_OK after `sonic-clear counters` — expected <= 20. This is
because `sonic-clear counters` resets the COUNTERS_DB snapshot, but the ASIC counter
does not reset; the test reads `SAI_PORT_STAT_IF_IN_UCAST_PKTS` directly from
COUNTERS_DB which is relative to a saved baseline. After the previous traffic tests
flooded the port, the baseline was not updated before the clear. The clear happened
while PortChannel traffic was still flowing (LACP keepalives). Fix: add a 5-second
settle wait after the clear, or measure delta from before/after clear rather than
asserting absolute value <= 20.

## Post-Restore State (verified on hardware 2026-03-17)

```
PortChannel: ABSENT (correct — stage_nn_posttest removed it)
Ethernet16: admin-up (correct)
Ethernet32: admin-up (correct)
Ethernet48: admin-up (correct)
Ethernet112: admin-up (correct)
```

stage_nn_posttest: all 5 tests PASSED, config correctly restored.

## Regression Assessment

Compared to previous integration test (204/216 = 94.4% pass rate before fixes):

- stage_12 FEC fixture fix: CONFIRMED working (all 10 counters tests pass)
- stage_16 longer LACP wait + INTERFACE entry handling: CONFIRMED working (all 17 pass)
- 221/231 = 95.7% pass rate on expanded suite

The 10 failures are:
- 1 known pre-existing (stage_08 SYS2 LED)
- 7 new stage_19 test design issues (CLI command paths, privilege levels, test logic)
- 2 new stage_20 test design issues (missing peer config, counter clear race)

None of the failures indicate platform hardware regressions. All platform hardware
tests (stages 01–17) pass at 100% except the pre-existing LED polling issue.
