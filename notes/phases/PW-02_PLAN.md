# PW-02 — PSU Telemetry Fix: Plan

## Problem Statement

PSU telemetry (VIN, IIN, IOUT, POUT, VOUT) is polled from the OpenBMC via the
`wedge100s-bmc-daemon`. The daemon reads raw PMBus LINEAR11 16-bit words over the BMC's
`/dev/ttyACM0` TTY and writes them as plain decimal integers to `/run/wedge100s/psu_{N}_{reg}`.
`psu.py` then decodes these words using `_pmbus_decode_linear11()`.

There are two known accuracy concerns:

### Issue 1: VOUT is derived, not directly read

The current implementation computes:

```
VOUT = POUT / IOUT
```

This avoids reading `READ_VOUT` (PMBus register `0x8b`) because VOUT uses LINEAR16 format,
which requires first reading `VOUT_MODE` (register `0x20`) to determine the exponent. The
ONL `psui.c` uses the same approximation.

The approximation introduces up to ~2% error (rounding in POUT and IOUT) and is undefined
when IOUT = 0 (no-load condition). At no load, `get_voltage()` returns `None`.

### Issue 2: LINEAR11 exponent sign extension

`psu.py` `_pmbus_decode_linear11()` uses the threshold `exp_raw < 16` to determine sign,
treating values 16–31 as negative (−16 to −1). This is correct for a 5-bit two's complement
exponent. The mantissa uses `man_raw < 1024` (threshold 2^10) for sign extension.

The implementation is mathematically correct. However, it has not been validated against
known PSU output values on live hardware with a powered PSU (PSU1 had no AC power during
initial bringup; only PSU2 was live).

### Issue 3: PMBus register byte order

`i2cget -f -y 7 0xNN 0xRR w` on OpenBMC returns a 16-bit word in little-endian byte order
(low byte first), reported as a hex value like `0x1234`. The `parse_last_int()` in
`wedge100s-bmc-daemon.c` converts this to a signed C `int` via `strtol` with base 0,
which correctly handles `0x` prefix. The value is then written as a decimal integer.
`psu.py` reads this decimal and passes it directly to `_pmbus_decode_linear11()`.

The PMBus spec (Table 7) says LINEAR11 is encoded with the **exponent in bits [15:11]**
and **mantissa in bits [10:0]**. `i2cget -w` returns the 16-bit word as a single integer;
the byte order within that integer depends on the I2C implementation. If the BMC kernel
driver swaps bytes (host-endian conversion), the decoded value would be wrong by up to
a factor of ~8. This needs hardware validation.

## Proposed Approach

1. **Validate LINEAR11 decoding on live hardware** (PSU2 is live). Read raw PMBus words,
   apply the decoder, compare against known-good reference (e.g., panel meter on AC input,
   or cross-check with BMC `sensors` command output).

2. **Add direct VOUT read** if accuracy of `POUT/IOUT` is unacceptable. The fix:
   - Add PMBus register `0x20` (VOUT_MODE) read in `wedge100s-bmc-daemon.c`
   - Add PMBus register `0x8b` (READ_VOUT) read
   - Decode VOUT as: `VOUT = VOUT_raw * 2^(VOUT_MODE_exponent - 16)` (LINEAR16 format)
   - Write `/run/wedge100s/psu_{N}_vout_raw` and `/run/wedge100s/psu_{N}_vout_mode`
   - Update `psu.py` to read and decode these files

3. **Endianness validation**: On the BMC, compare `i2cget -w` output for READ_VIN
   against known AC input (~120 VAC or ~230 VAC). If decoded value is off by a large
   factor, byte-swap the 16-bit word before applying LINEAR11.

### Files to Change

| File | Change |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c` | Optionally add READ_VOUT + VOUT_MODE registers; fix byte order if needed |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py` | Optionally add `vout_raw`/`vout_mode` decode path |

## Acceptance Criteria

- `get_input_voltage()` returns a value within 10% of measured AC input (nominally ~120 or ~230 V)
- `get_voltage()` returns a value within 5% of nominal DC output (typically 12.0 V)
- `get_power()` and `get_current()` are self-consistent: `POUT ≈ VOUT × IOUT` within 5%
- `get_voltage()` returns a non-None value even at light load (IOUT not zero)

## Risks

- **Endian risk**: If byte order in `i2cget -w` output is swapped relative to PMBus spec,
  all four telemetry values (VIN, IIN, IOUT, POUT) are wrong. Validate all four before
  declaring LINEAR11 decoding correct.
- **No AC on PSU1**: PSU1 was unpowered during initial bringup. Testing must use PSU2.
  Validate PSU1 reads return `None` (not garbage) when AC is absent.
- **TTY contention**: Do not run the bmc-daemon manually while the systemd timer is active.
  Stop `wedge100s-bmc-poller.timer` before manual testing.
- **Modifying the C daemon requires a .deb rebuild**. Use the bmc-poller approach
  (manual i2cget on BMC via SSH) for initial validation before touching the C code.
