# PF-04 — BMC Daemon: Implementation

## What Was Built

### `utils/wedge100s-bmc-daemon.c`

C binary, ~423 lines. Build: `gcc -O2 -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c`.
Installed to `/usr/bin/wedge100s-bmc-daemon`.

**Architecture:** single-session TTY polling. Opens `/dev/ttyACM0` once, logs in
once, reads all sensors, writes output files, closes and exits.

**TTY configuration:**
- 57600 baud, 8N1, raw mode (`IGNPAR`, `c_lflag=0`, `c_oflag=0`)
- Opened initially with `O_NONBLOCK`, then switched to blocking with `VMIN=1`
  after `tcsetattr`. USB CDC does not signal `select()` correctly under
  `O_NONBLOCK` on this kernel — blocking with `select()` timeouts is correct.
- Pattern from ONL `platform_lib.c` with `select()`-based timeouts replacing
  ONL's `usleep + read` polling.

**Login sequence (`tty_login`):**
1. Send `\r\x00` (CR + null byte, same as ONL)
2. Wait up to 1 s for `":~# "` prompt
3. If `" login:"` seen: send `"root\r\x00"`, wait for `"Password:"`, send
   `"0penBmc\r\x00"`, wait for prompt
4. Retry up to 10 times with 50 ms sleep between attempts

**Command execution (`send_cmd`):**
- Appends null byte after `\r\n` (mirrors ONL `write(fd, buf, strlen+1)`)
- Calls `drain()` before each write (100 ms settle to clear echo/prompt leftovers)
- `read_until()` blocks until `":~# "` prompt or 8 s timeout

**`parse_last_int(resp, cmd, base)`:**
- Finds the LAST occurrence of `cmd` in the response buffer (handles echo)
- Parses the first numeric token following it
- Returns `INT_MIN` on failure (caller skips writing the file)

**Thermal sensors (7 reads):**
```c
static const char *const thermal_paths[7] = {
    "/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input",
    ...
    "/sys/bus/i2c/devices/8-0049/hwmon/*/temp1_input",
};
```
Path uses BMC shell glob (`*`) for hwmon index. Output: millidegrees C decimal.

**Fan presence (1 read):**
`cat /sys/bus/i2c/devices/8-0033/fantray_present` → hex bitmask parsed with `base=0`.
Written to `/run/wedge100s/fan_present`.

**Fan RPM (10 reads):**
Front rotor: `fan(2*i-1)_input`, rear rotor: `fan(2*i)_input` for i=1..5.
Per `fani.c fid*2-1` / `fid*2` convention.

**PSU PMBus (2×(1+4) reads):**
- Mux select: `i2cset -f -y 7 0x70 0x{mux_ch:02x}` (single-byte, NO register prefix)
- PSU1: mux channel `0x02`, PMBus address `0x59`
- PSU2: mux channel `0x01`, PMBus address `0x5a`
- Registers: `0x88` (VIN), `0x89` (IIN), `0x8c` (IOUT), `0x96` (POUT)
- `i2cget -f -y 7 0xNN 0xNN w` returns `0xNNNN` (parsed with `base=0`)
- Written as raw LINEAR11 word (not decoded in the daemon)

### Complete output file list

```
/run/wedge100s/thermal_1 .. thermal_7    millidegrees C
/run/wedge100s/fan_present               bitmask (0=all present)
/run/wedge100s/fan_1_front .. fan_5_front  front rotor RPM
/run/wedge100s/fan_1_rear  .. fan_5_rear   rear rotor RPM
/run/wedge100s/psu_1_vin, psu_1_iin, psu_1_iout, psu_1_pout
/run/wedge100s/psu_2_vin, psu_2_iin, psu_2_iout, psu_2_pout
```

Total: 25 files.

### `service/wedge100s-bmc-poller.timer`

```ini
OnBootSec=15
OnUnitActiveSec=10
AccuracySec=1
```

15 s on boot gives platform-init and pmon time to start before first cache write.
10 s poll keeps data ≤10 s stale for `thermalctld` 60 s cycle.

### `service/wedge100s-bmc-poller.service`

`Type=oneshot`, `ExecStart=/usr/bin/wedge100s-bmc-daemon`, `TimeoutStartSec=30`,
`LogLevelMax=notice` (suppresses INFO-level lifecycle messages from journald —
8,600 lines/day at 10 s interval without this setting).

### Python layer (Phase R29)

`thermal.py`, `fan.py`, `psu.py` all changed to read from `/run/wedge100s/` files
instead of calling `bmc.file_read_int()` or `bmc.i2cget_word()`. Fan write path
(`set_fan_speed.sh`) remains a TTY call via `bmc.send_command()`.

## Hardware-Verified Facts

- Daemon full poll completes in 3–5 s (vs 65 s Python per-call) — pending hardware
  test (R28 notes: "pending hardware test" as of 2026-03-14)
- Timer `OnBootSec=15`, `OnUnitActiveSec=10` verified in service file
- Expected thermal range verified in lab: ~23000–40000 millidegrees (23–40°C)
- Expected fan RPM range: fan_N_front ~7500, fan_N_rear ~4950 (per fani.c typical)
- `fan_present=0` expected in lab (all 5 trays installed)

## Remaining Known Gaps

- PSU PMBus telemetry (VIN/IIN/IOUT/POUT) returns `None`/`0.0` in Python for PSU1
  (no AC) — expected. PSU2 decoding via `_pmbus_decode_linear11()` pending full
  hardware verification (Phase PW-02).
- No cross-process TTY lock (`fcntl.flock`). Single consumer (daemon) assumption
  holds; add if a second TTY consumer is ever introduced.
- Fan set_speed still uses bmc.py TTY directly — the only remaining TTY dependency
  after R29.
