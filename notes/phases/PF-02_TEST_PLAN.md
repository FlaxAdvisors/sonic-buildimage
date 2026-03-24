# PF-02 — CPLD Driver: Test Plan

## What a Passing Test Looks Like

The CPLD driver is working correctly when:
1. The `wedge100s_cpld` kernel module is loaded.
2. Sysfs attributes appear at `/sys/bus/i2c/devices/1-0032/`.
3. `cpld_version` returns the expected `2.6`.
4. PSU presence and pgood attributes reflect the physical hardware state.
5. LED attributes can be read and written without error.

## Required Hardware State

- Platform init complete (`wedge100s-platform-init.service` has run).
- At least PSU2 installed with AC power (so `psu2_present=1`, `psu2_pgood=1`
  gives a known reference state).
- Lab configuration: PSU1 present but no AC (`psu1_present=1`, `psu1_pgood=0`).

## Test Actions

### Step 1 — Module loaded

```bash
lsmod | grep wedge100s_cpld
```

Expected: one line matching `wedge100s_cpld`.

### Step 2 — Driver bound to CPLD

```bash
ls /sys/bus/i2c/devices/1-0032/
```

Expected: directory contains: `cpld_version`, `psu1_present`, `psu1_pgood`,
`psu2_present`, `psu2_pgood`, `led_sys1`, `led_sys2`.

### Step 3 — CPLD version

```bash
cat /sys/bus/i2c/devices/1-0032/cpld_version
```

Expected: `2.6`

### Step 4 — PSU presence and pgood

```bash
cat /sys/bus/i2c/devices/1-0032/psu1_present
cat /sys/bus/i2c/devices/1-0032/psu1_pgood
cat /sys/bus/i2c/devices/1-0032/psu2_present
cat /sys/bus/i2c/devices/1-0032/psu2_pgood
```

Expected (lab state):
- `psu1_present` = `1` (PSU1 installed)
- `psu1_pgood` = `0` (PSU1 no AC)
- `psu2_present` = `1` (PSU2 installed)
- `psu2_pgood` = `1` (PSU2 operational)

### Step 5 — LED read

```bash
cat /sys/bus/i2c/devices/1-0032/led_sys1
cat /sys/bus/i2c/devices/1-0032/led_sys2
```

Expected: `0x02` (green — set by platform-init or ledd).

### Step 6 — LED write (non-destructive cycle)

```bash
ORIG=$(cat /sys/bus/i2c/devices/1-0032/led_sys2)
echo 0 > /sys/bus/i2c/devices/1-0032/led_sys2   # set off
cat /sys/bus/i2c/devices/1-0032/led_sys2          # verify: 0x00
echo $ORIG > /sys/bus/i2c/devices/1-0032/led_sys2 # restore
cat /sys/bus/i2c/devices/1-0032/led_sys2           # verify: restored
```

Expected: `0x00` after off write; original value after restore.

### Step 7 — PSU sysfs accessible from inside pmon

```bash
docker exec pmon cat /sys/bus/i2c/devices/1-0032/psu2_pgood
```

Expected: `1` (same as host — `/sys` is bind-mounted into pmon).

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| `wedge100s_cpld` in lsmod | yes | no |
| `1-0032/` directory exists | yes | no |
| `cpld_version` | `2.6` | any other value |
| `psu2_present` | `1` | `0` or error |
| `psu2_pgood` | `1` | `0` or error |
| `led_sys2` readable | `0x00`–`0xff` | error |
| `led_sys2` writable | write accepted, readback matches | I/O error or readback wrong |
| sysfs from pmon | same value as host | error or mismatch |

## Mapping to Test Stage

These checks map to `tests/stage_09_cpld/` (planned). Some PSU state checks
overlap with `tests/stage_05_psu/`.

## State Changes and Restoration

Step 6 writes to `led_sys2` (turns it off momentarily). It is restored in the
same step. The LED will be briefly off on the physical hardware during the test
(< 1 second). This is acceptable in a lab environment. In production, skip
Step 6 or add an assertion that it is currently not a critical-alarm LED state.
