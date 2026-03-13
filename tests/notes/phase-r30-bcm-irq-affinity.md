# Phase R30 — BCM IRQ Affinity + GRUB Kernel Args

## Root Cause

BCM56960 ASIC fires ~150 HW interrupts/second on IRQ 16 (`linux-kernel-bde`).
With default SMP affinity (all CPUs, mask `f`), the kernel's HI softirq queue
saturates a randomly-chosen CPU, creating 15–30 s windows where sshd cannot
accept new TCP connections.

## Evidence

- `/proc/interrupts` line 16: `IO-APIC 16-fasteoi linux-kernel-bde`
- Before fix: `ssh` latency first cold connect = **65 s**
- After `echo 1 > /proc/irq/16/smp_affinity`: **0.25 s** consistently

## Files Changed

### `device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf`
Added:
```
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"
```
Matches ONL `platform-config.yml` kernel args for this platform.
Effective for newly installed images (GRUB boot parameter).

### `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py`
Added `_pin_bcm_irq()` function called from `do_install()`:
```python
def _pin_bcm_irq():
    with open('/proc/irq/16/smp_affinity', 'w') as f:
        f.write('1\n')
```
Pins IRQ 16 to CPU 0, leaving CPUs 1–3 free for sshd/userspace.
Runs at every `wedge100s-platform-init.service` start — covers running systems
that were not re-installed with the new GRUB args.

## Verification (verified on hardware 2026-03-11)

```bash
# Simulate pre-fix: spread IRQ across all CPUs
sudo sh -c 'echo f > /proc/irq/16/smp_affinity'
cat /proc/irq/16/smp_affinity_list    # → 0-3

# Run platform install (triggers _pin_bcm_irq)
sudo python3 /usr/local/bin/accton_wedge100s_util.py install

# Confirm pinned
cat /proc/irq/16/smp_affinity_list    # → 0
```

## SSH Latency Measurements

| State | SSH latency (cold) |
|---|---|
| Default affinity (all CPUs, mask f) | 65 s (first connection) |
| IRQ 16 pinned to CPU 0 (mask 1) | 0.25 s |

## ONL Reference

ONL `platform-config.yml` for wedge100s-32x:
```yaml
kernel-args: nopat intel_iommu=off noapic
```
Source: `/export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/`
