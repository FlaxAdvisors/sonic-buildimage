# Wedge100S-32X Optics Setup — CLI Fixes + Link Bring-Up

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four sonic-utilities transceiver CLI bugs, bring up four 100G optical ports (Ethernet100/104/108/116), and write a platform operator reference guide.

**Architecture:** Track 1 patches Python files in-place on the target switch (`admin@192.168.88.12`) for rapid iteration, then commits diffs to the `src/sonic-utilities` submodule once validated. Track 2 is hardware investigation using CLI commands and BMC GPIO access. Track 3 is a reference guide built from findings in Tracks 1–2.

**Tech Stack:** Python 3 / Click / sonic-utilities (`scripts/sfpshow`, `show/interfaces/__init__.py`, `utilities_common/sfp_helper.py`), SONiC STATE_DB (redis db 6), sfputil, xcvrd (pmon container), OpenBMC GPIO, Arista EOS CLI (peer at 192.168.88.14 via jump host).

---

## File Map

| Action | Path |
|--------|------|
| Modify (live) | `/usr/bin/sfpshow` on target |
| Modify (live) | `/usr/lib/python3/dist-packages/show/interfaces/__init__.py` on target |
| Source (submodule) | `src/sonic-utilities/scripts/sfpshow` |
| Source (submodule) | `src/sonic-utilities/show/interfaces/__init__.py` |
| Create | `notes/SONiC-wedge100s-Optics-Setup-Guide.md` |
| Update | `tests/STAGED_PHASES.md` |

Live-patch workflow: edit the source file locally → `scp` to target → test → apply the same edit to the submodule source.

---

## Task 1: Diagnose and Fix `show interfaces transceiver status` (all-ports)

**Files:**
- Modify: `src/sonic-utilities/scripts/sfpshow` (lines 551–578, `convert_interface_sfp_status_to_cli_output_string`)
- Live target: `/usr/bin/sfpshow`

**Symptom:** `show interfaces transceiver status` (no port arg) prints "Transceiver status info not applicable" for SFF-8636 modules; per-port query works.

**Root cause hypothesis:** `TRANSCEIVER_STATUS|<subport>` is not populated by xcvrd for SFF-8636 modules. The current code only reads `TRANSCEIVER_STATUS_SW` inside an `if sfp_status_dict:` gate that fires only when `TRANSCEIVER_STATUS` is non-empty (line 562). If `TRANSCEIVER_STATUS` is empty/None, the entire merge block is skipped and `sfp_status_dict` stays None → the `len > 2` check fails.

- [ ] **Step 1: Confirm root cause via redis on target**

```bash
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_STATUS|Ethernet104'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_STATUS_SW|Ethernet104'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_STATUS|Ethernet116'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_STATUS_SW|Ethernet116'"
```

Expected: `TRANSCEIVER_STATUS` is empty (no output or `(empty array)`), `TRANSCEIVER_STATUS_SW` has 3 keys (`cmis_state`, `status`, `error_description` or similar).

If `TRANSCEIVER_STATUS` IS populated → root cause is the `len > 2` threshold with only 2 useful keys; adjust the threshold or the condition accordingly.

- [ ] **Step 2: Confirm per-port command still shows data**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet104"
ssh admin@192.168.88.12 "show interfaces transceiver status"
```

Record the exact output of both. The first should show status data; the second should show "not applicable" for SFF-8636 ports.

- [ ] **Step 3: Apply fix to `convert_interface_sfp_status_to_cli_output_string` on target**

**Branch A — `TRANSCEIVER_STATUS` is empty for SFF-8636 (confirmed by Step 1 showing empty output):**

Remove the `if sfp_status_dict:` guard so all four STATE_DB tables merge unconditionally.

In `src/sonic-utilities/scripts/sfpshow`, replace lines 559–566:

```python
# BEFORE (lines 559-566):
        sfp_status_dict = state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS|{}'.format(first_subport))
        if sfp_status_dict:
            # Additional handling to ensure that the CLI output remains the same
            # after restructuring the diagnostic data in the state DB
            sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS_SW|{}'.format(interface_name)) or {})
            sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS_FLAG|{}'.format(first_subport)) or {})
            sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_DOM_FLAG|{}'.format(first_subport)) or {})
        if sfp_status_dict and len(sfp_status_dict) > 2:

# AFTER (Branch A):
        sfp_status_dict = state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS|{}'.format(first_subport)) or {}
        sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS_SW|{}'.format(interface_name)) or {})
        sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_STATUS_FLAG|{}'.format(first_subport)) or {})
        sfp_status_dict.update(state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_DOM_FLAG|{}'.format(first_subport)) or {})
        if sfp_status_dict and len(sfp_status_dict) > 2:
```

**Branch B — `TRANSCEIVER_STATUS` IS populated but has exactly 1–2 keys (> 2 threshold fires):**

Keep the `if sfp_status_dict:` merge block as-is, but lower the threshold. Check how many keys xcvrd writes for SFF-8636 modules by counting the keys returned in Step 1. Change the threshold to match the actual minimum key count:

```python
# AFTER (Branch B — if TRANSCEIVER_STATUS has exactly N keys, set threshold to N-1):
        if sfp_status_dict and len(sfp_status_dict) > 0:  # or > 1 depending on Step 1 count
```

Apply whichever branch matches Step 1's findings. Deploy to target:

```bash
scp src/sonic-utilities/scripts/sfpshow admin@192.168.88.12:~/sfpshow.new
ssh admin@192.168.88.12 "sudo cp /usr/bin/sfpshow /usr/bin/sfpshow.bak && sudo cp ~/sfpshow.new /usr/bin/sfpshow"
```

- [ ] **Step 4: Verify fix on target**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver status"
```

Expected: Ethernet104, Ethernet108, Ethernet116 now show actual status fields (tx1disable, rx1los, status, etc.) instead of "not applicable". CMIS ports (if any) still render correctly.

Also verify per-port still works:
```bash
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
```

- [ ] **Step 5: Apply the same change to the submodule source**

Edit `src/sonic-utilities/scripts/sfpshow` lines 559–566 with the identical change from Step 3.

---

## Task 2: Diagnose and Fix `show interfaces transceiver info Ethernet100`

**Files:**
- Modify: `src/sonic-utilities/scripts/sfpshow` (function `convert_interface_sfp_info_to_cli_output_string`, lines 473–505)
- Live target: `/usr/bin/sfpshow`

**Symptom:** `show interfaces transceiver info Ethernet100` returns "SFP EEPROM Not detected". `show interfaces transceiver eeprom Ethernet100` works. Ethernet104 and Ethernet116 both work.

- [ ] **Step 1: Narrow the divergence**

```bash
ssh admin@192.168.88.12 "sfpshow info -p Ethernet100"
ssh admin@192.168.88.12 "sfpshow eeprom -p Ethernet100"
```

If `sfpshow info` works but `show interfaces transceiver info` doesn't → the bug is in the Click wrapper (`show/interfaces/__init__.py`). If `sfpshow info` also fails → the bug is in sfpshow itself.

- [ ] **Step 2: Check STATE_DB content for Ethernet100**

```bash
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_INFO|Ethernet100'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_INFO|Ethernet104'"
```

Compare the two outputs. Look for fields that differ: `type`, `encoding`, `dom_capability`, `connector`, `spec_compliance`. A field with value `N/A` or `None` may trigger the "Not detected" branch if improperly handled.

- [ ] **Step 3: Check `convert_interface_sfp_info_to_cli_output_string` for gating conditions**

Key line to inspect (line 486):
```python
if sfp_info_dict['type'] == RJ45_PORT_TYPE:
```
Confirm `RJ45_PORT_TYPE` value:
```bash
ssh admin@192.168.88.12 "python3 -c \"from utilities_common.platform_sfputil_helper import RJ45_PORT_TYPE; print(repr(RJ45_PORT_TYPE))\""
```

Check if Ethernet100's `type` field in STATE_DB matches `RJ45_PORT_TYPE` accidentally (would produce wrong branch). Also check if `sfp_info_dict` is None (would produce "Not detected" at line 500–503).

From `convert_interface_sfp_info_to_cli_output_string` line 484:
```python
sfp_info_dict = state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_INFO|{}'.format(interface_name))
if sfp_info_dict:   # ← if None/empty → falls through to "Not detected"
```

If STATE_DB has the data but the function returns "Not detected", check `get_first_subport()` return value for Ethernet100:
```bash
ssh admin@192.168.88.12 "python3 -c \"
from utilities_common import platform_sfputil_helper as h
h.load_platform_sfputil()
h.platform_sfputil_read_porttab_mappings()
print(h.get_first_subport('Ethernet100'))
print(h.get_first_subport('Ethernet104'))
\""
```

If `get_first_subport('Ethernet100')` returns None → the early-exit at line 479 fires → "Not detected". Check `portconfig.ini` / `port_config.ini` for Ethernet100 entry.

- [ ] **Step 4: Apply fix based on diagnosis**

**Fix A — `get_first_subport` returns None for Ethernet100:**

Verify `port_config.ini` entry:
```bash
grep "Ethernet100" device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini
```

The file format is: `# name    lanes    alias    index    speed`. Ethernet100 at 4×25G lanes 85,86,87,88 (stride-4 port at physical port 26) needs an `index` column value of 26. If the index column is missing or wrong, add/fix it:

```
Ethernet100    85,86,87,88    hundredGigE1/26    26    100000
```

Deploy the updated port_config.ini to target and restart the affected services:
```bash
scp device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini \
    admin@192.168.88.12:~/port_config.ini.new
ssh admin@192.168.88.12 "
  sudo cp /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini \
         /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini.bak
  sudo cp ~/port_config.ini.new \
         /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini
  sudo systemctl restart pmon
"
sleep 15
```

**Fix B — `TRANSCEIVER_INFO|Ethernet100` is not populated (xcvrd hasn't scanned it):**
```bash
ssh admin@192.168.88.12 "sudo systemctl restart pmon && sleep 15"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_INFO|Ethernet100'"
```

- [ ] **Step 5: Verify fix**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver info Ethernet100"
```

Expected: "SFP EEPROM detected" followed by vendor name, serial, part number. Must match output from `show interfaces transceiver eeprom Ethernet100`.

Apply fix to submodule source if it was a code change; document root cause in `tests/notes/` if it was a config issue.

---

## Task 3: Add DOM Sensor Display to `eeprom -d` for SFF-8636

**Files:**
- Modify: `src/sonic-utilities/scripts/sfpshow` (function `convert_dom_to_output_string`, lines 375–469)
- Live target: `/usr/bin/sfpshow`

**Goal:** Verify that `sfpshow eeprom -d` displays DOM data for SFF-8636 modules. If DOM data is in STATE_DB (`TRANSCEIVER_DOM_SENSOR`) but not displayed, fix the display path. If xcvrd doesn't populate DOM for certain modules (dom_capability=N/A), add a direct register read fallback.

- [ ] **Step 1: Check if xcvrd populated DOM sensor data**

```bash
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet100'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet104'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet108'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet116'"
```

Expected: At minimum temperature, voltage, and rx/tx power fields. For modules with dom_capability=N/A, this may be empty.

- [ ] **Step 2: Test current `eeprom -d` behavior**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet116"
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet104"
```

Record output. If DOM section renders correctly (ChannelMonitorValues with rx1power–rx4power, tx1bias–tx4bias) → no code change needed for this capability.

- [ ] **Step 3: If DOM section is missing for some modules despite STATE_DB having data**

Check `sfp_type` returned for these modules:
```bash
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGET 'TRANSCEIVER_INFO|Ethernet104' type"
```

In `convert_dom_to_output_string` (line 381), the QSFP path fires only if `sfp_type.startswith('QSFP')`. Confirm the type string starts with "QSFP". If it starts with something else (e.g., "100GBASE"), add that prefix to the condition or normalize the type before the check.

If fix needed, update `convert_dom_to_output_string` line 381:
```python
# BEFORE:
        if sfp_type.startswith('QSFP') or is_sfp_cmis:
# AFTER (if type string doesn't start with QSFP for some modules):
        if sfp_type.startswith(('QSFP', 'C100')) or is_sfp_cmis:
```
Replace `'C100'` with whatever prefix the ColorChip or other modules use.

- [ ] **Step 4: If DOM section missing — deploy fix to target and verify**

If Step 3 required a code change to `convert_dom_to_output_string` (line 381), deploy:

```bash
scp src/sonic-utilities/scripts/sfpshow admin@192.168.88.12:~/sfpshow.new
ssh admin@192.168.88.12 "sudo cp ~/sfpshow.new /usr/bin/sfpshow"
```

**Note:** If Step 1 shows empty `TRANSCEIVER_DOM_SENSOR` tables for any module (dom_capability=N/A), the DOM section will remain empty for that module. This is expected behavior — do not add a live-register fallback; that would require sudo access embedded in library code and is out of spec scope.

- [ ] **Step 5: Verify on target**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet100"
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet104"
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet108"
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet116"
```

Expected: Each output shows DOM section with non-zero values for at least rx power or temperature. Ports with true hardware LOS (Ethernet100) may show -inf for rx power — that is correct, not a bug. Ports with dom_capability=N/A may show no DOM section — that is also expected.

Apply any code changes to submodule source.

---

## Task 4: Add SFF-8636 `pm` Rendering Path

**Files:**
- Modify: `src/sonic-utilities/scripts/sfpshow` (function `convert_interface_sfp_pm_to_cli_output_string`, lines 602–654)
- Live target: `/usr/bin/sfpshow`

**Goal:** `show interfaces transceiver pm Ethernet104` currently shows "Transceiver performance monitoring not applicable". For SFF-8636 modules (not CMIS), fall back to `TRANSCEIVER_DOM_SENSOR` and render a per-lane table.

- [ ] **Step 1: Confirm current behavior and STATE_DB availability**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet116"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_PM|Ethernet116'"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet116'"
```

Expected: `pm` shows "not applicable"; `TRANSCEIVER_PM` is empty; `TRANSCEIVER_DOM_SENSOR` has rx/tx power, bias, temperature, voltage.

- [ ] **Step 2: Add SFF-8636 rendering branch to `convert_interface_sfp_pm_to_cli_output_string`**

The current code (line 618): `if sfp_pm_dict:` → renders ZR/CMIS table. `else:` → "not applicable".

Add an else-if branch: if sfp_pm_dict is empty but module is SFF-8636, read DOM sensor and render lane table.

```python
    def convert_interface_sfp_pm_to_cli_output_string(self, state_db, interface_name):
        first_subport = platform_sfputil_helper.get_first_subport(interface_name)
        if first_subport is None:
            click.echo("Error: Unable to get first subport for {} while converting SFP PM".format(interface_name))
            output = ZR_PM_NOT_APPLICABLE_STR + '\n'
            return output

        sfp_pm_dict = state_db.get_all(
            self.db.STATE_DB, 'TRANSCEIVER_PM|{}'.format(first_subport))
        sfp_threshold_dict = state_db.get_all(
            state_db.STATE_DB, 'TRANSCEIVER_DOM_THRESHOLD|{}'.format(first_subport))
        # Convert VDM THRESHOLD fields to legacy DOM THRESHOLD fields
        self.convert_vdm_fields_to_legacy_fields(state_db, first_subport, sfp_threshold_dict, CCMIS_VDM_THRESHOLD_TO_LEGACY_DOM_THRESHOLD_MAP, 'THRESHOLD')
        table = []
        indent_num = 4
        indent = ' ' * indent_num
        if sfp_pm_dict:
            # ... existing ZR/CMIS rendering (unchanged) ...
            output = '\n' + indent
            for param_name, (unit, prefix) in ZR_PM_INFO_MAP.items():
                row = [param_name, unit]
                values = []
                for suffix in ZR_PM_VALUE_KEY_SUFFIXS:
                    key = prefix + '_' + suffix
                    values.append(
                        float(sfp_pm_dict[key]) if key in sfp_pm_dict else None)

                thresholds = []
                for suffix in ZR_PM_THRESHOLD_KEY_SUFFIXS:
                    key = self.convert_pm_prefix_to_threshold_prefix(
                        prefix) + suffix
                    if key in sfp_threshold_dict and sfp_threshold_dict[key] != 'N/A':
                        thresholds.append(float(sfp_threshold_dict[key]))
                    else:
                        thresholds.append(None)

                tca_high, tca_low = None, None
                if values[2] is not None and thresholds[0] is not None:
                    tca_high = values[2] > thresholds[0]
                if values[0] is not None and thresholds[2] is not None:
                    tca_low = values[0] < thresholds[2]

                for field in values + thresholds[:2] + [tca_high] + thresholds[2:] + [tca_low]:
                    row.append(self.beautify_pm_field(prefix, field))
                table.append(row)

            output += tabulate(table,
                               ZR_PM_HEADER, disable_numparse=True).replace('\n', '\n' + indent)
            output += '\n'
        else:
            # SFF-8636 fallback: render TRANSCEIVER_DOM_SENSOR as per-lane table
            sfp_info_dict = state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_INFO|{}'.format(interface_name)) or {}
            is_cmis = is_transceiver_cmis(sfp_info_dict)
            dom_sensor_dict = state_db.get_all(state_db.STATE_DB, 'TRANSCEIVER_DOM_SENSOR|{}'.format(first_subport)) or {}
            if not is_cmis and dom_sensor_dict:
                SFF8636_PM_HEADER = ['Lane', 'Rx Power (dBm)', 'Tx Bias (mA)', 'Tx Power (dBm)']
                lane_rows = []
                for lane in range(1, 5):
                    rx = dom_sensor_dict.get('rx{}power'.format(lane), 'N/A')
                    bias = dom_sensor_dict.get('tx{}bias'.format(lane), 'N/A')
                    txpwr = dom_sensor_dict.get('tx{}power'.format(lane), 'N/A')
                    lane_rows.append([str(lane), str(rx), str(bias), str(txpwr)])
                output = '\n' + indent
                output += tabulate(lane_rows, SFF8636_PM_HEADER,
                                   disable_numparse=True).replace('\n', '\n' + indent)
                # Module-level sensors (not per-lane)
                temp = dom_sensor_dict.get('temperature', 'N/A')
                voltage = dom_sensor_dict.get('voltage', 'N/A')
                output += '\n{}Temperature: {} C\n{}Voltage: {} Volts\n'.format(
                    indent, temp, indent, voltage)
            else:
                output = ZR_PM_NOT_APPLICABLE_STR + '\n'
        return output
```

Deploy the updated `/usr/bin/sfpshow` to target:
```bash
scp src/sonic-utilities/scripts/sfpshow admin@192.168.88.12:~/sfpshow.new
ssh admin@192.168.88.12 "sudo cp ~/sfpshow.new /usr/bin/sfpshow"
```

- [ ] **Step 3: Verify on target**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet116"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet104"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet108"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet100"
```

Expected: Each port shows a 4-lane table with Rx Power / Tx Bias / Tx Power columns, plus module-level Temperature and Voltage. Ports with LOS show `-inf` for Rx power — that is correct. If `dom_sensor_dict` is empty for any port, the command should still produce readable output rather than crashing.

- [ ] **Step 4: Apply changes to submodule source**

Apply the same edits to `src/sonic-utilities/scripts/sfpshow`.

---

## Task 5: Ethernet116 TX Disable — Investigation and Resolution

**Priority: First.** Peer is sending; TX disable is the only blocker.

**Files:** No code changes expected. Hardware state changes only.

- [ ] **Step 1: Check xcvrd logs for TX disable events**

```bash
ssh admin@192.168.88.12 "sudo docker logs pmon 2>&1 | grep -i 'tx.*disable\|disable.*tx\|Ethernet116' | tail -30"
```

If xcvrd explicitly set TX disable with a reason → note the reason; that determines whether pmon restart will clear it.

- [ ] **Step 2: Check current TX disable state via STATE_DB and CLI**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
ssh admin@192.168.88.12 "sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_STATUS|Ethernet116'"
```

Confirm `tx1disable` through `tx4disable` are all `True`. Record exact STATE_DB values.

- [ ] **Step 3: Attempt pmon restart**

```bash
ssh admin@192.168.88.12 "sudo systemctl restart pmon"
sleep 20
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
ssh admin@192.168.88.12 "show interfaces status Ethernet116"
```

If TX disable clears and link comes up within 30s → log root cause (xcvrd init race or failure during first init) and proceed to Task 6.

If TX disable does not clear after pmon restart → proceed to Step 4.

- [ ] **Step 4: BMC module RESET via GPIO**

First check BMC reachability:
```bash
ping -c1 -W2 192.168.88.13 && ssh -o ConnectTimeout=5 root@192.168.88.13 echo ok
```
If SSH fails → stop and prompt user for `ssh-copy-id root@192.168.88.13` (password: `0penBmc`).

On BMC, find the RESET GPIO for port 28 (Ethernet116):
```bash
ssh root@192.168.88.13 "gpiodetect"
ssh root@192.168.88.13 "gpioinfo | grep -i 'qsfp\|reset\|28'"
```

Then assert and release RESET:
```bash
ssh root@192.168.88.13 "
CHIP=\$(gpiodetect | grep -i qsfp | head -1 | awk '{print \$1}')
LINE=\$(gpioinfo \$CHIP | awk '/reset.*28|28.*reset/{print NR-1}')
echo \"Using chip \$CHIP, line \$LINE\"
gpioset \$CHIP \$LINE=0
sleep 1
gpioset \$CHIP \$LINE=1
"
```

Wait 5 seconds then check:
```bash
sleep 5
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
```

If TX disable is now False → link should come up within 30s. Check:
```bash
ssh admin@192.168.88.12 "show interfaces status Ethernet116"
```

If TX disable still True after BMC reset → proceed to Step 5.

- [ ] **Step 5: Direct register write (last resort)**

Only if Steps 3 and 4 both failed. Write TX disable register to 0x00 (all lanes enabled):
```bash
ssh admin@192.168.88.12 "sudo sfputil write-eeprom -p Ethernet116 -n 0 -o 86 -d 00"
```

Page 0, offset 86 (0x56), value 0x00 = all four TX lanes enabled. The TX disable register is volatile RAM in the module MCU — it does not affect vendor calibration data.

Check immediately:
```bash
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
sleep 30
ssh admin@192.168.88.12 "show interfaces status Ethernet116"
```

- [ ] **Step 6: Observe and record link-up state**

Once TX disable clears, allow up to 30 seconds for RS-FEC training and link establishment. Run:
```bash
ssh admin@192.168.88.12 "show interfaces status Ethernet116"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet116"
ssh admin@192.168.88.12 "show interfaces counters Ethernet116"
```

Record oper state, DOM readings (Rx power should be ~+0.8 dBm from earlier measurement), and counter activity.

Write findings to `tests/notes/ethernet116-cwdm4-linkup.md`.

---

## Task 6: Ethernet104 (LR4) and Ethernet108 (SR4) — FEC and Peer Investigation

**Files:** No code changes expected. May result in `config interface fec` CLI commands.

- [ ] **Step 1: Check Arista peer FEC configuration**

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et27/1 transceiver detail; show interfaces Et28/1 transceiver detail'
```

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et27/1; show interfaces Et28/1'
```

Check: Are Et27/1 and Et28/1 admin-up? What FEC mode is configured on Arista? Arista LR4 often uses `fec none` in some deployments.

- [ ] **Step 2: Check current SONiC FEC configuration**

```bash
ssh admin@192.168.88.12 "show interfaces status Ethernet104 Ethernet108"
ssh admin@192.168.88.12 "redis-cli -n 4 HGET 'PORT_TABLE:Ethernet104' fec"
ssh admin@192.168.88.12 "redis-cli -n 4 HGET 'PORT_TABLE:Ethernet108' fec"
```

Current config: `rs` (RS-FEC) on both ports.

- [ ] **Step 3: Align SONiC FEC with peer if mismatch found**

If Arista Et27/1 (Ethernet104 peer) uses FEC=none:
```bash
ssh admin@192.168.88.12 "sudo config interface fec Ethernet104 none"
sleep 30
ssh admin@192.168.88.12 "show interfaces status Ethernet104"
```

If Arista Et28/1 (Ethernet108 peer) uses FEC=none:
```bash
ssh admin@192.168.88.12 "sudo config interface fec Ethernet108 none"
sleep 30
ssh admin@192.168.88.12 "show interfaces status Ethernet108"
```

If peer uses RS-FEC and no change was made → proceed to Step 4.

- [ ] **Step 4: Verify admin-up state and DOM readings after any config change**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet104"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet104"
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet108"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet108"
```

Look for TX bias > 0 mA (confirms laser is on) and Rx power above -40 dBm (signal present). If Rx power is -inf despite signal-present LOS=False finding, the DOM capability for these modules may report stale data.

- [ ] **Step 5: If link remains down after FEC alignment**

Check FEC error counters (indicates FEC is running but not locking):
```bash
ssh admin@192.168.88.12 "show interfaces counters -r Ethernet104 Ethernet108"
```

If FEC correctable errors are rapidly incrementing → RS-FEC is active but frame sync is failing; likely a lane polarity or speed mismatch. Check Arista interface speed:
```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et27/1 | grep -i "bandwidth\|speed\|fec"'
```

Write findings to `tests/notes/ethernet104-108-linkup.md`.

---

## Task 7: Ethernet100 (SR4) — Rx LOS Investigation

**Priority: Last.** No received signal at all; possible physical/peer-side issue.

- [ ] **Step 1: Check Arista Et26/1 TX status**

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et26/1 transceiver detail; show interfaces Et26/1'
```

Confirm: Is Et26/1 admin-up? Is Arista TX power > -40 dBm? Is Arista reporting Rx LOS on our signal too?

- [ ] **Step 2: MPO cable orientation check (software-side)**

Check LLDP from Arista toward Ethernet100:
```bash
ssh admin@192.168.88.12 "show lldp neighbor Ethernet100"
```

If LLDP discovery works but link is down → FEC issue, not cable polarity.
If no LLDP → no physical Rx signal. MPO-12 Type B is required for QSFP28-SR4 to QSFP28-SR4. A Type A cable reverses lane order → all-lane Rx LOS. This requires physical inspection; flag to user.

- [ ] **Step 3: Check LP_MODE via BMC if physical connection confirmed good**

```bash
ssh root@192.168.88.13 "gpioinfo | grep -i 'lp\|lpmode\|26'"
```

Identify the LP_MODE GPIO line for port 26 (Ethernet100). If LP_MODE is asserted (active), the module is in low-power mode and may suppress TX. Assert LP_MODE=0 (deassert):
```bash
ssh root@192.168.88.13 "gpioset <chip> <line>=0"
sleep 5
ssh admin@192.168.88.12 "show interfaces status Ethernet100"
```

- [ ] **Step 4: Record findings**

Even if Ethernet100 cannot be brought up (physical blocker), document the exact failure signature and root cause in `tests/notes/ethernet100-sr4-linkup.md`.

---

## Task 8: Finalize `src/sonic-utilities` Submodule Changes

**Files:**
- `src/sonic-utilities/scripts/sfpshow` (already updated in Tasks 1–4)
- `src/sonic-utilities/show/interfaces/__init__.py` (if modified in Task 2)

**Note:** Do not run `git commit`; user manages source control. Provide the staged diff for user to review.

sonic-utilities is built as a Python wheel (`sonic_utilities-1.2-py3-none-any.whl`), separate from the platform `.deb`. The live-patched files on target ARE the deployed fix. The submodule changes will be incorporated into the next full SONiC image build (`make target/sonic-broadcom.bin`). No standalone sonic-utilities rebuild is needed here.

- [ ] **Step 1: Verify submodule files match the validated target versions**

```bash
diff <(ssh admin@192.168.88.12 cat /usr/bin/sfpshow) src/sonic-utilities/scripts/sfpshow
```

Expected: no diff. If diff is non-empty, the target has a change not yet reflected in the submodule — apply the missing change to the submodule source before proceeding.

If `show/interfaces/__init__.py` was modified in Task 2:
```bash
diff <(ssh admin@192.168.88.12 cat /usr/lib/python3/dist-packages/show/interfaces/__init__.py) \
     src/sonic-utilities/show/interfaces/__init__.py
```

- [ ] **Step 2: Show git diff for user review**

```bash
(cd src/sonic-utilities && git diff HEAD)
```

Confirm only the intended lines are changed. Expected output: changes to `scripts/sfpshow` for Fix 1 (status merge), Fix 4 (pm SFF-8636 path), and any other validated fixes from Tasks 1–4.

- [ ] **Step 3: Final end-to-end verification on target**

```bash
ssh admin@192.168.88.12 "show interfaces transceiver status"
ssh admin@192.168.88.12 "show interfaces transceiver status Ethernet116"
ssh admin@192.168.88.12 "show interfaces transceiver info Ethernet100"
ssh admin@192.168.88.12 "show interfaces transceiver eeprom --dom Ethernet116"
ssh admin@192.168.88.12 "show interfaces transceiver pm Ethernet116"
```

All five commands must produce correct output without "not applicable" or "Not detected" errors for the optical ports. Record passing outputs in `tests/notes/`.

---

## Task 9: Write Platform Operator Reference Guide

**Files:**
- Create: `notes/SONiC-wedge100s-Optics-Setup-Guide.md`
- Update: `tests/STAGED_PHASES.md` (add Phase 21)

- [ ] **Step 1: Write the guide with hardware-verified data from Tasks 1–7**

Create `notes/SONiC-wedge100s-Optics-Setup-Guide.md` with these sections:

1. **Hardware Overview** — port table (Ethernet100/104/108/116), fiber types (MPO-12 / LC duplex), peer mapping, LP_MODE/RESET accessibility note (driven by PCA9505 via BMC, not host CPU).

2. **Transceiver CLI Command Reference** — table: command, what it reads, which module types work, known limitations:
   | Command | Source | Works for | Notes |
   |---------|--------|-----------|-------|
   | `presence` | STATE_DB TRANSCEIVER_INFO | all | |
   | `eeprom` | STATE_DB TRANSCEIVER_INFO | all | |
   | `eeprom --dom` | STATE_DB TRANSCEIVER_DOM_SENSOR | SFF-8636 (after fix) | dom_capability=N/A modules may show zeros |
   | `info` | STATE_DB TRANSCEIVER_INFO | all (after fix) | Ethernet100 was broken pre-fix |
   | `status` | STATE_DB TRANSCEIVER_STATUS + SW | all (after fix) | was broken for SFF-8636 pre-fix |
   | `pm` | STATE_DB TRANSCEIVER_DOM_SENSOR | SFF-8636 (new path) | ZR/CMIS path unchanged |
   | `lpmode` | sfputil hardware read | all | requires sudo |
   | `error-status` | sfputil hardware read | all | requires sudo |

3. **DOM Data Reference** — how to read optical power/bias/temperature post-fix. Rx power thresholds: SR4 (−1 to −9 dBm), LR4 (−1 to −14.4 dBm), CWDM4 (−1 to −9.5 dBm).

4. **Link Bring-Up Procedure** — ordered checklist:
   - Verify presence: `show interfaces transceiver presence Ethernet116`
   - Verify EEPROM readable: `show interfaces transceiver eeprom Ethernet116`
   - Check TX disable state: `show interfaces transceiver status Ethernet116`
   - If TX disabled: restart pmon → BMC reset → sfputil write-eeprom (in order)
   - Check FEC alignment with peer
   - Verify peer admin-up and matching FEC
   - Allow 30s for convergence; check oper state

5. **FEC Configuration Table:**
   | Port | Type | SONiC FEC | Arista FEC | Verified |
   |------|------|-----------|------------|---------|
   | Ethernet100 | QSFP28-SR4 | rs | rs | TBD |
   | Ethernet104 | QSFP28-LR4 | rs (or none) | TBD from Task 6 | TBD |
   | Ethernet108 | QSFP28-SR4 | rs | TBD from Task 6 | TBD |
   | Ethernet116 | CWDM4 | rs | rs | TBD |

6. **Troubleshooting** — three scenarios:
   - Rx LOS with fiber connected → check LP_MODE via BMC
   - TX disabled on CWDM4 → `sudo docker logs pmon | grep -i tx.*disable`, then pmon restart / BMC reset / sfputil write-eeprom
   - Signal present but link down → FEC mismatch: `config interface fec EthernetX none` to test, `show interfaces counters -r EthernetX` for FEC error rate

7. **Verified Configuration** — updated as each port reaches oper-up.

- [ ] **Step 2: Update `tests/STAGED_PHASES.md`**

Add a Phase 21 section recording the CLI fixes and link-up state for all four optical ports with hardware-verified date and per-port status.

- [ ] **Step 3: Review the guide for accuracy**

Cross-check all CLI commands against what was actually run and verified in Tasks 1–7. Mark each hardware-verified item with `(verified on hardware YYYY-MM-DD)`. For ports not yet oper-up, document the last known failure state and root cause rather than leaving them as placeholders.
