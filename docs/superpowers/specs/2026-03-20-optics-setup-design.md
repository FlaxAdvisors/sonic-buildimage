# Design: Wedge100S-32X Optics Setup — CLI Fixes + Link Bring-Up

**Date:** 2026-03-20
**Branch:** wedge100s
**Status:** Approved

---

## Overview

Four 100G QSFP28 optical transceivers are installed in the Accton Wedge100S-32X and cabled to a peer Arista EOS Wedge100S-32X. All four ports are currently oper-down. In parallel, `show interfaces transceiver` CLI commands exhibit inconsistent behavior for SFF-8636 modules on this SONiC build.

This document covers: (1) fixing four sonic-utilities CLI issues, (2) investigating and resolving the link-down state on all four optical ports, and (3) writing a platform operator reference guide.

---

## Hardware Context

| SONiC Port | Type | Fiber | Arista Port | Peer Transceiver |
|---|---|---|---|---|
| Ethernet100 | QSFP28-SR4-100G (Arista) | MPO-12 | Et26/1 | QSFP28-SR4-100G (Arista) |
| Ethernet104 | QSFP28-LR4-100G (Arista) | LC duplex | Et27/1 | QSFP-100G-LR4-M (Proficium) |
| Ethernet108 | QSFP28-SR4-100G (Arista) | MPO-12 | Et28/1 | QSFP28-SR4-100G (Arista) |
| Ethernet116 | C100QSFPCWDM400B (ColorChip) | LC duplex | Et30/1 | C100QSFPCWDM400B (ColorChip) |

All four SONiC ports: admin-up, oper-down, FEC=rs (RS-FEC configured).

**Current failure signatures (hardware-verified 2026-03-20):**

| Port | Rx LOS | TX Disable | DOM Rx Power | DOM TX Bias | Note |
|---|---|---|---|---|---|
| Ethernet100 | True (all lanes) | False | -inf | 0.0 mA | No signal from peer |
| Ethernet104 | False (all lanes) | False | -inf | 0.0 mA | Signal present; dom_capability N/A |
| Ethernet108 | False (all lanes) | False | -inf | 0.0 mA | Signal present; dom_capability N/A |
| Ethernet116 | False (all lanes) | **True (all lanes)** | +0.8 dBm | 0.0 mA | **Peer IS sending; our TX disabled** |

**LP_MODE note:** On the Wedge100S-32X, QSFP LP_MODE and RESET lines are driven by PCA9505 GPIO expanders on the I2C mux board (per ONL sfpi.c). They are not directly accessible from the host CPU. The platform sfp.py returns False for `get_lpmode()` by design. LP_MODE state must be checked and cleared via the BMC or the i2c-daemon.

---

## Track 1: sonic-utilities CLI Fixes

### Workflow

Patch Python files in-place on the target switch for rapid iteration. Once all four fixes are validated, commit the diffs to `src/sonic-utilities` (submodule) and rebuild the `.deb` once.

Installed CLI location on target: `/usr/bin/` (Click entry points) and `/usr/lib/python3/dist-packages/` (library modules).

### Fix 1 — `show interfaces transceiver status` (all-ports)

**Symptom:** Running without a port argument returns "Transceiver status info not applicable" for all SFF-8636 optical modules, even though per-port queries work correctly.

**Root cause (to confirm on target):** In `sfpshow`, the `convert_interface_sfp_status_to_cli_output_string()` function is shared between per-port and all-ports paths — there is no separate dispatch. The actual gate that produces `QSFP_STATUS_NOT_APPLICABLE_STR` is a `len(sfp_status_dict) > 2` threshold check. For SFF-8636 modules, xcvrd writes only `TRANSCEIVER_STATUS_SW` (cmis_state, status, error) with 3 keys, but does not populate `TRANSCEIVER_STATUS` with per-lane fields (`tx1disable`–`tx4disable`, `rx1los`–`rx4los`). When the combined dict has ≤ 2 relevant keys after merging, the "not applicable" branch fires. The per-port path works because it reads live hardware registers directly rather than relying solely on STATE_DB content.

**Fix:** Both per-port and all-ports paths call `convert_interface_sfp_status_to_cli_output_string()` which reads exclusively from STATE_DB — there is no live register read in either path. The fix is therefore in xcvrd (to populate `TRANSCEIVER_STATUS` per-lane keys for SFF-8636 modules) or in `convert_interface_sfp_status_to_cli_output_string()` (to handle the case where per-lane keys are absent by using the `TRANSCEIVER_STATUS_SW` fields or a direct sfputil call).

### Fix 2 — `show interfaces transceiver info <port>`

**Symptom:** Returns "SFP EEPROM Not detected" for Ethernet100 (SR4) while `show interfaces transceiver eeprom Ethernet100` shows full data. Works correctly for Ethernet104 (LR4) and Ethernet116 (CWDM4).

**Root cause (to debug on target):** In sfpshow, both `info` and `eeprom` call the same `convert_interface_sfp_info_to_cli_output_string()` function. Since they share internal code, the divergence is likely in the Click wrapper layer or in STATE_DB content timing rather than in a separate decode path. First diagnostic step: run `sfpshow info Ethernet100` directly (bypassing the `show interfaces` Click wrapper) to confirm whether the symptom reproduces. If it does not reproduce via sfpshow directly, the issue is in the Click wrapper. If it does, check whether `TRANSCEIVER_INFO|Ethernet100` has a field that differs from the other modules (encoding=`256B/257B` vs `NRZ`, or `dom_capability: N/A`) that might gate the output.

**Fix:** Once the divergence point is located, remove or correct the gate. The EEPROM is readable and STATE_DB is populated for Ethernet100, so "Not detected" is incorrect.

### New Capability 1 — `eeprom` DOM extension for SFF-8636

Extend `show interfaces transceiver eeprom` output to decode and display DOM sensor data for SFF-8636 modules when the module supports it. DOM sensor fields live in page 0 (lower memory, flat address space) of the SFF-8636 address map: temperature (bytes 22–23), voltage (bytes 26–27), RX power per lane (bytes 34–41), TX bias per lane (bytes 42–49), TX power per lane (bytes 50–57). Alarm/warning thresholds are in page 3 (upper memory) and are out of scope for this extension.

Only displayed when a live read succeeds and bytes are non-zero (to avoid printing zeros for dom_capability=N/A modules that don't populate DOM registers).

### New Capability 2 — `pm` SFF-8636 fallback

Extend `show interfaces transceiver pm` to fall back to `TRANSCEIVER_DOM_SENSOR` in STATE_DB (database 6) when the module type is SFF-8636 (not CMIS). The existing CMIS `pm` output uses a coherent-optics ZR schema (OSNR, CFO, DGD, Pre/Post-FEC BER, etc.) which does not apply to QSFP28 intensity-modulated modules. A new SFF-8636 rendering path is required with columns appropriate for SR4/LR4/CWDM4:

| Lane | Rx Power (dBm) | Tx Bias (mA) | Tx Power (dBm) | Temperature (C) | Voltage (V) |
|------|----------------|--------------|----------------|-----------------|-------------|

Temperature and Voltage are module-level (not per-lane) and are shown once, not repeated per row. This makes `pm` the consistent "show optical health" command for all installed module types.

---

## Track 2: Optics Bring-Up Investigation

### Priority order

1. **Ethernet116 (CWDM4)** — peer is sending, TX disable is the only blocker. Investigate first.
2. **Ethernet104 (LR4) and Ethernet108 (SR4)** — signal present, likely FEC or Arista config issue.
3. **Ethernet100 (SR4)** — no Rx signal at all; physical/peer-side issue to diagnose last.

### Ethernet116 Investigation Steps

**Background:** The TX disable register (SFF-8636 page 0, byte 86) is a volatile control register in the module's MCU RAM — it resets to 0x00 (all lanes enabled) on module power-cycle and cannot corrupt vendor calibration data. It is distinct from the non-volatile EEPROM regions that store vendor name, serial, and calibration constants. The four steps below are ordered from safest/most reversible to most direct.

1. **Check xcvrd logs** for any explicit TX disable event to identify whether SONiC set this state:
   ```
   sudo docker logs pmon 2>&1 | grep -i "tx.*disable\|disable.*tx\|Ethernet116"
   ```

2. **Restart pmon** (restarts xcvrd). If xcvrd set TX disable during a failed initialization sequence, it may clear it on a clean restart:
   ```
   sudo systemctl restart pmon
   sleep 15
   show interfaces transceiver status Ethernet116
   ```
   If TX disable is now False and link comes up, xcvrd initialization was the root cause — investigate why xcvrd disables TX on first init for this module type.

3. **BMC module reset** (assert/deassert RESET via OpenBMC GPIO). This physically resets the module and returns all volatile registers — including TX disable — to factory defaults without any I2C write:
   ```
   ssh root@192.168.88.13
   # Enumerate QSFP RESET GPIO lines: gpiodetect && gpioinfo <chip>
   # Assert RESET (active low, assert = 0):
   gpioset $(gpiofind QSFP_RESET_28)=0
   sleep 1
   # Deassert RESET:
   gpioset $(gpiofind QSFP_RESET_28)=1
   ```
   The exact GPIO chip and line name for port 28 (Ethernet116) must be confirmed via `gpiodetect` on the BMC. After reset, allow 2 seconds for module init, then check `show interfaces transceiver status Ethernet116`.

4. **Direct register write — last resort only, after steps 1–3 fail.** The TX disable register is volatile (not the non-volatile EEPROM storing calibration data), but use `write-eeprom` only if the module has retained TX disable state through a BMC reset and a confirmed fresh module power-cycle:
   ```
   sudo sfputil write-eeprom -p Ethernet116 -n 0 -o 86 -d 00
   ```
   Page 0, offset 86 (0x56), value 0x00 = all four lanes enabled. There is no `sfputil tx_enable` or `sfputil tx_disable` standalone CLI command in this build.

5. Observe link state after whichever step clears TX disable.

### Ethernet104 / Ethernet108 Investigation Steps

1. Check Arista peer FEC configuration on Et27/1 and Et28/1 via EOS CLI. Align SONiC to match (prefer matching peer rather than reconfiguring Arista).
2. Verify Arista admin-up state on both ports.
3. If FEC is the issue: try `config interface fec Ethernet104 none` as a diagnostic; restore to `rs` if that was correct.
4. Check TRANSCEIVER_DOM_SENSOR for TX bias after link-up attempt — if still 0 and dom_capability=N/A, treat 0 as unreliable for these modules.

### Ethernet100 Investigation Steps

1. Check Arista Et26/1 TX status via EOS.
2. Verify MPO cable is Type B (straight-through). 100GBASE-SR4 requires lane N to lane N end-to-end. Since both ends are QSFP28-SR4, both expect Type B. A Type A (flipped) cable reverses lane order and causes all-lane Rx LOS.
3. If Arista TX is up and cable is correct, check LP_MODE via BMC for this port.

### FEC Alignment

SONiC current: `rs` (RS-FEC) on all four ports. Expected correct config:
- SR4 (Ethernet100/108): RS-FEC — standard for 100GBASE-SR4
- LR4 (Ethernet104): RS-FEC or none — depends on peer; Arista LR4 typically uses RS-FEC but some deployments use none
- CWDM4 (Ethernet116): RS-FEC — standard for 100G CWDM4

If Arista peer has FEC=none on any port, align SONiC to match using `config interface fec EthernetX none`.

---

## Track 3: Platform Operator Reference Guide

**File:** `notes/SONiC-wedge100s-Optics-Setup-Guide.md`

### Sections

1. **Hardware Overview** — port table, fiber types, peer mapping, LP_MODE/RESET accessibility note.

2. **Transceiver CLI Command Reference** — per-command table: what it reads, which module types it works for, known limitations. Commands: `presence`, `eeprom`, `info`, `status`, `error-status`, `pm`, `lpmode`. STATE_DB key layout for below-CLI access.

3. **DOM Data Reference** — how to read optical power/bias/temperature for SFF-8636 modules after CLI fixes. Rx power thresholds: SR4 (−1 to −9 dBm), LR4 (−1 to −14.4 dBm), CWDM4 (−1 to −9.5 dBm).

4. **Link Bring-Up Procedure** — ordered checklist: presence → EEPROM valid → DOM readable → TX not disabled → FEC aligned → peer admin-up + FEC matched → link up. Exact CLI commands at each step.

5. **FEC Configuration** — table: module type → required FEC on Tomahawk, Arista EOS equivalents, verification via `show interfaces counters` FEC error rate.

6. **Troubleshooting** — three scenarios: (a) Rx LOS with fiber connected → check LP_MODE via BMC; (b) TX disabled on CWDM4 → clear via sfputil, check xcvrd logs; (c) signal present but link down → FEC mismatch diagnostic.

7. **Verified Configuration** — working state record updated incrementally as each port reaches oper-up: port, FEC mode, DOM readings, hardware-verified date. Ports still blocked are listed with documented root cause rather than a placeholder oper-up entry.

---

## Success Criteria

- `show interfaces transceiver status` (all-ports) shows correct flag data for all installed SFF-8636 modules
- `show interfaces transceiver info` and `eeprom` return consistent data for all four optical ports
- `show interfaces transceiver pm` shows DOM sensor readings for SFF-8636 modules
- At least one optical port (Ethernet116 CWDM4) reaches oper-up within 30 seconds of TX enable
- All four optical ports reach oper-up (or blockers documented with root cause); allow 30 seconds convergence after each config change before asserting oper-up
- CLI fixes committed to `src/sonic-utilities` submodule and rebuilt into `.deb`
- `notes/SONiC-wedge100s-Optics-Setup-Guide.md` written and hardware-verified
