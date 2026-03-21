# Optical Port Investigation — 2026-03-20

## Hardware Under Test

| Port | Module | Peer | Fiber |
|------|--------|------|-------|
| Ethernet100 | Arista QSFP28-SR4-100G (SN G2120113967) | Arista Et26/1 | MPO multimode |
| Ethernet104 | Arista QSFP28-LR4-100G (SN S2109025969, Class 6, 4.5W) | Arista Et27/1 | LC single-mode |
| Ethernet108 | Arista QSFP28-SR4-100G (SN G2120114779) | Arista Et28/1 | MPO multimode |
| Ethernet116 | ColorChip CWDM4 (SN 17314400) | n/a | dead laser confirmed |

## Root Cause Chain Investigated

### 1. Stale EEPROM Cache — CONFIRMED BUG (fixed)

`sfp.py` `write_eeprom()` was inherited from `SfpOptoeBase`, which calls `get_eeprom_path()`.
On Wedge100S, `get_eeprom_path()` returns the daemon cache file
(`/run/wedge100s/sfp_N_eeprom`) when it exists.

**Effect:** ALL xcvrd control writes (TX_DISABLE clear, high-power-class enable) wrote
only to the cache file, never to the physical module over I2C. The module remained in its
power-on reset state indefinitely.

**Fix:** Overrode `write_eeprom()` in `sfp.py` to:
- Navigate mux tree via smbus2 on CP2112 bus 1 (muxes 0x70-0x73, ch = (bus-2)%8)
- Write to I2C addr 0x50 at the correct offset
- Re-read full 256 bytes from hardware
- Atomically replace daemon cache file

Mux topology (from wedge100s-i2c-daemon.c):
```
mux 0x70 ch0-7 → ONL buses  2- 9
mux 0x71 ch0-7 → ONL buses 10-17
mux 0x72 ch0-7 → ONL buses 18-25
mux 0x73 ch0-7 → ONL buses 26-33
channel = (bus - 2) % 8
```

### 2. DOM Cache — Never Updated (by design, non-polling arch)

Daemon only writes EEPROM cache on module insertion (valid ID byte). Stable modules are
served from cache forever. DOM values (TX bias, RX power) are insertion-time snapshots.

After the write_eeprom fix, cache is refreshed after every xcvrd write, so DOM reads
are current immediately following any control operation.

### 3. Hardware Register State (confirmed via physical I2C read)

After deploying the write_eeprom fix and forcing writes:

| Module | TX_DISABLE(86) | PwrCtrl(93) | CDR(98) | TX bias | RX power |
|--------|----------------|-------------|---------|---------|----------|
| Ethernet104 (LR4) | 0x00 ✓ | 0x02 ✓ | 0xFF ✓ | 0mA | -inf |
| Ethernet100 (SR4) | 0x00 ✓ | 0x00 (class 4, ok) | 0xFF ✓ | 0mA | -inf |

Bytes 9-12 = 0x55 on Arista modules — these are unimplemented registers that return
0x55 as a fixed pattern, not alarm conditions.

Ethernet100 byte 3 = 0x0F: all 4 host-side Tx LOS flags set — ASIC SerDes TX not
reaching the module on this port.

### 4. Peer Transceiver State (Arista EOS)

| Port | Bias | Tx Power | Rx Power |
|------|------|----------|----------|
| Et27/1 (→ Eth104) | 56.88 mA | 0.67 dBm | -30.00 dBm |
| Et28/1 (→ Eth108) | 6.50 mA | -0.06 dBm | -30.00 dBm |
| Et26/1 (→ Eth100) | 5.98 mA | -0.26 dBm | -30.00 dBm |

Arista IS transmitting on all 3 ports. Rx = -30 dBm is the floor (our TX = 0mA, so no
signal received). This implies: either the fiber is not physically connected on our end,
or our module is not receiving (high-loss fiber or module fault).

### 5. BCM SerDes State

ASIC port ce27 (Ethernet104) phy diag shows:
- TXAMP = 8,0 (SerDes TX is active)
- SD=0, LCK=0 (ASIC RX not receiving, no eye)
- UC_CFG = 0x0404 (same as working port ce22/Ethernet112)

SerDes configuration is identical to the working copper DAC port.

### 6. LP_MODE — Not Software Controllable

`sfp.py` stubs `get_lpmode()` and `set_lpmode()` as not accessible from host CPU.
PCA9535 chips are configured ALL INPUTS (CFG=0xFF) — confirmed on hardware.
BMC SYSCPLD has no QSFP LP_MODE sysfs attributes.
BCM ASIC has no accessible GPIO for LP_MODE.
ONL sfpi.c returns UNSUPPORTED for all QSFP control operations.

**LP_MODE may be hardwired HIGH on the Wedge100S-32X PCB**, forcing all QSFP modules
into low-power mode permanently. This would explain TX bias = 0mA despite all other
registers being correct. Hardware documentation needed to confirm.

## Status

- **Ethernet116**: Dead laser (ColorChip CWDM4) — replace transceiver
- **Ethernet104**: Software-side fully correct. TX = 0mA. LP_MODE suspect.
- **Ethernet108**: Mux navigation for bus 31 returns zeros (ch5 of mux 0x73 — needs check)
- **Ethernet100**: Tx LOS from host (byte 3 = 0x0F) — ASIC not driving SerDes to this module

## Software Fix Applied

`platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`:
- Added `smbus2` import with graceful fallback
- Added `_mux_for_bus()` helper encoding the 4-mux topology
- Added `write_eeprom()` override: real I2C write + hardware re-read + cache update

## Next Steps

1. Boot ONL on target — if optical ports TX laser fires under ONL, there is something
   in the ONL init sequence (or kernel driver) that clears LP_MODE that we are missing.
2. Check Wedge100S hardware schematic or Facebook OCP docs for LP_MODE pin routing.
3. Check if the BMC I2C bus can access a CPLD/GPIO that drives LP_MODE per-port.
4. Verify fiber connectivity — Arista is transmitting, confirm physical LC/MPO cables
   are seated in the correct Wedge100S QSFP cages.

(verified on hardware 2026-03-20)
