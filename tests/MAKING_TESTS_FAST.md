# Making Tests Fast on the Wedge 100S

## The Problem

On a clean boot, `run_tests.py` ran 238 tests in **2217 seconds (37 minutes)**.
Of that, **2002 seconds** (90%) was unexplained stall time — not real test logic.

The worst offenders from `tests/timing.log`:

| Test | Time | Root cause |
|---|---|---|
| `test_supported_speeds_in_state_db` | 355s | exec_command stall × 6 xcvrd cycles |
| `test_psu_pgood_implies_present` | 228s | exec_command stall × 4 reads |
| `test_portchannel_rx_counters_increment` | 304s | intentional 65s sleep + stalls |
| `test_autoneg_disable_accepted` | 121s | exec_command stall × 2 cycles |
| `test_portchannel_tx_counters_increment` | 118s | exec_command stall + LLDP miss |
| `test_fec_rs_accepted` | 115s | exec_command stall × 2 cycles |
| `test_teamd_feature_enabled` | 56s | exec_command stall × 1 cycle |

Without stalls, all 238 tests should complete in **under 6 minutes**.

---

## Root Cause 1: BCM ASIC IRQ Storms Stall SSH Channel Opens

### The hardware

The Broadcom Tomahawk ASIC driver (`linux-kernel-bde`) fires on IRQ 11 (XT-PIC, fixed
to CPU0) at **200–400 interrupts/second** during normal operation. The CP2112
USB-to-I2C bridge (`i801_smbus` + `ehci_hcd:usb1`) fires on IRQ 5 (XT-PIC, CPU0) at
**~40 interrupts/second** during xcvrd DOM polling.

```
  5:     107717          0          0          0    XT-PIC  i801_smbus, ehci_hcd:usb1
 11:     590897          0          0          0    XT-PIC  linux-kernel-bde
```

Both XT-PIC IRQs are **permanently bound to CPU0** and cannot be moved via
`smp_affinity` (XT-PIC is a legacy single-CPU interrupt controller, not PCI-MSI).

### The xcvrd DOM polling cycle

`xcvrd` (the transceiver daemon) polls all 32 QSFP ports for DOM data (temperature,
voltage, optical power, etc.) every **~57 seconds** via the CP2112 USB-HID bridge.
Each poll consumes ~40 I2C interrupts/second for ~15 seconds:

```
polling active (15s): 40 irq/s × 15s = 600 IRQs to CPU0
quiescent (42s): near-zero
total cycle: ~57s
```

During the 15-second burst, CPU0 runs nearly continuously in interrupt context.
High-frequency IPIs (inter-processor interrupts) from CPU0's TLB shootdowns and RCU
callbacks add scheduling jitter to all other CPUs — including CPU2 where sshd lives.

### How SSH channel opens were stalling

paramiko's `SSHClient.exec_command(cmd, timeout=None)` internally calls
`transport.open_session(timeout=None)`. With `timeout=None`, the channel-open
negotiation waits **indefinitely** for sshd to acknowledge the new channel.

When xcvrd's DOM poll burst lands, sshd's session process on CPU2 experiences enough
IPI jitter that it delays acknowledging new SSH channel opens by **up to 57 seconds
per xcvrd cycle**. A test that makes 4 SSH calls during 4 consecutive xcvrd stalls
would block for 4 × 57s = **228 seconds** — all passing, all correct, all inexplicably
slow.

The stall durations observed were integer multiples of 57s:
- 57s = 1 xcvrd cycle
- 114–120s = 2 cycles
- 228s = 4 cycles
- 355s = 6 cycles

### The fix

**`tests/lib/ssh_client.py`**: Change `exec_command(cmd, timeout=None)` →
`exec_command(cmd, timeout=timeout)` on both the initial call and the reconnect retry.

```python
# Before (stalls for entire xcvrd cycle):
stdin, stdout, stderr = self._client.exec_command(cmd, timeout=None)

# After (times out at command's own timeout, triggers reconnect+retry):
stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
```

When a channel open stalls for `timeout` seconds (10–15s for most test commands),
paramiko raises `SSHException`. The existing `except` clause catches it, reconnects
(fresh transport), and retries. By the time the reconnect completes (~5s), the xcvrd
burst has usually subsided and the retry opens instantly.

**Net effect**: A 115s stall becomes a ~16s reconnect-and-retry cycle.

---

## Root Cause 2: paramiko `recv_exit_status()` Hangs Indefinitely

Before the channel-open fix was identified, `run()` used the standard paramiko pattern:

```python
exit_code = stdout.channel.recv_exit_status()  # no timeout — hangs forever
```

`recv_exit_status()` blocks until the remote command exits AND the channel close
message is received. During BCM IRQ storms, the TCP ACK for the channel-close can be
delayed indefinitely if the switch's network path is saturated.

### The fix

Replace with `status_event.wait(timeout=timeout)`:

```python
if not stdout.channel.status_event.wait(timeout=timeout):
    raise TimeoutError(f"Command timed out after {timeout}s: {cmd!r}")
exit_code = stdout.channel.exit_status
```

`status_event` is set as soon as the exit status arrives — it does not wait for
channel teardown, so it returns promptly even during network jitter.

---

## Root Cause 3: SFTP Upload Hangs During IRQ Storms

`run_python()` originally uploaded test scripts via SFTP:

```python
sftp = self._client.open_sftp()
sftp.file(path).write(content)  # no timeout — hangs forever
```

paramiko's SFTP `open_sftp()` and `sftp.file().write()` have **no timeout**
mechanism. They block on the transport socket directly, bypassing all timeout
parameters.

### The fix

Replace SFTP upload with a base64-piped shell command that goes through the
timeout-protected `run()` path:

```python
encoded = base64.b64encode(code.encode()).decode()
self.run(
    f"echo {encoded} | base64 -d | sudo tee {script_path} > /dev/null",
    timeout=15,
)
```

---

## Root Cause 4: BCM IRQ Storm Saturates CPU0, Disrupts SSH TCP

During peak xcvrd DOM polling, CPU0 processes ~40 IRQs/second for the CP2112 bridge.
When the BCM ASIC driver fires simultaneously (200–400 IRQs/s), CPU0 generates
high-frequency IPIs to broadcast TLB flushes and RCU quiescent states. This
occasionally causes the switch's Linux TCP/IP stack to delay ACKing or forwarding
packets, causing TCP retransmits on the management channel.

The default Linux TCP retransmit timers are extremely conservative:
- `tcp_syn_retries` default: 6 (up to **127 seconds** before giving up a new
  connection)
- `tcp_retries2` default: 15 (up to **924 seconds** before killing a stalled
  established connection)

If SSH drops and `connect()` retries, each attempt times out after 30s ×
(retries − 1) + delays = up to 190s.

### The fix (`tools/tasks/system_tuning.py`)

```
net.ipv4.tcp_syn_retries=2    # reconnect after storm in ≤7s (not ≤127s)
net.ipv4.tcp_retries2=5       # give up stalled connection after ~6s (not ~924s)
```

Written to `/etc/sysctl.d/99-wedge100s-ssh.conf`, applied idempotently via
`tools/deploy.py --task system_tuning`.

---

## Root Cause 5: Management Plane CPU Contention

eth0 IRQ affinity was set by `irqbalance` to CPU0 — the same CPU saturated by BCM
and xcvrd storms. Every SSH/TCP packet had to fight for CPU0 attention.

### The fix (`tools/tasks/cpu_affinity.py`)

Pin the management plane to CPU2 (away from BCM/I2C IRQ storms on CPU0):

| Resource | Before | After |
|---|---|---|
| eth0 IRQs 54–58 (PCI-MSIX) | Distributed by irqbalance | Pinned to CPU2 via `smp_affinity=4` |
| eth0 RPS/XPS queues | Any CPU | CPU2 |
| `sshd` (CPUAffinity) | Any CPU | CPU2 |
| `networking.service` (CPUAffinity) | Any CPU | CPU2 |
| `wedge100s-mgmt-affinity.service` | — | Oneshot boot script applying above |

Applied idempotently via `tools/deploy.py --task cpu_affinity`.

**Note**: BCM IRQ 11 is XT-PIC, permanently fixed to CPU0. `smp_affinity` writes on
XT-PIC IRQs silently fail (correct — the hardware doesn't support it). BCM is already
isolated from CPU2 by virtue of being stuck on CPU0.

---

## Root Cause 6: Zombie `dhclient` Removes Management Route

After `systemctl restart networking`, the old `dhclient` PID is only killed if it
matches the PID recorded in `/run/dhclient.eth0.pid`. If the PID file is stale, the
old dhclient survives as a zombie. When its DHCP lease expires, it runs the `EXPIRE`
handler — removing eth0's IP address and default route from the management VRF.

Result: SSH becomes unreachable for up to 10 minutes until dhclient re-discovers
and re-leases.

A second issue: a `dhclient-enter-hooks.d/noop-renew` script was installed that
contained `exit 0`. Because enter-hooks are **sourced** (not exec'd) by
`dhclient-script`, this `exit 0` exited `dhclient-script` itself on every RENEW,
silently skipping route installation after any networking restart.

### The fix (`tools/tasks/system_tuning.py`)

1. **Remove the noop-renew hook** (was causing silent route loss on every RENEW).

2. **Add `ExecStartPre` to kill zombie dhclient before networking restarts:**
   ```
   [Service]
   ExecStartPre=-/bin/sh -c 'pkill -f "dhclient.*eth0" ; sleep 1 ; true'
   ExecStartPost=-/bin/systemctl restart ssh
   ```
   Written to `/etc/systemd/system/networking.service.d/restart-ssh.conf`.

3. **Restart sshd after networking restarts** via `ExecStartPost` (above) — ensures
   sshd re-binds to the mgmt VRF socket, which is recreated on every networking
   restart.

Applied idempotently via `tools/deploy.py --task system_tuning`.

---

## Correct Workflow Before Running Tests

Always run `tools/deploy.py` after a fresh image install or reboot. It is idempotent:

```bash
cd /export/sonic/sonic-buildimage.claude
tools/deploy.py [--dry-run]
```

Tasks applied (in order):

| Task | What it does |
|---|---|
| `system_tuning` | TCP retransmit timers, networking→ssh drop-in, remove noop-renew hook |
| `cpu_affinity` | Pin eth0/sshd/networking to CPU2; isolate BCM IRQs on CPU0 |
| `mgmt_vrf` | Create management VRF and configure eth0 in it |
| `breakout` | 4×25G breakout on ports 1–4 |
| `portchannel` | PortChannel1 with LACP on Ethernet16+32 |
| `vlans` | VLAN 10 (access ports) and VLAN 999 (PortChannel) |
| `optical` | FEC=rs on connected 100G optical/DAC ports |

---

## Platform-Specific SAI Limitations Discovered During Testing

### Breakout sub-ports have limited counter support

On this Tomahawk SAI, breakout sub-ports (e.g. `Ethernet0` through `Ethernet3`) only
expose two COUNTERS_DB fields:

```
SAI_PORT_STAT_IN_DROPPED_PKTS
SAI_PORT_STAT_OUT_DROPPED_PKTS
```

The full `SAI_PORT_STAT_IF_*` counter set (octets, ucast, errors, discards) is only
available on non-breakout 100G ports such as `Ethernet16`.

**Fix**: `test_counters_key_fields_present` now checks `Ethernet16` instead of
`Ethernet0`.

### LLDP Redis cache is stale after portchannel member operations

`LLDP_ENTRY_TABLE` in Redis can be momentarily cleared when portchannel members are
deleted and re-added. The stage_20 traffic test derives the peer's chassis MAC from
LLDP to install a static ARP entry (required because the EOS PortChannel is L2-only
and won't respond to ARP dynamically). When the Redis cache was empty at setup time,
no static ARP was installed, ping sent 0 TX packets, and `test_portchannel_tx_counters_increment` failed.

**Fix**: `_get_lldp_peer_mac()` now queries `lldpctl -f keyvalue` (real-time lldpd
daemon state) first, falling back to Redis only if lldpctl returns nothing.

---

## SSH Keepalive Strategy

The session-wide SSH connection (one TCP connection, many multiplexed channels) is
configured in `connect()`:

```python
self._client.get_transport().set_keepalive(60)
```

This sends an SSH-level keepalive every 60 seconds. Combined with `tcp_retries2=5`
(TCP gives up stalled connection in ~6s), a dead connection is detected and
triggers reconnect within ~66 seconds.

`connect()` retries up to 5 times with 30s connect timeout and 10s between attempts
(`retries=5, retry_delay=10, connect_timeout=30`). Worst-case reconnect: ~190s if
SSH is completely unreachable. Typical reconnect after a BCM storm: ~5s.

---

## Expected Test Run Times (Post-Fix)

With all tunings applied and `exec_command(timeout=timeout)` fix in place:

| Stage | Tests | Expected |
|---|---|---|
| stage_00 pretest | 9 | < 5s |
| stage_01 eeprom | 10 | < 3s |
| stage_02 system | 10 | < 8s |
| stage_03 platform | 12 | < 10s |
| stage_04–06 thermal/fan/psu | 22 | < 20s |
| stage_07–08 qsfp/led | 16 | < 15s |
| stage_09 cpld | 12 | < 5s |
| stage_10 daemon | 14 | < 15s |
| stage_11 transceiver | 7 | < 25s |
| stage_12 counters | 9 | < 30s |
| stage_13 link | ~10 | < 15s |
| stage_15 autoneg/fec | ~20 | < 60s |
| stage_16 portchannel | ~15 | < 90s |
| stage_17 report | 1 | < 10s |
| stage_19 platform cli | 9 | < 10s |
| stage_20 traffic | 6 | < 150s |
| stage_21 lpmode | ~5 | < 15s |
| stage_nn posttest | 4 | < 5s |
| **Total** | **~238** | **< 10 minutes** |

The largest intentional waits: stage_20 `test_portchannel_rx_counters_increment`
waits 65s for LACP PDUs; stage_20 fixture waits 40s for LACP convergence. These are
necessary and correct.

---

## Monitoring Stalls

`tests/timing.log` records per-test elapsed time. After a run:

```bash
# Show outliers (>= 20s)
awk '$1+0 >= 20' tests/timing.log | sort -rn | head -20

# Show total stall overhead
awk '$1+0 >= 20 {stall += $1} $1+0 < 20 {fast += $1} END {
    printf "Stall: %.0fs  Fast: %.0fs\n", stall, fast
}' tests/timing.log
```

Any test taking > 2× the xcvrd cycle (~57s) without an explicit `time.sleep()` in
its code is hitting the SSH channel-open stall and should be investigated with the
IRQ rate at the time of failure:

```bash
# On the switch: sample IRQ rates
watch -n1 'cat /proc/interrupts | grep -E "bde|smbus|eth0"'
```
