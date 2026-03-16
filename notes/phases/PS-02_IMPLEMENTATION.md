# PS-02 IMPLEMENTATION — Fan Subsystem

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/fan.py`
  (new file)

## Class Structure

Two classes are defined: `Fan(FanBase)` and `FanDrawer(FanDrawerBase)`.

### FanDrawer

- `drawer_index`: 1-based (1–5), stored as `self.index`
- Constructor appends exactly one `Fan(drawer_index)` to `self._fan_list`
- `get_presence()` delegates to `self._fan_list[0].get_presence()`
- `get_status()` delegates to `self._fan_list[0].get_status()`
- `set_status_led()` / `get_status_led()`: no per-tray LEDs; always returns
  `False` / `'off'`

### Fan

- `fan_index`: 1-based (1–5), stored as `self.index`
- Corresponds to a single fan tray (both rotors)

## Daemon File Layout

The daemon writes these files to `/run/wedge100s/`:

| File | Content |
|---|---|
| `fan_present` | `fantray_present` bitmask, decimal integer |
| `fan_N_front` | Front rotor RPM for tray N, decimal integer |
| `fan_N_rear` | Rear rotor RPM for tray N, decimal integer |

Where N is 1-based (1–5). BMC hardware source: i2c-8 / 0x33.

## Key Decisions

**Module-level caching.** `_fantray_cache` and `_rpm_cache` are module-level
dicts shared by all `Fan` instances. Cache TTL is 2 seconds (`_CACHE_TTL`).
This prevents 5 separate file reads when `thermalctld` polls all fans in one
pass. The cache is invalidated by `set_speed()` via `_rpm_cache.clear()`.

**min(front, rear) RPM policy.** `get_speed_rpm()` returns
`min(available_rpms)`. This matches ONL `fani.c` which marks a tray failed if
any rotor stalls. If one rotor reads 0, the tray reports 0 RPM (stalled).
If a daemon file is unreadable for one rotor, the other value alone is used
(`available = [v for v in (front, rear) if v is not None]`).

**`get_target_speed()` raises `NotImplementedError` until `set_speed()` is called.**
The module-level `_target_speed_pct = None` is the sentinel. This is the
correct behavior: `thermalctld` must not run is_under/over_speed checks before
it has issued a speed command.

**All trays share one speed target.** `set_fan_speed.sh` on the BMC sets PWM
for all 5 trays simultaneously (BMC fan board design). There is no per-tray
PWM.

**`get_speed_tolerance()` returns 20.** Common Accton policy across platforms
(confirmed in AS7712, AS9716, etc.). 20% allows for normal tray-to-tray
variation without false alarms.

**`get_direction()` returns `FAN_DIRECTION_INTAKE`.** Fixed, per ONL `fani.c`
comment "F2B = INTAKE". The Wedge 100S-32X airflow is front-to-back.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64, 2026-02-25):
- BMC sysfs path confirmed: `/sys/bus/i2c/devices/8-0033/fan1_input` ≈ 7500 RPM
- `fantray_present` = `0x0` when all 5 trays installed
- Fan tray indexing: tray 1 → `fan1_input` (front), `fan2_input` (rear)
  through tray 5 → `fan9_input` (front), `fan10_input` (rear)
- Daemon file `/run/wedge100s/fan_1_front` confirmed readable

## Remaining Known Gaps

- Per-fan-tray LED control is not implemented (hardware has no per-tray
  addressable LEDs accessible from the host).
- `get_model()` and `get_serial()` return `'N/A'` — fan trays have no
  identity EEPROM visible to the host.
- `set_speed()` failure (e.g. ttyACM0 not passed to pmon container) is
  silent: returns `False` without logging. The caller (`thermalctld`) handles
  this by trying again on the next cycle.
- No low-speed alarm threshold — only `get_speed_tolerance()` is used.
