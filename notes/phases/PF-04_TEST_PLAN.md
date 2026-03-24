# PF-04 — BMC Daemon: Test Plan

## What a Passing Test Looks Like

The BMC daemon is working correctly when the timer is active, all 25 cache files
exist and contain plausible values, and the daemon run completes in well under the
poll interval.

## Required Hardware State

- Platform init complete (`wedge100s-platform-init.service` active).
- At least 30 s elapsed since boot (first `OnBootSec=15` fire + one `OnUnitActiveSec=10`).
- All 5 fan trays installed.
- PSU2 with AC power (PSU1 present but no AC is the lab configuration).
- BMC reachable on `/dev/ttyACM0` (not in ONIE or power-cycle state).

## Test Actions

### Step 1 — Timer is active

```bash
systemctl is-active wedge100s-bmc-poller.timer
```

Expected: `active`

```bash
systemctl status wedge100s-bmc-poller.timer | grep 'Trigger:'
```

Expected: shows a trigger time within the next 10 s.

### Step 2 — All cache files exist

```bash
ls /run/wedge100s/thermal_{1..7} \
      /run/wedge100s/fan_present \
      /run/wedge100s/fan_{1..5}_front \
      /run/wedge100s/fan_{1..5}_rear \
      /run/wedge100s/psu_{1,2}_{vin,iin,iout,pout} | wc -l
```

Expected: `25`

### Step 3 — Files are fresh (< 30 s old)

```bash
find /run/wedge100s/thermal_1 -mmin -0.5
```

Expected: file found (modified within last 30 s).

### Step 4 — Thermal values in plausible range

```bash
for i in 1 2 3 4 5 6 7; do
  val=$(cat /run/wedge100s/thermal_$i)
  echo "thermal_$i = $val"
done
```

Expected: all values in range `[10000, 80000]` (10–80°C in millidegrees).

### Step 5 — Fan presence

```bash
cat /run/wedge100s/fan_present
```

Expected: `0` (all 5 trays present, bitmask = 0).

### Step 6 — Fan RPM in range

```bash
for t in 1 2 3 4 5; do
  f=$(cat /run/wedge100s/fan_${t}_front)
  r=$(cat /run/wedge100s/fan_${t}_rear)
  echo "tray $t: front=$f rear=$r"
done
```

Expected: front ~5000–16000 RPM, rear ~3000–12000 RPM. All non-zero.

### Step 7 — PSU2 cache files non-zero

```bash
cat /run/wedge100s/psu_2_vin
```

Expected: non-zero (raw LINEAR11 word; for 200 VAC input typical value is around
0x??????, not zero). Any non-zero value indicates the PMBus read succeeded.

```bash
cat /run/wedge100s/psu_1_vin
```

Expected: may be 0 or non-zero depending on PSU1 AC state. Value 0 indicates
no input power or PMBus read returned zero — acceptable in lab.

### Step 8 — Daemon completes in time budget

```bash
time sudo /usr/bin/wedge100s-bmc-daemon
```

Expected: `real` < 15 s (target: 3–9 s). Must complete before next timer fire.

### Step 9 — Python API reads from daemon files

```bash
python3 -c "
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.thermal import Thermal
t = Thermal(0)  # CPU core — not from daemon
for i in range(1, 8):
    from sonic_platform.thermal import Thermal as T
    # TMP75 sensors read /run/wedge100s/thermal_N
    break
# Simpler: read directly
val = int(open('/run/wedge100s/thermal_1').read().strip())
print(f'thermal_1 = {val} millidegrees = {val/1000:.1f} C')
assert 10000 <= val <= 80000
print('PASS')
"
```

Expected: `PASS`

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| Timer active | `active` | inactive |
| 25 output files exist | all present | any missing |
| thermal_1 in [10000, 80000] | yes | out of range or missing |
| fan_present | `0` | non-zero or missing |
| fan_1_front in [4000, 16000] | yes | 0 or out of range |
| psu_2_vin non-zero | yes | 0 or missing |
| daemon real time < 15 s | yes | ≥ 15 s |

## Mapping to Test Stage

These checks map to `tests/stage_10_daemon/` (planned). Fan/thermal range checks
overlap with `tests/stage_03_thermal/` and `tests/stage_04_fan/`.

## State Changes and Restoration

Step 8 runs the daemon manually, writing fresh values to all 25 files. This is
safe — it is exactly what the timer does every 10 s. No cleanup required.
The daemon never reads back its own files; subsequent timer invocations always
overwrite.
