# PSU Subsystem

## Hardware

- 2 × AC PSUs (650 W rated), labeled PSU-1 and PSU-2
- Hardware access split between two domains:

**Presence / power-good — Host CPLD (fast path):**
- CPLD at host `i2c-1` / `0x32` (`wedge100s_cpld` driver, Phase R26)
- Register `0x10` (PSU status):
  - Bit 0: PSU1 present (active-low; driver inverts to `psu1_present` = 1 when present)
  - Bit 1: PSU1 power good (active-high; `psu1_pgood` = 1 when good)
  - Bit 4: PSU2 present (active-low; driver inverts to `psu2_present` = 1 when present)
  - Bit 5: PSU2 power good (active-high; `psu2_pgood` = 1 when good)
- Live observed value: `0xe0` (both bits 7:6 set, all PSU bits clear at time of capture —
  PSU1 was unpowered in the lab)

**PMBus telemetry — BMC (slow path):**
- BMC bus 7, via PCA9548 mux at `0x70`:
  - PSU1: mux val `0x02`, address `0x59`
  - PSU2: mux val `0x01`, address `0x5a`
- PMBus registers read by daemon:
  - `0x88` READ_VIN — AC input voltage
  - `0x89` READ_IIN — AC input current
  - `0x8c` READ_IOUT — DC output current
  - `0x96` READ_POUT — DC output power
- All encoded as PMBus LINEAR11 16-bit words; daemon writes raw decimal integers
  to `/run/wedge100s/` files

## Driver / Daemon

- **`wedge100s_cpld`** kernel module (Phase R26): exposes CPLD attributes via sysfs at
  `/sys/bus/i2c/devices/1-0032/`. Provides `psu1_present`, `psu1_pgood`, `psu2_present`,
  `psu2_pgood` as read-only sysfs files.
- **`wedge100s-bmc-daemon`** (Phase R28): polls BMC PMBus via `/dev/ttyACM0`;
  writes raw LINEAR11 words (decimal integers) to:
  - `/run/wedge100s/psu_1_vin`, `/run/wedge100s/psu_1_iin`
  - `/run/wedge100s/psu_1_iout`, `/run/wedge100s/psu_1_pout`
  - Same set for `psu_2_*`
- `psud` (SONiC daemon in `pmon`) calls `Psu` API methods on a ~60 s cycle.

## Python API

- **Class:** `Psu` in `sonic_platform/psu.py` — inherits `PsuBase`; 1-based index 1–2
- **Instantiated by:** `Chassis.__init__()` as `Psu(i)` for `i` in 1–2

| Method | Returns | Source |
|---|---|---|
| `get_presence()` | `bool` | `/sys/bus/i2c/devices/1-0032/psu{N}_present` |
| `get_powergood_status()` | `bool` | `/sys/bus/i2c/devices/1-0032/psu{N}_pgood` |
| `get_status()` | `bool` | `get_presence() and get_powergood_status()` |
| `get_type()` | `'AC'` | hardcoded |
| `get_capacity()` | `650.0` (W) | hardcoded |
| `get_voltage()` | `float` (V) or `None` | `/run/wedge100s/psu_N_pout` ÷ `psu_N_iout` (POUT/IOUT) |
| `get_current()` | `float` (A) or `None` | `/run/wedge100s/psu_N_iout` (LINEAR11 decoded) |
| `get_power()` | `float` (W) or `None` | `/run/wedge100s/psu_N_pout` (LINEAR11 decoded) |
| `get_input_voltage()` | `float` (V) or `None` | `/run/wedge100s/psu_N_vin` (LINEAR11 decoded) |
| `get_input_current()` | `float` (A) or `None` | `/run/wedge100s/psu_N_iin` (LINEAR11 decoded) |
| `get_status_led()` | `'green'` or `'red'` | derived from `get_status()` |
| `set_status_led()` | `False` | not implemented |

**LINEAR11 decoder** (`_pmbus_decode_linear11(raw)`):
- Bits [15:11]: 5-bit two's-complement exponent N
- Bits [10:0]: 11-bit two's-complement mantissa Y
- Value = Y × 2^N (returns float in base SI units: V, A, W)

**VOUT computation:** `pout / iout` — avoids LINEAR16 `VOUT_MODE` complexity.
Returns `None` when `iout` is zero (no-load condition).

**Telemetry cache TTL:** 30 s (one dict per PSU, module-level `_psu_cache`).

## Pass Criteria

- `/sys/bus/i2c/devices/1-0032/psu2_present` reads `1` (PSU2 inserted)
- `/sys/bus/i2c/devices/1-0032/psu2_pgood` reads `1` (PSU2 power good)
- `Psu(2).get_presence()` returns `True`
- `Psu(2).get_powergood_status()` returns `True`
- `/run/wedge100s/psu_2_vin` is readable and decodes to ~200–240 V
- `/run/wedge100s/psu_2_pout` decodes to a positive wattage value
- `Psu(2).get_power()` returns a float > 0
- `show platform psustatus` shows both PSUs (or the live one) as `OK`

## Known Gaps

- `get_model()` and `get_serial()` return `'N/A'`. PMBus `MFR_MODEL` (reg `0x9a`) requires
  an SMBus block-read transaction not implemented in `bmc.py` (which only supports
  byte/word reads via `i2cget`).
- `set_status_led()` returns `False`; PSU LED control is not exposed via CPLD sysfs.
- PSU1 was unpowered in the lab during development; PSU1 PMBus telemetry is unverified
  on hardware.
- DC output voltage (`get_voltage()`) is derived from POUT/IOUT rather than read directly;
  returns `None` at no load (IOUT = 0) even if the PSU is present and powered.
- The `_CACHE_TTL` of 30 s means telemetry can be up to 30 s stale during a rapid
  power event.
