# PW-01 — Chassis LED: Test Plan

## Overview

Verify that both front-panel CPLD LEDs behave correctly under SONiC API control,
that the `/run/wedge100s` mirror files are maintained, and that the utility no
longer uses `i2cset`.

## Required Hardware State

- SONiC running on Wedge 100S-32X (192.168.88.12)
- `wedge100s_cpld` kernel module loaded (`/sys/bus/i2c/devices/1-0032/led_sys1` present)
- pmon running (so ledd owns SYS2 and has populated `/run/wedge100s/led_sys2`)

## Dependencies

- Phase R26 (wedge100s_cpld driver) must be complete
- No dependency on other PW phases

---

## Test Actions

### T1: SYS1 write/read round-trip via chassis API

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.chassis import Chassis
c = Chassis()
for color in ('green', 'red', 'off'):
    assert c.set_status_led(color), f"set_status_led({color!r}) returned False"
    got = c.get_status_led()
    print(f"set {color!r} -> got {got!r}")
    assert got == color, f"mismatch: expected {color!r}, got {got!r}"
print("PASS")
EOF
```

**Pass:** All three assertions pass, output ends with `PASS`.

### T2: /run mirror is written by set_status_led

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.chassis import Chassis
c = Chassis()
c.set_status_led('green')
with open('/run/wedge100s/led_sys1') as f:
    raw = f.read().strip()
val = int(raw, 0)
print(f"run file: {raw!r} -> {val}")
assert val == 2, f"expected 2 (green), got {val}"
c.set_status_led('red')
val2 = int(open('/run/wedge100s/led_sys1').read().strip(), 0)
assert val2 == 1, f"expected 1 (red), got {val2}"
c.set_status_led('green')  # restore
print("PASS")
EOF
```

**Pass:** Both assertions pass.

### T3: /run mirror populated by ledd (SYS2)

```bash
ssh admin@192.168.88.12 bash -c '
  echo "led_sys2 run file:"
  cat /run/wedge100s/led_sys2
  echo "led_sys2 sysfs:"
  cat /sys/bus/i2c/devices/1-0032/led_sys2
'
```

**Pass:** Both values present and equal (either `0` or `2`).
**Fail:** `/run/wedge100s/led_sys2` does not exist (ledd not writing /run mirror).

### T4: SYS1 raw sysfs write, API read

```bash
ssh admin@192.168.88.12 bash -c '
  echo 2 | sudo tee /sys/bus/i2c/devices/1-0032/led_sys1 > /dev/null
  python3 -c "
from sonic_platform.chassis import Chassis
c = Chassis()
color = c.get_status_led()
print(\"got:\", color)
assert color == \"green\", f\"expected green got {color!r}\"
print(\"PASS\")
"'
```

**Pass:** Output is `got: green` then `PASS`.

### T5: Blue encoding round-trip

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.chassis import Chassis
c = Chassis()
assert c.set_status_led('blue'), "set_status_led('blue') returned False"
got = c.get_status_led()
print(f"blue -> {got!r}")
assert got == 'blue', f"expected 'blue', got {got!r}"
c.set_status_led('green')  # restore
print("PASS")
EOF
```

**Pass:** `blue` round-trips correctly.
**Expected failure before PW-01:** returns `'off'`.

### T6: Utility set led uses /run, not i2cset

```bash
# Verify no i2cset call in the relevant code path
grep -n 'i2cset' \
  platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py
# Expected: only the fan-speed helper line (not the LED set block)
# After PW-01: zero matches in the led/fan-LED section

ssh admin@192.168.88.12 sudo python3 \
  /usr/lib/python3/dist-packages/accton_wedge100s_util.py set led 2
ssh admin@192.168.88.12 bash -c '
  echo "led_sys1 run:"; cat /run/wedge100s/led_sys1
  echo "led_sys2 run:"; cat /run/wedge100s/led_sys2
'
```

**Pass:** `/run/wedge100s/led_sys1` and `led_sys2` both contain `2`.

### T7: SYS2 driven by ledd — port state

```bash
ssh admin@192.168.88.12 cat /sys/bus/i2c/devices/1-0032/led_sys2
# Expected: 0x02 if any port is up (DAC ports Ethernet16/32/48/112 are normally up)
```

**Pass:** `0x02` when at least one port is up.
**Fail:** `0x00` when ports are up (ledd not running or led_control.py not loading).

### T8: Healthd path simulation (SYS1 red/green)

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.chassis import Chassis
c = Chassis()
c.set_status_led('red')
assert c.get_status_led() == 'red', "red set failed"
c.set_status_led('green')
assert c.get_status_led() == 'green', "restore to green failed"
print("PASS")
EOF
```

---

## Pass/Fail Criteria Summary

| Test | Pass condition |
|---|---|
| T1 | green/red/off all round-trip correctly via chassis API |
| T2 | `/run/wedge100s/led_sys1` updated by `set_status_led()` |
| T3 | `/run/wedge100s/led_sys2` exists and matches sysfs after ledd starts |
| T4 | Raw sysfs `0x02` decoded as `green` |
| T5 | `blue` color round-trips (not decoded as `off`) |
| T6 | Utility writes `/run` files without calling `i2cset` |
| T7 | SYS2 is `0x02` when any port is up |
| T8 | `red` and `green` set/get without error |
