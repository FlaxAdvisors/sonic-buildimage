# I2C Bus Corruption Incident — 2026-03-27

## What happened

During investigation of the slow-reboot issue, the rule in CLAUDE.md was violated:
`i2cdetect -y 1` was run after stopping `wedge100s-i2c-daemon` but WITHOUT stopping `pmon`.

`pmon` was still running and accessed the CP2112 bus concurrently with `i2cdetect`.
This corrupted in-flight transactions and left the PCA9548 mux at 0x74 in a stuck state.

## Symptoms

- `wedge100s-i2c-daemon` crash loop: `mux_deselect_all failed (attempt 1/2)` every ~8s
- `cp2112 0003:10C4:EA90.0001: Transfer timed out, cancelling.` kernel messages at ~100ms intervals
- After USB driver rebind: device moved from `/dev/hidraw0` to `/dev/hidraw1`
- BMC `reset_qsfp_mux.sh` temporarily unstuck mux but CP2112 remained in bad state
- `cp2112_i2c_flush.sh` + mux reset + `rmmod/modprobe hid_cp2112` did not fully recover

## Recovery

Platform reset (wedge_power.sh reset via BMC) to clear CPLD and USB device state.

## Rule reinforced

**Stop ALL THREE before any i2c access:**
```bash
sudo systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon
```
Not just one or two. The mux corruption scenario requires all three stopped.

## Slow reboot investigation — pre-corruption findings

Before the bus corruption, the investigation found:
- Our custom daemons stop in 46ms — NOT contributing to the pause
- 13 Docker containers running when `/sbin/reboot` is called
- `stop_sonic_services()` in the reboot script only stops syncd and pmon
- 11 containers still running → systemd stops Docker during reboot → Docker stops all containers
- Docker default container stop timeout: 10s per container
- `TimeoutStopSec=5` should be added to both daemon service files (best practice)
- The root cause of the "big pause" is likely Docker container shutdown time

## Next steps for slow reboot fix

1. Add `TimeoutStopSec=5s` to `wedge100s-i2c-daemon.service` and `wedge100s-bmc-daemon.service`
2. Optionally: create `platform_reboot` script to `docker stop $(docker ps -q)` before calling `/sbin/reboot`
   — but this is a SONiC-wide behavior, not platform-specific
