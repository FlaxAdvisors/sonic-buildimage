# PS-03 IMPLEMENTATION — PSU Subsystem

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py`
  (new file)

## CPLD Sysfs Paths

```
/sys/bus/i2c/devices/1-0032/psu1_present   # 1 = present (driver inverts active-low bit 0 of reg 0x10)
/sys/bus/i2c/devices/1-0032/psu1_pgood     # 1 = power good (bit 1, active-high)
/sys/bus/i2c/devices/1-0032/psu2_present   # 1 = present (bit 4, active-low, inverted by driver)
/sys/bus/i2c/devices/1-0032/psu2_pgood     # 1 = power good (bit 5, active-high)
```

Implemented via `_read_cpld_attr(name)`, which reads and returns `int(val, 0)`.

## Daemon File Layout

Files written by `wedge100s-bmc-daemon` to `/run/wedge100s/`:

| File | PMBus register | Description |
|---|---|---|
| `psu_N_vin` | 0x88 READ_VIN | AC input voltage (V) |
| `psu_N_iin` | 0x89 READ_IIN | AC input current (A) |
| `psu_N_iout` | 0x8c READ_IOUT | DC output current (A) |
| `psu_N_pout` | 0x96 READ_POUT | DC output power (W) |

N is 1-based. Files contain raw LINEAR11 16-bit words as plain decimal integers.

## PMBus LINEAR11 Decoder

`_pmbus_decode_linear11(raw)` decodes as:

```
exponent = signed 5-bit: bits [15:11]
mantissa = signed 11-bit: bits [10:0]
value = mantissa × 2^exponent
```

Two's complement conversion: `exp_raw < 16` → positive; `exp_raw ≥ 16` →
subtract 32 for negative exponent (typical for fractional values like 0.5 A).
Returns `float` in base SI units (V / A / W) — SONiC API expects these units
directly, not milli-units.

## DC Output Voltage Calculation

`get_voltage()` returns `pout / iout`. This avoids implementing PMBus LINEAR16
(used by VOUT registers), which requires reading `VOUT_MODE` (register 0x20)
to obtain the fixed-point exponent. The ONL `psui.c` uses the same technique.

`get_voltage()` returns `None` when IOUT is zero (no load or PSU in standby).

## Key Decisions

**Separate caches for presence and telemetry.** `_read_cpld_attr()` reads live
from sysfs every call (fast, < 1 µs). Telemetry from daemon files is cached
for 30 s (`_CACHE_TTL`) to match `psud`'s 60 s poll interval.

**1-based indexing.** `Psu.__init__(index)` takes 1-based index. Internal
`self._idx = index - 1` is used for the `_psu_cache` array. File names use
1-based N via `psu_n = psu_idx + 1`.

**`get_type()` returns `'AC'`** — Wedge 100S-32X ships with AC input PSUs only.

**`get_capacity()` returns `650.0`** — rated watts. ONL `psui.c` also uses
650 W. No PMBus register is read for this; it is a hardware constant.

**`get_status_led()` returns green/red based on `get_status()`** — no dedicated
PSU LED sysfs attribute. The global SYS1/SYS2 LEDs are handled by
`chassis.py` and `led_control.py`.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, 2026-02-25):
- PSU2 was live; PSU1 had no AC power in the lab
- CPLD `psu2_present` = 1, `psu2_pgood` = 1 (verified)
- CPLD `psu1_present` = 1 (card inserted), `psu1_pgood` = 0 (no AC)
- Daemon files `/run/wedge100s/psu_2_vin`, `psu_2_iin`, `psu_2_iout`,
  `psu_2_pout` all readable and decoded correctly

## Remaining Known Gaps

- `get_model()` and `get_serial()` return `'N/A'`. Reading MFR_MODEL (PMBus
  0x9a) requires an SMBus block-read transaction; `bmc.py` only implements
  word/byte reads over the TTY interface.
- VOUT is computed as POUT/IOUT. Under very light load (IOUT < 0.1 A), the
  result may be imprecise. Direct VOUT register reading would require LINEAR16
  decode.
- The `_psu_cache` is per-process. If pmon restarts, the cache is cleared
  immediately. This is correct behavior.
