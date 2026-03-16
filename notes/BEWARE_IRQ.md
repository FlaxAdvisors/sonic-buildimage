# BEWARE_IRQ.md — Accton Wedge 100S-32X IRQ / SSH Responsiveness Pitfalls

**Read this before:** changing polling rates, loading/unloading I2C drivers, adjusting IRQ
affinity, modifying xcvrd DOM intervals, enabling BGP on data-plane ports, or tuning TCP
kernel parameters on this platform.

---

## 1. IRQ 18 Overload — I2C Presence Polling

### Danger

The original `get_change_event()` in `chassis.py` polled 33 GPIO sysfs pins in a tight
loop (0.1 s sleep × 33 reads = **330 I2C reads/second**). Every read crossed the CP2112
USB-HID bridge → `ehci_hcd:usb1` → IRQ 18. The result was **~800 IRQ 18/second**,
saturating CPU2 with HI softirqs and causing 15–30 s windows where sshd would not accept
connections.

The fix is **bulk PCA9535 register reads**: 4 I2C reads (one per register) cover all 32
ports, replacing the 33-read per-poll-cycle loop. Sleep interval increased to 1.0 s. The
SMBus handles are held open for the process lifetime in `platform_smbus.py`.

### Before / After

| Metric | Before | After |
|---|---|---|
| IRQ 18/sec | ~800 | ~69 |
| I2C reads/sec (presence) | 330 | ~8 |
| HI softirq CPU2 | CPU-saturating | ~38/s |

```bash
# Verify current IRQ 18 rate on hardware (watch for 3 s):
a=$(grep -E "ehci_hcd|i801_smbus" /proc/interrupts | awk '{sum+=$2} END{print sum}')
sleep 3
b=$(grep -E "ehci_hcd|i801_smbus" /proc/interrupts | awk '{sum+=$2} END{print sum}')
echo "IRQ 18/s: $(( (b-a)/3 ))"
# Expected: ~69. If >200 with pmon running → presence polling has regressed.
```

### What drives the residual 69/sec

After the fix, 66 IRQ/s above the 3/s USB-SOF baseline come from:
- xcvrd presence polls (8 reads/sec via `platform_smbus`)
- thermalctld BMC ttyACM0 traffic
- ledd polling
- psud PSU CPLD reads (30 s interval, now patched from 3 s)

If psud is reverted to its upstream default of `PSU_INFO_UPDATE_PERIOD_SECS = 3`, the
rate will jump by ~10/s (minor but measurable). The far more dangerous regression is
reverting `get_change_event()` to per-port GPIO sysfs reads.

### smbus2 force=True is required

The `gpio-pca953x` kernel driver holds the I2C device address for i2c-36/0x22 and
i2c-37/0x23. Direct smbus2 reads require `force=True` (uses `I2C_SLAVE_FORCE` ioctl).
Without it, every read returns `EBUSY`.

```bash
# Confirm driver is bound (expected — do not unbind):
ls /sys/bus/i2c/drivers/gpio-pca953x/
# → 36-0022  37-0023
```

### PCA9535 INT# is not wired — edge detection is impossible

`dmesg | grep pca953x` shows `pca953x 36-0022: using no AI` and
`pca953x 37-0023: using no AI`. Neither PCA9535 INT# pin is connected to any host CPU
GPIO. There is no interrupt-driven presence detection path. The 1 s polling loop is the
minimum achievable without hardware modification.

---

## 2. BCM IRQ Affinity — IRQ Number Is Not Stable

### Danger: the IRQ number changes between kernel configs

Early sessions observed BCM (`linux-kernel-bde`) on **IRQ 16** (IO-APIC routing, kernel
without `noapic`). After `noapic` was added to the kernel command line (matching ONL's
`platform-config.yml`), BCM moved to **IRQ 11** on XT-PIC. IRQ 16 no longer exists.
Any code that hardcodes `/proc/irq/16/smp_affinity` silently fails:

```
WARNING: Could not pin BCM IRQ 16 to CPU 0: [Errno 2] No such file or directory:
'/proc/irq/16/smp_affinity'
```

### Always discover the IRQ dynamically

```python
# Correct pattern (from accton_wedge100s_util.py):
for line in open('/proc/interrupts'):
    if 'linux-kernel-bde' in line:
        irq = int(line.split(':')[0].strip())
        break
```

### XT-PIC constraint with noapic

With `noapic` in effect (current production kernel args), BCM is on **XT-PIC IRQ 11**,
hardwired to **CPU0**. `/proc/irq/11/smp_affinity` does not exist and cannot be written.
IRQ affinity tuning for BCM itself is impossible on this kernel configuration.

```bash
# Confirm BCM IRQ and type:
grep linux-kernel-bde /proc/interrupts
# Expected with noapic: "  11:  ...  XT-PIC      linux-kernel-bde"
# If IO-APIC:           "  16:  ...  IO-APIC 16-fasteoi  linux-kernel-bde"
```

### What CAN be moved: eth0-TxRx-0

`eth0` (management NIC) uses PCI-MSI-X interrupts that are movable regardless of
`noapic`. Moving `eth0-TxRx-0` off CPU0 decouples management-plane RX from BCM's
CPU0 interrupt load:

```bash
# Discover eth0-TxRx-0 IRQ dynamically:
grep 'eth0-TxRx-0' /proc/interrupts | awk -F: '{print $1}' | tr -d ' '
# Then pin to CPU2 (bitmask 0x4):
echo 4 | sudo tee /proc/irq/<N>/smp_affinity

# Verify:
cat /proc/irq/<N>/smp_affinity   # → 4
```

This is performed at boot by `wedge100s-platform-init.service` via `_pin_bcm_irq()` in
`accton_wedge100s_util.py`. The function must use dynamic IRQ discovery, not hardcoded
numbers.

### Why IRQ affinity alone cannot cure the BCM storm

Tested: pinning BCM IRQ to CPU3 (`echo 8 > /proc/irq/16/smp_affinity`) moves the ISR
but does **not** move BCM tasklets. The BCM ISR calls `tasklet_hi_schedule()`, which
queues work into the kernel HI softirq queue. The softirq handler runs on whichever CPU
picks it up next — consistently CPU2 on this 4-core SMP system, regardless of which CPU
handled the ISR. Moving the IRQ does not help.

The only kernel-level fix would be `isolcpus=3`, which is not practical for a production
SONiC image and was not pursued.

---

## 3. Boot Gap / TCP Black Hole

### What happens

Within the first ~10 minutes after boot, there are three SSH blackout windows:

| Gap | Duration | Cause |
|---|---|---|
| Boot gap | ~136 s | syncd cold-start programs BCM56960 ASIC tables |
| BGP gap 1 | ~65 s | orchagent programs first BGP route batch into ASIC |
| BGP gap 2 | ~148 s | orchagent programs second BGP route batch into ASIC |

The serial console remains **fully responsive** during these gaps — the system is not
frozen. This is a pure TCP-level black hole: BCM IRQ bursts starve NET_TX/NET_RX
softirqs, so SYN-ACK packets are not delivered and new TCP connections time out.

### IRQ 16 rate during ASIC init (estimated from uptime/count data)

```
Steady-state:   ~104–148 IRQ/s
During ASIC init: ~2,556 IRQ/s   (25× higher for first ~300 s)
```

Gap durations match TCP SYN retry backoff math (`tcp_syn_retries=6`: gaps of 63 s,
126 s). This is an inherent BCM SDK characteristic, not a SONiC or platform bug.

### Mitigation applied

Reduce `tcp_syn_retries` from the default 6 to 2. With retries=2, the TCP backoff
sequence is 1+2+4 = 7 s maximum. Since the BCM storm itself only lasts 3–5 s in steady
state, the client's SYN-ACK arrives on the first or second retry:

```bash
# Applied persistently on hardware:
echo "net.ipv4.tcp_syn_retries=2" | sudo tee /etc/sysctl.d/99-wedge100s-ssh.conf
sudo sysctl -w net.ipv4.tcp_syn_retries=2
```

The boot-time gap (136 s) cannot be shortened — it is the BCM SDK cold-start time.
The correct mitigation is **test runner retry logic**:

```python
# tests/lib/ssh_client.py:
SSHClient.connect(retries=5, retry_delay=10, connect_timeout=30)
```

### Steady-state 60 s periodic gap (xcvrd-triggered)

After boot, a recurring 3–5 s BCM IRQ storm fires every ~60 s. Root cause:

```
xcvrd DOM poll cycle (60 s period)
  Phase 1 (t=0):  EEPROM reads for N present transceivers → IRQ 18 spike ~390/s
  Phase 2 (t+30s): DOM data written to Redis
    → orchagent receives simultaneous Redis notifications
      → rapid SAI calls to syncd → BCM SDK spikes to 4,000–4,400 IRQ/s
        → ISR at ~100µs/call consumes ~44% of one CPU in non-preemptable irq context
          → NET_TX softirq starved → new TCP SYN connections fail
```

Hardware evidence (IRQ rates measured per second):

```
time       i16/s   i18/s   comment
03:56:11    147     337    Phase 1: EEPROM reads (9 ports)
03:56:42   4243      39    Phase 2: BCM spike 31 s later
03:56:43    151      32    Storm over
```

With `tcp_syn_retries=2`, the observable SSH gap drops from ~33 s to ≤7 s. Persistent
SSH sessions survive all spikes; only new SYN handshakes that fall within the 3–5 s
window are affected.

### BGP ARP storm — a separate but similar failure mode

If BGP neighbors are configured on data-plane ports that have physical carrier (even
without a real peer), bgpd's reconnect retry cycle generates ARP requests that are
CPU-trapped by the BCM ASIC:

```
2 ports with physical carrier + BGP neighbors configured
  → bgpd ARP retry every ~10 s per reconnect timer
    → BCM CPU-trap interrupts: 1,878–4,342/s
      → CPU0 softirq saturates → 30 s SSH blackouts
```

Verified by isolation (2026-03-12): admin-down the cable-connected ports → BCM drops
to 145 IRQ/s → 15/15 SSH attempts succeed immediately.

**Current mitigation:** BGP is disabled via `sudo config feature state bgp disabled`.
If BGP is re-enabled, ensure no BGP neighbors are configured on ports with live physical
carrier unless a real BGP peer exists.

---

## 4. IRQ Number Instability

### Summary

The BCM IRQ number is dynamically assigned by the kernel and has changed between builds
on this platform:

| Kernel configuration | BCM IRQ | Type | Movable? |
|---|---|---|---|
| Without `noapic` | 16 | IO-APIC fasteoi | Yes (any CPU) |
| With `noapic` (current) | 11 | XT-PIC | No (CPU0 only) |

**Never hardcode an IRQ number in platform code or scripts.** Always parse
`/proc/interrupts` for the `linux-kernel-bde` label:

```bash
# Shell:
grep linux-kernel-bde /proc/interrupts | cut -d: -f1 | tr -d ' '

# Python:
import re
for line in open('/proc/interrupts'):
    m = re.match(r'\s*(\d+):.*linux-kernel-bde', line)
    if m:
        bcm_irq = int(m.group(1))
```

Similarly, `eth0-TxRx-0` IRQ number (currently 55) must be discovered dynamically —
PCI-MSI-X vector assignment is not guaranteed across reboots or kernel upgrades.

The same principle applies to any IRQ named in a systemd `ExecStart` script or
oneshot service: resolve the name at runtime, not install time.

---

## 5. Dead End: IPMI / REST BMC Access

Investigated as an alternative to the `/dev/ttyACM0` path for BMC sensor reads
(Phase R31, 2026-03-11).

**IPMI is completely absent** on this platform: `modprobe ipmi_si` returns
`No such device`; the BMC does not listen on UDP port 623; this is a Facebook-OpenBMC
build that predates IPMI support.

**The Facebook REST API** (port 8080 over IPv6 link-local on `usb0`) does work and
returns all thermal + fan RPM data in one HTTP call, but adds 1.0–1.4 s latency per
call due to the BMC server invoking `sensors` as a subprocess. Our existing C daemon
(`wedge100s-bmc-poller`) updates `/run/wedge100s/` files every 3 s; Python reads those
files in under 1 ms. REST is not an improvement and cannot replace the TTY path for
fan speed writes (`writable = false` in `/etc/rest.cfg`; no fan speed POST endpoint exists).

Current architecture (C daemon + tmpfs files) remains the correct approach.

---

## Reference

| Source notes | Contents |
|---|---|
| `tests/notes/irq18-refactor-phases1-2.md` | Phase 1–4 measurements, bit ordering, Phase 4 infeasibility |
| `tests/notes/ssh-responsiveness-2026-03-09.md` | BCM IRQ 16 affinity fix and CPU layout |
| `tests/notes/ssh-responsiveness-2026-03-10.md` | Boot-gap root cause; steady-state xcvrd trigger chain; tcp_syn_retries |
| `tests/notes/ssh-responsiveness-2026-03-12.md` | IRQ number instability; BGP ARP storm; XT-PIC/noapic finding |
| `tests/notes/REFACTOR_HWSUPPORT.md` | Full phase status table and implementation details |
| `tests/notes/phase-r30-bcm-irq-affinity.md` | R30 initial BCM IRQ pin attempt (later corrected) |
| `tests/notes/phase-r31-ipmi-rest-investigation.md` | IPMI/REST dead-end investigation |
