# Ethernet104/108 Optical Port Investigation — 2026-03-20

**Branch:** wedge100s
**Target:** hare-lorax (admin@192.168.88.12)
**Status:** LP_MODE root cause confirmed; hardware deassertion proven to work; software
init path not yet wired in.

---

## Hardware Under Test

| Port | Port Index (0-based) | Module | Peer |
|------|----------------------|--------|------|
| Ethernet104 | 26 | Arista QSFP28-LR4-100G (SN S2109025969, Class 6, 4.5W) | Arista Et27/1 |
| Ethernet108 | 27 | Arista QSFP28-SR4-100G (SN G2120114779, Class 4, 3.5W) | Arista Et28/1 |
| Ethernet100 | 25 | Arista QSFP28-SR4-100G (SN G2120113967) | Arista Et26/1 |
| Ethernet116 | 29 | ColorChip CWDM4 (SN 17314400) | n/a — dead laser |

---

## QSFP28 Control Knobs (SFF-8636)

There are three independent mechanisms that can keep a QSFP28 module's TX lasers off.
Understanding them is essential for diagnosing any optical link failure.

### 1. Software TX_DISABLE (byte 86, bits 3:0)

Byte 86 is a writable EEPROM register accessible over I2C to the host CPU.
Each bit disables one TX lane: bit 0 = Lane 1, bit 1 = Lane 2, etc.

- Value `0x00` = all lanes enabled (TX_DISABLE deasserted in software)
- Value `0x0F` = all 4 lanes disabled

This is the register `xcvrd` writes when `admin shutdown` is applied to a port.
**On all ports under investigation, byte 86 = 0x00 (TX enabled).** This is NOT the blocker.

### 2. Hardware LP_MODE Pin (external GPIO, active-HIGH)

Each QSFP cage has a hardware `LP_MODE` (Low Power Mode) pin driven by an external
GPIO. When this pin is HIGH, the module self-limits to ≤1.5W, keeps TX lasers off,
and does not enable CDR or high-gain amplifiers.

**On the Wedge100S-32X, all 32 LP_MODE pins are driven by two PCA9535 GPIO expanders
connected to the host CPU via CP2112 bus 1 (mux 0x74).** See discovery below.

The module reports its LP_MODE state in byte 1 bit 3 (`In LPM Status`):
- bit 3 = 0 → module is in high-power mode
- bit 3 = 1 → module reports it is in LP_MODE

### 3. Software Power Override (byte 93, bits 1:0)

Byte 93 allows the host CPU to override the hardware LP_MODE pin via software:

| Byte 93 value | Meaning |
|---|---|
| `0x00` | Hardware LP_MODE pin controls power mode (default after reset) |
| `0x01` | Software override active; Power Set = 0 → HIGH POWER (LP_MODE deasserted) |
| `0x03` | Software override active; Power Set = 1 → LOW POWER (LP_MODE asserted via software) |
| `0x05` | Software override; Power Set = 0; High Power Class Enable (Class 5-7) → HIGH POWER |
| `0x07` | Software override; High Power Class Enable; Power Set = 1 → LOW POWER |

Bits:
- Bit 0 (`Power Override`): 1 = software controls power mode, 0 = hardware LP_MODE pin controls
- Bit 1 (`Power Set`): when Override=1, 1 = LP_MODE (low power), 0 = high power
- Bit 2 (`High Power Class Enable, Class 5-7`): must be set for Class 5-7 modules to draw >3.5W
- Bit 3 (`High Power Class Enable, Class 8`): must be set for Class 8 modules

When byte 93 bit 0 (Power Override) = 1, the hardware LP_MODE pin is **ignored** by the
module. Software entirely controls power state.

**xcvrd's role:** `sff_mgr.py` calls `api.set_lpmode(False)` on QSFP28 modules, which
(via `Sff8636Api`) writes byte 93 Power Override=1, Power Set=0 (high power). For Class
5+ modules it also writes the High Power Class Enable bit first. This should unlock the
laser via software even with the hardware LP_MODE pin asserted.

---

## Investigation Timeline

### Phase 1: Initial Diagnosis (wrong root cause)

Early investigation concluded that TXDIS was controlled by the BMC via the mux board,
unreachable from the host CPU. File was marked BLOCKED. **This was incorrect.**

The real issue was that both hardware LP_MODE and the software byte-93 path were
simultaneously keeping the modules in low-power mode, and neither was being deasserted
by the platform code.

### Phase 2: Stale EEPROM Cache Fix

`write_eeprom()` in `sfp.py` was inherited from `SfpOptoeBase`, which calls
`get_eeprom_path()`. On Wedge100S, that returned the daemon cache file. All xcvrd
control writes (TX_DISABLE, byte 93) were writing only to the cache file, never
reaching physical hardware over I2C.

**Fix:** Overrode `write_eeprom()` to navigate the mux tree via smbus2, write to
hardware, re-read 256 bytes from hardware, and atomically replace the cache file.

### Phase 3: LP_MODE Discovery (2026-03-20)

After the write_eeprom fix, byte 86 = 0x00 and byte 93 = 0x02 (Power Override set,
high power) were correctly reaching hardware. TX bias was still 0mA.

#### I2C Scan (with daemon timers stopped to avoid bus contention)

```bash
# Stop daemon timers before any manual I2C access
sudo systemctl stop wedge100s-i2c-poller.timer wedge100s-bmc-poller.timer
```

Scanning mux 0x74 (PCA9548, previously listed as "unassigned" in i2c_topology.json):

| Mux 0x74 Channel | Bus | Device Found | Purpose |
|---|---|---|---|
| ch0 | 34 | PCA9535 @ 0x20 | **LP_MODE ports 0-15** |
| ch1 | 35 | PCA9535 @ 0x21 | **LP_MODE ports 16-31** |
| ch2 | 36 | PCA9535 @ 0x22 | Presence ports 0-15 |
| ch3 | 37 | PCA9535 @ 0x23 | Presence ports 16-31 |
| ch4 | 38 | PCA9535 @ 0x24 | INT_L ports 0-15 |
| ch5 | 39 | PCA9535 @ 0x25 | INT_L ports 16-31 |
| ch6 | 40 | 24C64 EEPROM @ 0x50 | Board EEPROM |
| ch7 | 41 | PCA9535 @ 0x26 | (TBD) |

**LP_MODE chips at 0x20/0x21:** Config registers (0x06/0x07) = 0xFF = all inputs.
PCB pull-ups hold all LP_MODE lines HIGH (asserted = low-power mode). All 32 QSFP
modules boot into LP_MODE and stay there because no software deasserts the pins.

#### Bit Mapping

Same XOR-1 interleave as the presence chips (ONL sfpi.c):

```
port_index = 0-based port number (Ethernet108 = port 27)
group = port_index // 16           # 0 = chip 0x20, 1 = chip 0x21
line  = (port_index % 16) ^ 1     # XOR-1 interleave
reg   = line // 8                  # PCA9535 port0 (0x06/0x02) or port1 (0x07/0x03)
bit   = line % 8                   # bit within the byte

# For Ethernet108 (port 27):
# group=1 → chip 0x21 (mux 0x74 ch1)
# line = (27%16)^1 = 11^1 = 10
# reg = 10//8 = 1  (port1 = config reg 0x07, output reg 0x03)
# bit = 10%8 = 2
```

Active-HIGH: LP_MODE pin HIGH = module in low-power mode.
To DEASSERT LP_MODE: configure pin as output, drive LOW.
To ASSERT LP_MODE: set pin as input (pull-up drives HIGH).

#### Proof of Concept (hardware verified 2026-03-20)

Experiment: deasserted LP_MODE for Ethernet108 (port 27) only.

```python
# chip 0x21, ch1 of mux 0x74
# port27 → group=1, line=10, reg=1, bit=2
with SMBus(1) as i2c:
    i2c.write_byte(0x74, 1 << 1)              # select mux 0x74 ch1
    cfg = i2c.read_byte_data(0x21, 0x07)      # read port1 config
    out = i2c.read_byte_data(0x21, 0x03)      # read port1 output
    i2c.write_byte_data(0x21, 0x03, out & ~(1<<2))  # output register bit2 = LOW
    i2c.write_byte_data(0x21, 0x07, cfg & ~(1<<2))  # config bit2 = output
    i2c.write_byte(0x74, 0x00)
```

**Result after 500ms:**

| Metric | Before | After |
|--------|--------|-------|
| TX Bias L1-L4 | 0.00 mA | **6.40 mA** all lanes |
| RX Power | -inf | **+0.3 dBm** (Arista peer signal received) |
| Byte 1 bit 3 (InLPM) | 1 | **0** (module confirms high-power) |

LP_MODE was then restored (chip 0x21 bit2 set back to input) and TX bias returned to 0.

---

## Current State of sfp.py

`sfp.py` now implements:

| Method | Implementation |
|--------|---------------|
| `get_lpmode()` | Reads PCA9535 config register via mux 0x74; input pin = LP_MODE asserted (True) |
| `set_lpmode(False)` | Drives PCA9535 pin LOW via mux 0x74 → deasserts LP_MODE hardware |
| `set_lpmode(True)` | Sets PCA9535 pin as input → pull-up reasserts LP_MODE |
| `write_eeprom()` | Real I2C write via smbus2 + hardware re-read + cache update |
| `_hardware_read_eeprom()` | Reads 256 bytes directly from QSFP via smbus2 |

**Gap:** `get_lpmode()`/`set_lpmode()` in sfp.py are the PLATFORM-LAYER methods.
For QSFP28 modules xcvrd calls `api.set_lpmode()` (the xcvr_api path, which writes
byte 93), NOT `sfp.set_lpmode()`. The sfp.py hardware LP_MODE pin is never deasserted
by xcvrd in normal operation. See Work Items below.

---

## xcvrd Byte-93 State (post pmon restart)

After pmon restart, byte 93 for Ethernet108 = `0x03`:
- bit 0 (Power Override) = 1: software override active
- bit 1 (Power Set) = 1: LP_MODE via software ← WRONG (should be 0 = high power)
- bit 2 (High Power Class Enable) = 0: not set ← WRONG for Class 6 module

Hardware LP_MODE pin also still asserted (PCA9535 input mode / pull-up HIGH).

Both mechanisms are simultaneously asserting LP_MODE. xcvrd's byte-93 path appears to
be writing Power Set=1 (low power) instead of 0 (high power) — this is a separate bug
to investigate in `sff_mgr.py` logic.

---

## I2C Bus Ownership and Contention

The CP2112 USB-I2C bridge is accessed via two kernel interfaces:

| Interface | Used by | Access method |
|-----------|---------|---------------|
| `/dev/hidraw0` | `wedge100s-i2c-daemon` (C, host) | libhid raw USB HID writes |
| `/dev/i2c-1` | `sfp.py` (Python, pmon container) | smbus2 / kernel i2c-dev |

These are **two interfaces to the same physical bus**. There is no kernel-level
serialization between them. The daemon fires every 3 seconds; `sfp.py` acquires
`_eeprom_bus_lock` (in-process RLock) to serialize within xcvrd. But a daemon poll
concurrent with an sfp.py smbus2 transaction will cause bus corruption.

Currently the daemon timers must be stopped before any manual i2c access from the host.
In production, the 3s gap between daemon polls is the only protection.

---

## Work Items: LP_MODE Init (see brainstorm below)

The correct long-term fix requires deassert of all 32 LP_MODE hardware pins at
platform initialization time, coordinated through the same I2C ownership model as the
rest of the daemon architecture. See the brainstorm section.

---

## Ethernet100 and Ethernet116

**Ethernet116 (ColorChip CWDM4):** Dead laser — TX_FAULT bits set on all 4 lanes.
Hardware failure; module must be replaced.

**Ethernet100 (SR4):** Byte 3 = 0x0F — all 4 host-side TX LOS flags set. ASIC SerDes
is not driving signal to this port. LP_MODE deassertion will not fix this; BCM ASIC
SerDes configuration or port bringup sequence is likely the issue.

---

## Brainstorm: xcvrd-Driven LP_MODE Init via i2c Daemon IPC

### Problem Statement

`set_lpmode()` in `sfp.py` currently accesses `/dev/i2c-1` directly via smbus2.
The i2c daemon owns the bus via `/dev/hidraw0`. These must not run concurrently.

The daemon must be the single owner of the CP2112 bus. LP_MODE operations need to
go through the daemon so they participate in the daemon's serialization.

### Control Flow Today

```
wedge100s-i2c-daemon (host, C)
  └─ polls hidraw0 every 3s
  └─ writes /run/wedge100s/sfp_N_present
  └─ writes /run/wedge100s/sfp_N_eeprom

pmon container (xcvrd / sfp.py)
  └─ reads /run/wedge100s/sfp_N_present   ← no bus access
  └─ reads /run/wedge100s/sfp_N_eeprom    ← no bus access
  └─ write_eeprom() → smbus2 /dev/i2c-1  ← DIRECT BUS ACCESS (contention risk)
  └─ set_lpmode() → smbus2 /dev/i2c-1    ← DIRECT BUS ACCESS (contention risk)
```

### Target Architecture

```
wedge100s-i2c-daemon (host, C) — sole owner of CP2112 bus
  ├─ polls presence (existing)
  ├─ polls EEPROM on insertion (existing)
  ├─ owns LP_MODE deassert at startup (NEW)
  └─ processes LP_MODE request files (NEW)
      reads  /run/wedge100s/sfp_N_lpmode_req  ("0" = deassert, "1" = assert)
      writes /run/wedge100s/sfp_N_lpmode      (actual hardware state)

pmon container (xcvrd / sfp.py)
  ├─ get_lpmode() → reads /run/wedge100s/sfp_N_lpmode
  └─ set_lpmode() → writes /run/wedge100s/sfp_N_lpmode_req, polls for ack
```

### Work Items

**Item 1: Daemon startup LP_MODE deassert (wedge100s-i2c-daemon.c)**

On daemon start, after first presence poll:
- For each present port (sfp_N_present == "1"):
  - Navigate mux 0x74 ch0 (ports 0-15) or ch1 (ports 16-31)
  - Configure PCA9535 (0x20/0x21) pin as output driven LOW
  - Write `/run/wedge100s/sfp_N_lpmode` = "0" (deasserted)
- For absent ports: write `/run/wedge100s/sfp_N_lpmode` = "1" (pull-up = asserted)

This ensures all present QSFP modules come out of LP_MODE before xcvrd initializes,
with no bus contention (daemon is the only I2C owner at this point).

**Item 2: Daemon LP_MODE request processing (wedge100s-i2c-daemon.c)**

In the daemon's main poll loop (existing 3s cycle):
- For each port, check if `/run/wedge100s/sfp_N_lpmode_req` exists
- If req file differs from current lpmode state:
  - Apply PCA9535 change (same logic as Item 1)
  - Update `/run/wedge100s/sfp_N_lpmode`
  - Remove or update the req file

**Item 3: sfp.py get_lpmode() / set_lpmode() via files**

Replace current smbus2 implementations:
- `get_lpmode()` → reads `/run/wedge100s/sfp_N_lpmode` (written by daemon)
- `set_lpmode(lpmode)` → writes `/run/wedge100s/sfp_N_lpmode_req`, returns True

Note: set_lpmode() becomes asynchronous (daemon applies on next 3s cycle).
For xcvrd this is acceptable — it does not require synchronous LP_MODE confirmation.

**Item 4: Fix xcvrd byte-93 Power Set bug**

After LP_MODE hardware deassertion is working, investigate why sff_mgr.py writes
byte 93 = 0x03 (Power Override + Power Set = low power) instead of 0x01/0x05
(Power Override + high power). Likely: `api.set_lpmode(lpmode)` is being called with
lpmode=True instead of False, or the call is not being reached at all.

This is secondary — hardware LP_MODE deassertion (Item 1) is sufficient to bring up
the laser. Byte 93 Power Override=0 means hardware LP_MODE pin controls, which is
correct once hardware pin is driven LOW.

**Item 5: Update notes/i2c_topology.json**

Add the newly discovered devices to the topology map:
- mux 0x74 ch0: PCA9535 0x20 (LP_MODE ports 0-15)
- mux 0x74 ch1: PCA9535 0x21 (LP_MODE ports 16-31)
- mux 0x74 ch4: PCA9535 0x24 (INT_L ports 0-15)
- mux 0x74 ch5: PCA9535 0x25 (INT_L ports 16-31)

**Item 6: Validate all 32 ports**

After daemon LP_MODE init is wired in, verify all 32 ports:
- Present ports: LP_MODE deasserted on daemon start
- TX bias reads back >0 mA for all ports with modules installed
- Hot-plug: insertion triggers LP_MODE deassert for that port

### Sequencing Risk

The daemon deasserts LP_MODE before xcvrd has initialized. xcvrd may then write
byte 93 with an incorrect value (Item 4 bug) which could re-assert LP_MODE via
software override. Fix sequence: Item 1 (daemon hw deassert) + Item 4 (byte-93
fix) must both land before optical links can be expected to stay up through a pmon
restart.

(verified on hardware 2026-03-20)
