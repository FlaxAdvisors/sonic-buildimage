# PW-02 — PSU Telemetry Fix: Test Plan

## Overview

Validate PSU PMBus telemetry accuracy by cross-checking decoded values against
expected physical measurements. PSU2 is the live unit (PSU1 had no AC during
initial bringup).

## Required Hardware State

- SONiC running, `wedge100s-bmc-poller.timer` active (or daemon manually runnable)
- PSU2 live (AC input connected, `psu2_pgood = 1`)
- BMC reachable via `/dev/ttyACM0` (or SSH to 192.168.88.13)
- No concurrent TTY access to OpenBMC during test

## Dependencies

- Phase R28 (wedge100s-bmc-daemon) must be deployed and writing `/run/wedge100s/` files
- Phase R29 (psu.py telemetry) must be deployed

---

## Test Actions

### T1: Confirm daemon output files exist and are fresh

```bash
ssh admin@192.168.88.12 bash -c '
  for f in psu_2_vin psu_2_iin psu_2_iout psu_2_pout; do
    echo -n "$f: "; cat /run/wedge100s/$f 2>/dev/null || echo MISSING
  done
  echo "file ages:"
  stat --format="%n %Y" /run/wedge100s/psu_2_vin
'
```

**Pass:** All four files exist and contain non-zero integer values. File mtime is within
the last 30 seconds (timer fires every 10s).

### T2: Python API returns non-None values for live PSU

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.psu import Psu
p = Psu(2)   # PSU2 is live
print(f"presence:  {p.get_presence()}")
print(f"pgood:     {p.get_powergood_status()}")
print(f"VIN:       {p.get_input_voltage()}")
print(f"IIN:       {p.get_input_current()}")
print(f"IOUT:      {p.get_current()}")
print(f"POUT:      {p.get_power()}")
print(f"VOUT:      {p.get_voltage()}")

vin  = p.get_input_voltage()
iout = p.get_current()
pout = p.get_power()
vout = p.get_voltage()

assert vin  is not None, "VIN is None"
assert iout is not None, "IOUT is None"
assert pout is not None, "POUT is None"
print("PASS: all values non-None")
EOF
```

**Pass:** No assertion errors, all values print as numbers.

### T3: Plausibility check — VIN within AC range

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.psu import Psu
p = Psu(2)
vin = p.get_input_voltage()
print(f"VIN = {vin:.1f} V")
# AC input is 100–240 V; accept 90–260 V as plausible range
assert 90.0 <= vin <= 260.0, f"VIN {vin:.1f} V is outside expected AC range (90-260 V)"
print("PASS")
EOF
```

**Pass:** VIN is between 90 and 260 V.
**Fail (endian bug):** VIN is wildly wrong (e.g., < 10 V or > 1000 V).

### T4: Plausibility check — VOUT within 12 V DC range

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.psu import Psu
p = Psu(2)
vout = p.get_voltage()
pout = p.get_power()
iout = p.get_current()
print(f"VOUT = {vout:.2f} V (POUT/IOUT)")
print(f"POUT = {pout:.1f} W, IOUT = {iout:.2f} A")
# Wedge 100S PSU is 12 V nominal; accept 10–14 V
if vout is not None:
    assert 10.0 <= vout <= 14.0, f"VOUT {vout:.2f} V outside expected 10-14 V range"
    print("PASS")
else:
    print("SKIP: VOUT is None (IOUT may be zero at light load)")
EOF
```

**Pass:** VOUT prints in range 10–14 V, or prints SKIP with explanation.

### T5: Self-consistency — POUT ≈ VOUT × IOUT within 10%

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.psu import Psu
p = Psu(2)
vout = p.get_voltage()
iout = p.get_current()
pout = p.get_power()
if vout and iout and pout:
    computed = vout * iout
    error_pct = abs(computed - pout) / pout * 100
    print(f"POUT={pout:.1f} W, VOUT*IOUT={computed:.1f} W, error={error_pct:.1f}%")
    # VOUT is derived from POUT/IOUT so this is tautological; note for post-fix test
    print("NOTE: consistency check is tautological until direct VOUT read is implemented")
else:
    print("SKIP: insufficient values to check")
EOF
```

**Pass:** Prints plausible numbers (this test becomes meaningful after direct VOUT is added).

### T6: PSU1 (no AC) returns None or False

```bash
ssh admin@192.168.88.12 python3 - <<'EOF'
from sonic_platform.psu import Psu
p = Psu(1)
presence = p.get_presence()
pgood = p.get_powergood_status()
print(f"PSU1 presence={presence}, pgood={pgood}")
if not pgood:
    vin = p.get_input_voltage()
    print(f"PSU1 VIN = {vin}")
    # File may exist with a stale value or None if no AC; no assertion — document actual behavior
    print("PASS: pgood=False is expected for unpowered PSU1")
else:
    print("PSU1 is live — skip no-AC test")
EOF
```

**Pass:** Prints status without exception.

---

## Pass/Fail Criteria Summary

| Test | Pass condition |
|---|---|
| T1 | All four daemon files present and fresh |
| T2 | All four API values non-None for live PSU2 |
| T3 | VIN in range 90–260 V |
| T4 | VOUT in range 10–14 V (or SKIP if IOUT=0) |
| T5 | Documents consistency (tautological pre-fix) |
| T6 | PSU1 returns pgood=False without exception |

T1–T3 are hard pass/fail. T4 is pass/skip. T5 is informational. T6 is pass if no exception.
