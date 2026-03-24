# PS-03 PLAN — PSU Subsystem

## Problem Statement

SONiC's `psud` daemon (inside pmon) requires a `Psu` class implementing
`PsuBase` for each power supply. The Wedge 100S-32X has two AC PSUs.
`psud` publishes PSU state to Redis for `show platform psupply` and healthd.

PSU hardware access is split across two distinct sources:
1. **Presence and power-good:** CPLD register 0x10 at i2c-1/0x32 — directly
   accessible from the host via the `wedge100s_cpld` kernel module.
2. **PMBus telemetry (voltage/current/power):** PMBus registers on the PSU
   itself, read by OpenBMC over an internal I2C bus not accessible from the host.

## Proposed Approach

**Presence and pgood:** Read via `wedge100s_cpld` driver sysfs attributes:
- `/sys/bus/i2c/devices/1-0032/psu1_present` — 1 = present
- `/sys/bus/i2c/devices/1-0032/psu1_pgood` — 1 = power good
- Same for psu2.

The driver inverts the active-low bit from CPLD register 0x10 so the sysfs
value is already logic-level (1 = present/good).

**PMBus telemetry:** The `wedge100s-bmc-daemon` reads PMBus registers from
the PSU over the BMC's internal I2C bus and writes raw LINEAR11-encoded 16-bit
words to:
- `/run/wedge100s/psu_N_{vin,iin,iout,pout}`

The `Psu` class decodes these with a `_pmbus_decode_linear11()` function and
caches results for 30 seconds.

**DC output voltage (VOUT)** is computed as `POUT / IOUT` rather than reading
a dedicated VOUT register. This avoids implementing LINEAR16 VOUT_MODE parsing,
matching the approach in ONL's `psui.c`.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py` | Psu class |

`chassis.py` must instantiate `Psu(i)` for `i` in `range(1, NUM_PSUS + 1)`.

## Acceptance Criteria

- `Chassis().get_all_psus()` returns exactly 2 `Psu` objects
- `get_presence()` returns correct value (True when PSU is plugged in)
- `get_powergood_status()` returns True for a powered PSU
- `get_status()` returns True only when present AND pgood
- `get_type()` returns `'AC'`
- `get_capacity()` returns `650.0`
- `get_voltage()` returns a float in 11–13 V range (DC output) for powered PSU
- `get_input_voltage()` returns a float in 200–250 V range (AC input)

## Risks and Watchouts

- **PSU1 absent in lab:** Hardware notes show PSU1 had no AC power during
  initial development. Tests must handle one absent/unpowered PSU gracefully.
- **LINEAR11 decode correctness:** The 5-bit exponent and 11-bit mantissa are
  both signed (two's complement). An off-by-one in the sign extension produces
  wildly wrong values. Validate against known BMC register dumps.
- **VOUT via POUT/IOUT:** If IOUT is zero (PSU in standby or no load),
  `get_voltage()` returns `None`. This is correct behavior but can confuse
  monitoring tools that expect a voltage reading from a "present" PSU.
- **CPLD module required:** `psu{N}_present` and `psu{N}_pgood` sysfs attrs
  require `wedge100s_cpld.ko` to be loaded and the device registered at
  i2c-1/0x32. If the platform init service has not run, reads return `None`
  and all PSUs appear absent.
- **Cache TTL 30 s:** `psud` polls every 60 s. The 30 s TTL means at most one
  fresh read per `psud` poll. Do not reduce TTL without checking pmon I/O budget.
