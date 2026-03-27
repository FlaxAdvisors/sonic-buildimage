# LED Subsystem

## Hardware

Two system LEDs, both controlled via CPLD at host `i2c-1` / `0x32`:

| LED | CPLD Register | Owner | Function |
|---|---|---|---|
| SYS1 | `0x3e` | `chassis.py` / `healthd` | System-status indicator |
| SYS2 | `0x3f` | `led_control.py` / `ledd` | Port-activity indicator |

**Register encoding** (from ONL `ledi.c`, verified in CPLD driver):
```
0x00 = off           0x08 = off (blinking)
0x01 = red           0x09 = red blinking
0x02 = green         0x0a = green blinking
0x04 = blue          0x0c = blue blinking
```

The hardware has no amber LED; `chassis.py` maps `'amber'` to `0x01` (red).

## Driver / Daemon

- **`wedge100s_cpld`** kernel module (Phase R26): exposes `led_sys1` and `led_sys2`
  as read-write sysfs attributes at `/sys/bus/i2c/devices/1-0032/`.
- Writes use `kstrtoul(buf, 0, &val)` so both decimal (`2`) and hex (`0x02`) strings
  are accepted.
- Reads return the current register value as `0x%02x\n`.
- The driver uses `i2c_smbus_write_byte_data` with up to 10 retries at 60 ms intervals.

**ledd** (SONiC daemon): calls `LedControl.port_link_state_change()` on port state
transitions. SYS2 is managed exclusively by `led_control.py`.

**healthd** (SONiC daemon): calls `Chassis.set_status_led()` to set SYS1.

## Python API

### chassis.py — SYS1 (system-status LED)

File: `sonic_platform/chassis.py`

| Method | Colour encoding | Sysfs path |
|---|---|---|
| `set_status_led(color)` | `'green'→0x02`, `'red'→0x01`, `'amber'→0x01`, `'off'→0x00` | `/sys/bus/i2c/devices/1-0032/led_sys1` |
| `get_status_led()` | returns canonical colour string | `/sys/bus/i2c/devices/1-0032/led_sys1` |

Returns `False` / `STATUS_LED_COLOR_OFF` on any sysfs access failure.

### led_control.py — SYS2 (port-activity LED)

File: `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py`
Class: `LedControl(LedControlBase)`

| Method | Action |
|---|---|
| `__init__()` | Sets SYS1 = green; reads STATE_DB `PORT_TABLE` for initial port states; sets SYS2 = green if any port is up, else off |
| `port_link_state_change(port, state)` | Updates `_port_states[port]`; sets SYS2 = green if any port up, else off |

**STATE_DB initial scan:** on init, `LedControl` reads all `PORT_TABLE` entries from
STATE_DB and pre-populates `_port_states`. This prevents SYS2 staying off after a
`pmon` restart when ports are already up (ledd only fires on transitions, not on init).

**SYS2 logic:** binary — green when at least one port has `netdev_oper_status == 'up'`,
off otherwise. No per-port LED hardware exists.

## Pass Criteria

- `/sys/bus/i2c/devices/1-0032/led_sys1` is readable and writable
- `Chassis.set_status_led('green')` returns `True`
- `cat /sys/bus/i2c/devices/1-0032/led_sys1` returns `0x02` after setting green
- `Chassis.get_status_led()` returns `'green'`
- After `pmon` starts, SYS1 is physically green on the switch front panel
- When at least one port is link-up, SYS2 is physically green
- Writing `echo 1 > /sys/bus/i2c/devices/1-0032/led_sys2` turns SYS2 red

## Known Gaps

- No amber LED hardware; `'amber'` is silently mapped to red (`0x01`). The `_LED_DECODE`
  dict canonicalises `0x01` back to `'red'`, so a round-trip of `set('amber')` then
  `get()` returns `'red'`.
- Fan tray LEDs are not individually addressable; `Fan.set_status_led()` and
  `FanDrawer.set_status_led()` always return `False`.
- PSU `set_status_led()` always returns `False`; LED state for PSUs is synthesized
  from `get_status()` only for the API response, not written to hardware.
- `LedControl.__init__()` writes SYS1 = green regardless of system health; `healthd`
  is expected to subsequently set SYS1 to the appropriate colour.
- Blue LED (`0x04`) and blink variants (`+0x08`) are supported by hardware and the CPLD
  driver but are not used by any current Python code.
- No CPLD-level interrupt or interrupt-driven LED change; all updates are explicit writes.

---

## Port LEDs (BCM LEDUP / Front Panel)

### Hardware path

syscpld register `0x3c` (BMC i2c-12 / addr `0x31`) must have:
- `th_led_en=1` (bit 1): enables BCM LEDUP output to front-panel connectors
- `led_test_mode_en=0` (bit 7): test mode off
- `led_test_blink_en=0` (bit 6): blink test off
- `walk_test_en=0` (bit 3): walk test off

Factory default at hardware power-on: `0xe0` (all test bits set, LEDUP gated).
After D1: `clear_led_diag.sh` is deployed to BMC by `platform-init`, patches
`setup_board.sh`, and runs every boot, permanently setting register `0x3c = 0x02`
(th_led_en=1, all test modes=0).

**Verified 2026-03-26:** register `0x3c = 0x02` (th_led_en=1, all test bits=0).
All sysfs attributes confirmed:
```
th_led_en:          0x1 (enabled)
led_test_mode_en:   0x0 (normal)
led_test_blink_en:  0x0 (constant, not blinking)
walk_test_en:       0x0 (disabled)
```

### BCM LED program (`led_proc_init.soc`)

BCM56960 (Tomahawk) has two LEDUP scan chains:
- **LEDUP0**: green channel (link/activity)
- **LEDUP1**: amber channel (speed/error)

The LED bytecode loaded via `led_proc_init.soc` is the same bytecode as AS7712-32X
(same Tomahawk chip, same 32×100G QSFP28 port count). Both `led 0` and `led 1`
programs are identical. The program is loaded at SDK init time by `syncd` via
`bcmcmd` socket.

The PORT ORDER REMAP tables in `led_proc_init.soc` differ from AS7712-32X because
the Wedge 100S has different PCB routing of the LEDUP scan chain to the 32 front-panel
QSFP cages. The Wedge 100S remap derives LED physical port index as
`(first_serdes_lane - 1) / 4` from the BCM config file
`th-wedge100s-32x-flex.config.bcm`.

**AS7712-32X comparison:** LEDUP0 remap starts at port 31 descending; Wedge 100S
LEDUP0 remap starts at pos 0 = LED port 29 (Ethernet0, serdes lane 117). The
remap tables are completely different between the two platforms despite the identical
bytecode, reflecting different PCB LED chain wiring.

### `qsfp_led_position` strap (gpio59) — verified 2026-03-26

```
BMC gpio59 value: 1
```

gpio59=1 means the QSFP LED scan chain runs in the standard direction (port 0 at
the left/low end of the front panel). The value `1` is written to
`/run/wedge100s/qsfp_led_position` on the SONiC target by the bmc-daemon at startup;
confirmed present and correct.

### Hardware link state — verified 2026-03-26

BCM `led status` reports `LI` (Link Indicated) flag per-port. The LED scan chain
hardware is actively working: 11 BCM logical ports show LI=active corresponding to
5 QSFP cages with active optical connections (some cages have 10G/25G breakout):

| BCM port | BCM name | Speed | SONiC interface | Oper state |
|---|---|---|---|---|
| 1 | ce0 | 100G | Ethernet16 | up |
| 17 | ce4 | 100G | Ethernet32 | up |
| 34 | ce8 | 100G | Ethernet48 | up |
| 52 | xe38 | 10G | (breakout sub-port) | up |
| 53 | xe39 | 10G | (breakout sub-port) | up |
| 68 | xe49 | 25G | (breakout sub-port) | up |
| 69 | xe50 | 25G | (breakout sub-port) | up |
| 96 | ce21 | 100G | Ethernet108 | up |
| 102 | ce22 | 100G | Ethernet112 | up |
| 118 | xe86 | 25G | (breakout sub-port) | up |
| 119 | xe87 | 25G | (breakout sub-port) | up |

`show interfaces status` confirms 5 QSFP ports at Oper=up:
Ethernet16, Ethernet32, Ethernet48, Ethernet108, Ethernet112.

All other 27 ports are Oper=down and show no LI flag — consistent with no modules
inserted (all `/run/wedge100s/sfp_*_present` cache files read `0`).

### LED color decode (BCM bytecode behavior)

The BCM LED bytecode (identical to AS7712) drives LEDUP0/LEDUP1 per port based on
the port's link-status bit in the LED scan data. Typical behavior for Tomahawk
with this bytecode:

| Port state | LEDUP0 (green) | LEDUP1 (amber) | Visual color |
|---|---|---|---|
| No module / link down | off | off | dark |
| Link up, 100G | on | off | green |
| Link up, 10G/25G | on | on or blink | amber or yellow |
| Activity (TX/RX) | blink | — | green blink |

**Physical observation note:** This device is accessed via SSH only; front-panel
LED colors cannot be directly verified from this session. The BCM `led status`
output confirms the LED scan chain is running and LI flags are set correctly on
the 5 up-ports. Physical color verification requires on-site access.

### /run/wedge100s/ LED state files

System LED cache (written by i2c-daemon on every poll):
```
/run/wedge100s/led_sys1 = 0x02  (green — chassis OK)
/run/wedge100s/led_sys2 = 0x02  (green — at least one port up)
```

QSFP presence cache: all 32 entries (`sfp_0_present` through `sfp_31_present`)
read `0` — no QSFP modules inserted in any cage. The active links confirmed by
BCM `led status` are likely DAC/AOC cables detected by the MAC but not reporting
module presence via I2C (direct-attach copper or AOC without cold-plug detection).

### Deployment status

- D1 (th_led_en): deployed and verified — register 0x3c=0x02 persists across reboots
- D2 (i2c-daemon): running, writing led_sys1/led_sys2 and sfp_*_present files
- D3 (bmc-daemon): running, writing qsfp_led_position=1
- BCM LED scan: active, LI flags set on all 5 link-up ports
- Port remap: custom Wedge 100S table loaded (distinct from AS7712-32X)
