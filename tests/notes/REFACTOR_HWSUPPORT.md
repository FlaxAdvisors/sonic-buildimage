# REFACTOR_HWSUPPORT.md
# Wedge 100S-32X: Hardware Support Refactoring Plan

## Problem Statement

Interactive SSH sessions on the SONiC switch are near-unusable due to sustained
high-priority softirq load. The root cause is **IRQ 18** (`ehci_hcd:usb1` +
`i801_smbus`) accumulating **~800 interrupts/second** — more than 5× the BCM ASIC
IRQ 16 rate (~150/sec). This originates from the pmon Python daemons hammering the
CP2112 USB-HID I2C bridge with individual per-port reads.

## Root Cause Analysis

### Hardware Path

```
pmon daemon (Python)
  → smbus2 / gpio sysfs read
    → gpio-pca953x or smbus kernel driver
      → CP2112 USB-HID bridge
        → ehci_hcd USB controller
          → IRQ 18 (~800/sec)
```

### The Primary Offender: `chassis.get_change_event()`

xcvrd calls `get_change_event(timeout=1000ms)` once per second. Our implementation:

```python
# chassis.py — current broken pattern
def get_change_event(self, timeout=0):
    while True:
        for idx in range(1, NUM_SFPS + 1):   # 33 iterations
            present = sfp.get_presence()      # 1 GPIO sysfs read each
        time.sleep(0.1)                       # 10 loops per second
```

**Math:** 33 ports × 10 loops/sec = **330 GPIO sysfs reads/second**, each triggering
a PCA9535 I2C register read through the CP2112 USB bridge.

### What ONL Does Instead

ONL's `onlp_sfpi_presence_bitmap_get()` (sfpi.c):

```c
/* 4 I2C reads total for all 32 ports */
reg_val = onlp_i2c_readb(36, 0x22, 0, ONLP_I2C_F_FORCE);  /* ports 0-7  */
reg_val = onlp_i2c_readb(36, 0x22, 1, ONLP_I2C_F_FORCE);  /* ports 8-15 */
reg_val = onlp_i2c_readb(37, 0x23, 0, ONLP_I2C_F_FORCE);  /* ports 16-23 */
reg_val = onlp_i2c_readb(37, 0x23, 1, ONLP_I2C_F_FORCE);  /* ports 24-31 */
```

4 I2C reads, called **once per poll cycle** at a reasonable interval. Our current
approach generates **330 reads/second** for the same information.

### Comparison of I2C Access Rates

| Subsystem | ONL reads/sec | SONiC reads/sec | Ratio |
|---|---|---|---|
| QSFP presence (32 ports) | 0.2 (4 reads/5s) | 330 (33 reads × 10/s) | 1650× |
| PSU status | 0.03 (1 read/30s) | 0.67 (1 read/3s × 2 PSUs) | 22× |
| Fan presence/RPM | via BMC TTY | via BMC ttyACM0 | similar |
| Temperature | hwmon sysfs | hwmon sysfs | similar |

### Secondary Contributors

- **psud**: polls PSU CPLD every **3 seconds** — ONL pattern is on-demand or ~30s
- **thermalctld**: 15s fast-start interval generates 8 thermal reads during startup
- **ledd**: polling LED state (unknown interval, probably minor)

---

## Implementation Status

| Phase | Status | Notes |
|-------|--------|-------|
| 1: Bulk presence read | **DONE** | Verified on hardware; IRQ 18 ~330/sec → ~4/sec |
| 2: psud interval 3s→30s | **DONE** | Patched in pmon container via postinst |
| 3: Persistent SMBus pool | **DONE** | platform_smbus.py; psu.py uses it; force=True for kernel-driver-bound |
| 4: GPIO edge detection | **Infeasible** | PCA9535 INT# not wired to host CPU; dmesg: "using no AI" |
| 5: SSH boot-gap root cause | **Investigated** | BCM ASIC IRQ 16 burst during init; see notes below |
| 6: IRQ affinity tuning | **Tested/Ineffective** | Tasklet_hi still runs on CPU2; isolcpus needed |
| 6B: Steady-state SSH gaps | **Investigated + Mitigated** | tcp_syn_retries=2 (7 s max gap); counterpoll 5 s |
| 7: Fan control to BMC | Future | Disable thermalctld fan control; BMC already owns it |
| 8: Kernel hwmon for i2c-0 | Future | SMBus I801 sensors; zero CP2112 impact |

**Overall result after Phases 1–3 + 6B mitigations:** IRQ 18 reduced from ~800/sec to ~69/sec (11.6×).
Steady-state SSH gaps reduced from ~33 s to ≤7 s max (tcp_syn_retries=2).
Boot-time BCM ASIC init gap (~136 s) remains; handled by test runner retry logic.

---

## Refactoring Plan

### Phase 1 — DONE: Fix `get_change_event` (33→2 bulk reads)

**File:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`

Replace the per-port GPIO sysfs loop with a direct bulk read of the two PCA9535
input registers using smbus2. This matches the ONL pattern exactly.

```python
# Target architecture for get_change_event()
import smbus2, time

_PRESENCE_BUS_ADDRS = [
    (36, 0x22, 0),   # ports  0-7:  PCA9535 INPUT0
    (36, 0x22, 1),   # ports  8-15: PCA9535 INPUT1
    (37, 0x23, 0),   # ports 16-23: PCA9535 INPUT0
    (37, 0x23, 1),   # ports 24-31: PCA9535 INPUT1
]

def _read_presence_bitmap():
    """Read all 32 port presence bits in 4 I2C reads. Returns 32-bit int."""
    result = 0
    for group, (bus, addr, reg) in enumerate(_PRESENCE_BUS_ADDRS):
        byte = smbus2.SMBus(bus).read_byte_data(addr, reg)
        # PCA9535 PRESENT# is active-low; flip bits. ONL XOR-1 interleave corrected.
        result |= ((~byte) & 0xFF) << (group * 8)
    return result

def get_change_event(self, timeout=0):
    """Single bulk read per poll cycle — 4 I2C reads for all 32 ports."""
    deadline = time.monotonic() + (timeout / 1000.0 if timeout else 0)
    while True:
        bitmap = _read_presence_bitmap()
        events = {}
        for port in range(32):
            present = bool(bitmap & (1 << port))
            prev = self._prev_presence.get(port)
            if prev != present:
                events[str(port + 1)] = '1' if present else '0'
                self._prev_presence[port] = present
        if events:
            return True, {'sfp': events}
        if not timeout or time.monotonic() >= deadline:
            return True, {'sfp': {}}
        time.sleep(1.0)   # One read per second, not 10
```

**Expected IRQ 18 reduction from presence polling:** 330/sec → **4/sec** (82× improvement)

**Implementation notes:**
- Must validate bit ordering against ONL's `onlp_sfpi_reg_val_to_port_sequence()` logic
  (the XOR-1 interleave in sfpi.c must be reproduced)
- Keep the GPIO sysfs path in `sfp.get_presence()` for individual on-demand queries
- SMBus context should be opened once at init and kept open, not per-read
- Handle smbus2 errors gracefully (bus contention during mux operations)

### Phase 2 — DONE: Increase psud Polling Interval

**File:** In-container at `/usr/local/bin/psud` (upstream SONiC), or override via platform.

PSU status changes on a human timescale (PSU insertion/removal/failure). Polling at
3 seconds is 10× more frequent than necessary.

```python
# Current
PSU_INFO_UPDATE_PERIOD_SECS = 3

# Target — override in platform or patch
PSU_INFO_UPDATE_PERIOD_SECS = 30
```

The SONiC way to do this without patching upstream: create a platform override in
`device/accton/x86_64-accton_wedge100s_32x-r0/` that passes `--period 30` to psud
via a supervisor config override.

**Expected IRQ 18 reduction from PSU polling:** 0.67/sec → **0.07/sec**

### Phase 3 — DONE: Persistent smbus2 Handles + SMBus Bus Object Pool

Every individual GPIO sysfs read and smbus2 call currently opens and closes the I2C
bus file descriptor. This generates additional USB HID setup/teardown transactions.

Create a module-level `_SMBusPool` that:
- Opens each bus once at module import
- Keeps file descriptors open
- Provides thread-safe access via `threading.Lock()`

```python
# platform_smbus.py — new shared module
import smbus2, threading

_pool = {}
_lock = threading.Lock()

def get_bus(bus_num):
    with _lock:
        if bus_num not in _pool:
            _pool[bus_num] = smbus2.SMBus(bus_num)
        return _pool[bus_num]
```

### Phase 4 — INFEASIBLE: GPIO Edge Detection via inotify/select

**Result:** `dmesg | grep pca953x` shows `pca953x 36-0022: using no AI` and
`pca953x 37-0023: using no AI` ("no AI" = no interrupt assigned). The PCA9535 INT#
lines are not wired to any host CPU GPIO on the Wedge 100S-32X. Edge detection is not
possible without hardware modification.

### Phase 5 — SSH Boot-Gap Root Cause Analysis

**Findings:** SSH blackout gaps of 136 s, 65 s, and 148 s observed after fresh reboot.
Serial console responsive during gaps. Root cause identified as BCM ASIC IRQ 16 burst
during syncd cold-start.

Full details: `tests/notes/ssh-responsiveness-2026-03-10.md`

**Summary:**
- IRQ 16 (BCM56960 ASIC) fires at ~**2,556/sec during init** vs 104/sec steady state (25×)
- Fires exclusively on CPU1; BCM tasklets (HI softirq) saturate CPU2
- NET_RX/NET_TX softirqs starved → TCP SYN-ACKs not delivered → SSH connections timeout
- Gap durations match TCP SYN retry timeout cycles (63 s per cycle with tcp_syn_retries=6)
- rvtysh / Monit are NOT the cause (rvtysh completes in 180 ms; Monit starts 3 s INTO gap 2)

**Mitigation options:**
1. **IRQ affinity (Phase 6, tested)**: Pinning IRQ 16 to CPU3 does NOT reduce CPU2 HI softirq load
   because BCM `tasklet_hi_schedule()` picks CPU2 independently of where the ISR ran.
   Effective fix requires `isolcpus=3` kernel boot parameter (not practical for production image).
2. **Test runner retry logic (already done)**: `retries=5, retry_delay=10, connect_timeout=30`
3. **Accept the boot window**: Gaps are inherent to BCM SDK initialization; steady state fine

### Phase 6 — TESTED, INEFFECTIVE: IRQ 16 CPU Affinity Tuning

**Result:** Pinning IRQ 16 to CPU3 (`echo 8 > /proc/irq/16/smp_affinity`) successfully
moves the BCM ISR to CPU3 — `/proc/interrupts` confirms counts accumulate on CPU3.
However, `/proc/softirqs` HI column shows CPU2 still at 175,105 vs ~350 on other CPUs.

**Root reason:** The BCM ISR calls `tasklet_hi_schedule()` which queues the BCM tasklet
into the kernel's HI softirq queue. The `tasklet_hi_action` softirq handler then runs on
whichever CPU raises the softirq next — it is NOT tied to the IRQ's CPU. On this 4-core
SMP system, the scheduler consistently selects CPU2 for BCM tasklet execution regardless
of which CPU handled IRQ 16.

**Effective fix would require:** `isolcpus=3` kernel boot parameter (dedicate CPU3 to
BCM processing and prevent scheduler from running other tasks there). This would require
GRUB configuration change and is not practical for a production SONiC image.

**No change made to platform code.** IRQ 16 affinity left at default (0–3).

**Remaining mitigation:** The SSH test runner already implements retry logic
(`retries=5, retry_delay=10, connect_timeout=30`) which handles the boot-time gap.
Steady-state SSH (>10 min post-boot) is fully responsive after Phases 1–3.

### Phase 6B — Steady-State SSH Gaps: Root Cause and Mitigation (2026-03-10)

**New finding:** SSH gaps of ~33 s occur periodically in **steady state**, hours after boot.
Recurring every ~60 s (xcvrd DOM polling cycle). Same IRQ 16 mechanism; different trigger.

**Trigger chain:**
```
xcvrd DOM poll cycle (60 s period)
  Phase 1: EEPROM reads for 9 transceivers → IRQ 18 spike 391/sec for 2 s
  Phase 2 (~30 s later): DOM data update → writes 9 entries to Redis
    → orchagent receives 5+ simultaneous notifications (confirmed: "message repeated 5 times")
      → orchagent makes 5+ rapid SAI calls to syncd
        → BCM SDK activity spikes to 4,200-4,400 IRQ 16/sec (30× steady-state baseline)
          → BCM ISR (~100µs/call) consumes ~44% of one CPU in non-preemptable irq context
            → NET_TX softirq starved → SYN-ACK not sent → new SSH connections fail
```

**Hardware evidence (2026-03-10):**
- syslog: `orchagent [message repeated 5 times]` at 03:16:26, `orchagent` at 03:16:42
- IRQ 16 peaks at **4,200-4,419/sec** at 03:16:41–45 and 03:55:42-46, 03:56:42 (repeated)
- Journal has zero entries from 03:16:20–03:17:01 during IRQ storm (system too busy to log)
- IRQ 18 spike (xcvrd EEPROM) at 03:16:11–13 → ~31 s delay → IRQ 16 spike at 03:16:41–45
- xcvrd 60 s cycle confirmed: EEPROM at 03:56:11, IRQ 16 spike at 03:56:42 (31 s later)
- HI softirq on CPU2 stays at **26-32/sec during spikes** — mechanism is ISR time, NOT HI backlog
- Persistent SSH sessions survive the spike; only NEW connection SYN handshakes fail
- Ports with transceivers: Ethernet4/20/36/52/68/84/108/112/116 (9 ports)

**Mitigations applied on hardware (persistent):**

1. **`tcp_syn_retries=2`** — caps SSH gap duration from 63 s to 7 s max:
   ```bash
   echo "net.ipv4.tcp_syn_retries=2" > /etc/sysctl.d/99-wedge100s-ssh.conf
   ```
   Rationale: The IRQ storm lasts 3–5 s. With tcp_syn_retries=6, TCP backoff (1+2+4... s)
   extends the observable gap to 33+ s. With retries=2, client gets SYN-ACK within 7 s.

2. **PORT_STAT and RIF_STAT counterpoll → 5 s** (was 1 s default):
   ```bash
   counterpoll port interval 5000; counterpoll rif interval 5000
   ```
   Confirmed in CONFIG_DB. Does NOT reduce IRQ 16 baseline (still ~120/sec — BCM internal
   timer, not driven by polling frequency). Reduces continuous BCM SAI call pressure.

**Counterpoll baseline IRQ test:**
- Before: 104/sec baseline (with PORT_STAT at 1 s)
- After: 120/sec baseline (with PORT_STAT at 5 s) — essentially unchanged
- Conclusion: baseline IRQ 16 is BCM SDK internal timer, independent of SAI call rate

Full investigation notes: `tests/notes/ssh-responsiveness-2026-03-10.md` (section: Steady-State)

### Phase 7 — Medium Term: Fan Control Delegation to BMC

The BMC already runs its own PID-controlled fan management based on its own thermal
sensors. SONiC's thermalctld issues `set_fan_speed.sh` commands that compete with
the BMC's native control loop.

**Recommendation:** Disable thermalctld's **fan control** while retaining its thermal
monitoring and alerting roles.

Options:
1. Set `"skip_thermalctld": true` in `pmon_daemon_control.json` and rely entirely on BMC
   (simplest, but loses SONiC thermal event logging)
2. Create a custom `ThermalManager` subclass that reports temperatures to SONiC but
   returns `False` from fan control methods (BMC handles speed)
3. Extend thermalctld poll interval from 60s to 300s for thermal reporting only

The BMC fan control is already hardware-proven in ONL deployments of this platform.

### Phase 8 — Medium Term: Kernel hwmon Drivers for On-board Sensors

Temperature sensors on i2c-0 (SMBus I801) and i2c-1 (CP2112):

**i2c-0 (SMBus I801 — NO CP2112, zero IRQ 18 impact):**
- 0x44: unknown device (voltage monitor?)
- 0x48: ADS1015 ADC (misidentified? may be LM75/TMP75)

**If any i2c-0 temperature sensors are accessible as lm75:**
```bash
echo lm75 0x48 > /sys/bus/i2c/devices/i2c-0/new_device
# Creates /sys/bus/i2c/devices/0-0048/hwmon/*/temp1_input
```

This offloads temperature reads from userspace (Python smbus2 → USB) to kernel hwmon
(kernel driver → smbus → i801, using the SMBus I801 which does NOT go through CP2112).

### Phase 9 — Long Term: C Extension Module for Platform HAL

The deepest performance fix is replacing Python pmon daemons with a C extension
(or compiled Rust/Go) that:
- Opens all I2C buses once at startup
- Issues bulk reads using `I2C_RDWR` ioctl (atomic multi-message transfers where
  supported) rather than per-register smbus2 calls
- Caches presence and status with a configurable TTL
- Serves SONiC platform API calls from cache, only refreshing from hardware at interval

This matches the ONL ONLP architecture: a C library called on-demand with internal
caching, rather than multiple Python daemons polling independently.

**Feasibility assessment:**
- SONiC platform API is Python; a C extension module wrapping the HAL is the interface
- Alternatively: a background C daemon exposing a Unix socket, with a thin Python
  wrapper that reads from the socket (similar to how sonic_ax_impl works)

---

## Implementation Priority and Expected Impact

| Phase | Status | Effort | IRQ Impact | Risk |
|---|---|---|---|---|
| 1: Bulk presence read | **DONE** | — | IRQ 18: 330/sec → 4/sec (**82×**) | — |
| 2: psud interval 3s→30s | **DONE** | — | IRQ 18: −10× | — |
| 3: Persistent SMBus handles | **DONE** | — | IRQ 18: −2× | — |
| 4: GPIO edge detection | **Infeasible** | — | N/A (INT# not wired) | — |
| 5: SSH gap root-cause analysis | **DONE** | — | BCM IRQ 16 burst 2,556/sec during init | — |
| 6: IRQ 16 affinity to CPU3 | **Tested/Ineffective** | — | tasklet_hi still on CPU2; isolcpus needed | — |
| 6B: Steady-state SSH gaps | **DONE** | — | tcp_syn_retries=2 caps gap at 7 s; counterpoll 5 s | — |
| 7: Fan control to BMC | Future | 4 hours | Minimal IRQ18, major ttyACM relief | Low |
| 8: Kernel hwmon for i2c-0 | Future | 4 hours | Minimal (i2c-0 is SMBus I801, not CP2112) | Low |
| 9: C extension HAL | Future | 1–2 weeks | Additional 2–5× | High |

**Achieved after Phases 1–3 + 6B:** IRQ 18 ~800/sec → ~69/sec (11.6×). Steady-state SSH gaps
capped at ≤7 s (tcp_syn_retries=2 + counterpoll 5 s). Boot-time gap (136 s) is a BCM SDK artifact
handled by test runner retry logic. No userspace fix for boot gap exists.

---

## ONL Architecture Reference

ONL's ONLP for Wedge 100S-32X makes these choices:

1. **SFP presence**: 4 bulk I2C reads (one per PCA9535 register) → 32-bit bitmap
   Called by the ONLP server process on-demand or at a fixed slow interval.

2. **Fan/PSU telemetry**: BMC TTY (`/dev/ttyACM0`) with full login per command.
   Acceptable because ONL's ONLP daemon calls this on-demand (operator query or
   slow monitoring loop), not in a 3-second polling loop.

3. **Temperature**: lm75 hwmon sysfs `/sys/bus/i2c/drivers/lm75/<device>/temp1_input`
   Kernel driver handles all caching and i2c transactions; userspace just reads sysfs.

4. **No background daemons**: ONLP is a library, not a daemon. Applications call it
   when they need data. There are no independent polling loops competing for I2C.

The key ONL lesson for SONiC: **batch reads and avoid per-port I2C transactions**.

---

## Testing Plan

After each phase:
1. Measure `cat /proc/interrupts | grep "linux-kernel-bde\|i801_smbus\|ehci_hcd"`
   — compare IRQ 18 rate before/after
2. Measure `cat /proc/softirqs | grep HI` on CPU2 — should decrease proportionally
3. Run `tests/run_tests.py` stage_01 through stage_14 — all must pass
4. Verify SSH session responsiveness: measure latency of `date` command over 60s period
5. Verify QSFP presence events fire correctly on module insertion (most critical
   correctness requirement for Phase 1)

---

## Files Modified / To Modify

| File | Phase | Change |
|---|---|---|
| `platform/.../sonic_platform/chassis.py` | 1 ✓ | Rewrote `get_change_event()` with bulk PCA9535 reads |
| `platform/.../sonic_platform/chassis.py` | 3 ✓ | Uses platform_smbus pool |
| `platform/.../sonic_platform/psu.py` | 3 ✓ | CPLD reads via platform_smbus, not subprocess |
| New: `platform/.../sonic_platform/platform_smbus.py` | 3 ✓ | Persistent SMBus pool, force=True |
| `debian/sonic-platform-accton-wedge100s-32x.postinst` | 2 ✓ | Patch psud 3→30 s; force-reinstall wheel; restart xcvrd/psud |
| `src/sonic-platform-daemons/sonic-psud/scripts/psud` | 2 ✓ | PSU_INFO_UPDATE_PERIOD_SECS 3→30 |
| `platform/.../utils/accton_wedge100s_util.py` | 8 | Add lm75 driver registration |
| `device/.../pmon_daemon_control.json` | 7 | Optionally disable thermalctld fan control |

---

## Notes Files

| File | Contents |
|---|---|
| `tests/notes/irq18-refactor-phases1-2.md` | Hardware measurements, bit ordering, Phase 4 infeasibility |
| `tests/notes/ssh-responsiveness-2026-03-10.md` | SSH boot-gap root cause (BCM IRQ 16 burst) |

---

## Risk Mitigation

**Phase 1 bit ordering risk**: The ONL PCA9535 bit order uses an XOR-1 interleave
(`onlp_sfpi_reg_val_to_port_sequence` in sfpi.c). The bulk read must reproduce this
exactly or port presence will map to wrong ports. Test with a known-present QSFP in
a specific port before and after.

**Phase 1 bus contention**: Direct smbus2 reads on i2c-36/37 while the gpio-pca953x
kernel driver also owns those buses could cause contention. Options:
- Unbind gpio-pca953x driver for the presence chips (use smbus2 exclusively)
- OR keep using GPIO sysfs but fix the loop: read one GPIO per chip instead of one per port
  (the pca953x driver caches INPUT register; triggering one read refreshes all 16 ports on that chip)

**Preferred low-risk Phase 1 variant** (keep gpio-pca953x driver):
```python
# Read one GPIO per chip to refresh the pca953x cache, then read all GPIOs from cache
# OR: use GPIO_GET_LINEVALUES_IOCTL on /dev/gpiochipN for bulk atomic read
import gpiod
chip0 = gpiod.Chip('/dev/gpiochip2')  # i2c-36/0x22, ports 0-15
chip1 = gpiod.Chip('/dev/gpiochip3')  # i2c-37/0x23, ports 16-31
lines0 = chip0.get_lines(list(range(16)))
lines1 = chip1.get_lines(list(range(16)))
# One ioctl per chip = 2 I2C reads, not 32 GPIO sysfs reads
values = lines0.get_values() + lines1.get_values()
```
Using `python3-gpiod` / `libgpiod` gives one ioctl per chip (bulk) and avoids
sysfs per-pin overhead entirely.
