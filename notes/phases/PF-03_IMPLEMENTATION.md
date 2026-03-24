# PF-03 — Platform Init: Implementation

## What Was Built

### `utils/accton_wedge100s_util.py`

Runtime platform init script. Key implementation details:

**Module load sequence (`kos` list)**:
```python
kos = [
    'modprobe i2c_dev',
    'modprobe i2c_i801',
    'modprobe hid_cp2112',
    'modprobe wedge100s_cpld',
]
```
Intentionally absent (Phase 2 architecture): `i2c_mux_pca954x`, `at24`, `optoe`,
`i2c_ismt`, `lm75`, `gpio_pca953x`.

**Device registration (`mknod` list)**:
```python
mknod = [
    'echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device',
]
```
Only the CPLD is registered. All mux-tree devices are owned by `wedge100s-i2c-daemon`.

**`_pin_bcm_irq()`** — dynamically reads `/proc/interrupts` to find:
- `linux-kernel-bde` IRQ (BCM56960 ASIC, hardwired to CPU0, cannot move)
- `eth0-TxRx-0` IRQ (management NIC TX/RX queue)
Sets `eth0-TxRx-0` smp_affinity to CPU2 (bitmask `4`) to isolate management
plane RX from BCM interrupt storms on CPU0.

Root cause documented in code comment: 32 BGP neighbors configured on DOWN ports
cause bgpd to ARP-retry continuously → BCM CPU-traps spike IRQ11 to 5000-6000/s
→ CPU0 softirq saturates → SSH blackouts of 30-50 s.

**`do_uninstall()` safety guard**:
```python
status_out = _sp.run(['docker', 'inspect', '--format={{.State.Status}}', 'pmon'],
                     ...).stdout.strip()
if status_out == 'running':
    print("ABORT: pmon is running ...")
    return 1
```
Prevents the `i2c_del_adapter()` kernel hang.

**`_warmup_qsfp_eeproms()`** — reads 1 byte from each optoe1 eeprom sysfs path
before xcvrd starts. Warms up DAC cable modules that return identifier `0x01`
(GBIC) on first cold read, then correct `0x11` (QSFP28) on subsequent reads.
(Present in Phase 1 path; Phase 2 daemon handles this via retry logic.)

### `service/wedge100s-platform-init.service`

```ini
[Unit]
Description=Accton WEDGE100S-32x Platform initialization service
Before=pmon.service
DefaultDependencies=no

[Service]
Type=oneshot
ExecStart=/usr/bin/accton_wedge100s_util.py install
ExecStop=/usr/bin/accton_wedge100s_util.py clean
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

`DefaultDependencies=no` is important — it allows the service to start early
in the boot sequence before standard dependency ordering kicks in.

### `debian/sonic-platform-accton-wedge100s-32x.postinst`

Key actions (from Phase R28 notes):
1. `depmod` — register new `.ko` with kernel module database
2. `mkdir -p /run/wedge100s`
3. `systemctl enable wedge100s-platform-init.service`
4. `systemctl enable wedge100s-bmc-poller.timer`
5. `systemctl enable wedge100s-i2c-poller.timer`
6. `systemctl start ...` for all three (if not already running)
7. Patch `pmon.sh`: idempotent `sed` to add `--volume /run/wedge100s:/run/wedge100s:ro`
   after the `--device /dev/ttyACM0` line

### GRUB kernel arguments (Phase R30)

`device/accton/x86_64-accton_wedge100s_32x-r0/installer.conf`:
```
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"
```
Matches ONL `r0.yml` kernel args. Effective for freshly installed images.
The `_pin_bcm_irq()` function provides runtime coverage for running systems.

## Hardware-Verified Facts

- `smp_affinity_list` for `eth0-TxRx-0` changes from `0-3` → `2` on
  `sudo python3 accton_wedge100s_util.py install` (verified on hardware 2026-03-11)
- First cold SSH connect: **65 s** before IRQ affinity fix, **0.25 s** after
  (verified on hardware 2026-03-11)
- `wedge100s-platform-init.service` state: `active (exited)` after boot
  (verified on hardware 2026-03-14)
- `pip3 show sonic-platform` shows package installed after dpkg (verified 2026-03-14)

## Remaining Known Gaps

- `_warmup_qsfp_eeproms()` is only called in Phase 1 path (when optoe1 sysfs
  exists). In Phase 2, the i2c-daemon handles EEPROM retry logic internally.
  The warmup function could be removed but is harmless.
- The `postinst` pmon.sh patch is idempotent but relies on the specific line
  ordering in `pmon.sh` not changing across SONiC releases.
- `driver_check()` only tests `hid_cp2112` and `i2c_dev` — it does not verify
  `wedge100s_cpld` is loaded. A more complete check would also test `1-0032/`
  existence, which `device_exist()` does.
