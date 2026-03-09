# IRQ 18 Reduction: Phases 1 and 2 (2026-03-09)

## Problem

Interactive SSH sessions on hare-lorax (SONiC Wedge 100S-32X) were
near-unusable due to sustained high-priority softirq load on CPU2.
Root cause: pmon's get_change_event() reading 33 GPIO sysfs pins per
0.1s loop = 330 I2C reads/sec through the CP2112 USB bridge → IRQ 18
at ~800/sec saturating CPU2 with HI softirqs.

## Baseline (before fix)

- IRQ 18 (i801_smbus + ehci_hcd:usb1): ~800/sec
- HI softirq CPU2: >200M accumulated, CPU-saturating
- SSH response: 15-30s blocked windows

## Phase 1: Bulk presence read (get_change_event)

**File:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`

Replaced the 33-iteration GPIO sysfs loop (330 reads/sec) with a
4-register smbus2 bulk read of the PCA9535 INPUT registers.

### Hardware mapping (verified on hardware 2026-03-09)

```
PCA9535 at i2c-36/0x22 (gpiochip596, "36-0022"): ports 0-15
PCA9535 at i2c-37/0x23 (gpiochip612, "37-0023"): ports 16-31
```

### Bit ordering (XOR-1 interleave)

GPIO lines are wired with XOR-1 interleave (from ONL sfpi.c):
- Register INPUT0 bit b → GPIO line b → port = group*16 + (b ^ 1)
- Register INPUT1 bit b → GPIO line 8+b → port = group*16 + (8+b ^ 1)

Formula: bit b in register r on chip group g → port = g*16 + (r*8+b)^1

Verified correct: bulk read matches individual GPIO sysfs reads for all
ports (verified 2026-03-09 with QSFPs in ports 0,4,8,12,16,20,26,27,28).

### smbus2 force=True required

gpio-pca953x driver holds the I2C device address. smbus2
`read_byte_data(addr, reg, force=True)` uses I2C_SLAVE_FORCE ioctl
to bypass the driver ownership check. Without force=True: EBUSY error.

### Sleep interval

Increased from 0.1s to 1.0s between polls — module insertion/removal
is a human-scale event; 1s polling is more than adequate.

### SMBus handles

Opened once in Chassis.__init__() and kept open — eliminates repeated
fd open/close overhead (each open previously also triggered USB setup).

## Phase 2: psud poll interval 3s → 30s

**File:** `src/sonic-platform-daemons/sonic-psud/scripts/psud`

Changed `PSU_INFO_UPDATE_PERIOD_SECS = 3` to `30`.
PSU insertion/removal is human-scale; 10× less frequent polling.

## Measured results (verified on hardware 2026-03-09)

| Metric | Before | After |
|---|---|---|
| IRQ 18/sec | ~800 | ~72 |
| HI softirq/sec CPU2 | CPU-saturating | ~38 |
| I2C reads/sec (presence) | 330 | ~8 |

The 72 IRQ/sec residual is from thermalctld, ledd, and the 2×4 smbus2
reads per second from xcvrd's get_change_event() calls.

## Persistence

- **chassis.py**: Wheel rebuilt and installed to
  `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl`
  Postinst now force-reinstalls the wheel into running pmon container.
- **psud**: Patched in source; postinst patches running container.
  Permanent fix requires full sonic-platform-daemons rebuild.

## Files modified

| File | Change |
|---|---|
| `sonic_platform/chassis.py` | Rewrite get_change_event() with _bulk_read_presence() |
| `src/sonic-platform-daemons/sonic-psud/scripts/psud` | PSU_INFO_UPDATE_PERIOD_SECS 3→30 |
| `debian/sonic-platform-accton-wedge100s-32x.postinst` | Add wheel reinstall + psud patch |
