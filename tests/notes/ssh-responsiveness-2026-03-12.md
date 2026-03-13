# SSH Responsiveness Investigation — 2026-03-12

## Symptoms

After deploying a fresh build (branch `wedge100s`, commit `902e49b2a`), SSH pauses
persisted despite R30 adding `noapic` + `_pin_bcm_irq()`.  The platform-init log showed:

```
WARNING: Could not pin BCM IRQ 16 to CPU 0: [Errno 2] No such file or directory:
'/proc/irq/16/smp_affinity'
```

Hypothesis going into this session: `wedge100s-bmc-poller` was correlating with the pauses.

---

## Diagnostic Commands

### 1. Confirm BCM IRQ number and interrupt topology

```bash
ssh admin@192.168.88.12 'cat /proc/interrupts | grep -E "linux-kernel-bde|eth0"'
```

**Output:**
```
 11:   13591266  0  0  0    XT-PIC      linux-kernel-bde
 54:          0  0  0  1  PCI-MSIX-0000:02:00.0   0-edge      eth0
 55:     163628  0  0  0  PCI-MSIX-0000:02:00.0   1-edge      eth0-TxRx-0
 56:          0  30298  0  0  PCI-MSIX-0000:02:00.0   2-edge      eth0-TxRx-1
 57:          0  0  33272  0  PCI-MSIX-0000:02:00.0   3-edge      eth0-TxRx-2
 58:          0  0  0  29549  PCI-MSIX-0000:02:00.0   4-edge      eth0-TxRx-3
```

**Finding:** BCM is on XT-PIC **IRQ 11** (not 16 as R30 assumed). XT-PIC is hardwired to
CPU0 — `/proc/irq/11/smp_affinity` cannot be changed. R30's `_pin_bcm_irq()` was
silently doing nothing because `/proc/irq/16/smp_affinity` does not exist.

### 2. Confirm kernel args (noapic in effect)

```bash
ssh admin@192.168.88.12 'cat /proc/cmdline'
```

`noapic` is present. With `noapic`, the I/O APIC is disabled; XT-PIC (8259A) routes all
legacy interrupts to CPU0. PCI-MSI/MSI-X interrupts (eth0-TxRx-*) still use the local
APIC and can have affinity changed.

### 3. SSH timing test baseline

```bash
for x in {1..30}; do time ssh admin@192.168.88.12 date; sleep 1; done
```

Showed repeated `[10025ms]` entries (ConnectTimeout=10 hit) and `[7xxx ms]` slow connects.

### 4. Isolate bmc-poller as cause (ruled out)

```bash
ssh admin@192.168.88.12 'sudo systemctl stop wedge100s-bmc-poller.timer'
```

Blackouts continued after stopping the poller → **bmc-poller is NOT the cause**.

### 5. Measure BCM IRQ rate during blackout

```bash
ssh admin@192.168.88.12 \
  'a=$(grep "linux-kernel-bde" /proc/interrupts | awk "{print \$2}"); \
   sleep 2; \
   b=$(grep "linux-kernel-bde" /proc/interrupts | awk "{print \$2}"); \
   echo "BCM IRQ/s: $(( (b-a)/2 ))"'
```

During normal operation: **148/sec** (baseline). During BGP retry cycles: **5000–6000/sec**
(observed from journal correlation with `/proc/interrupts` snapshots).

### 6. Isolate BGP as cause (confirmed)

```bash
ssh admin@192.168.88.12 'sudo systemctl stop bgp'
```

**30/30 pings succeeded. Zero blackouts. SSH fully responsive.**

```bash
ssh admin@192.168.88.12 'sudo systemctl start bgp'
```

**Blackouts returned within 10 seconds.**

### 7. Inspect ConfigDB for BGP neighbors

```bash
ssh admin@192.168.88.12 'sudo python3 -c "
from swsscommon.swsscommon import SonicV2Connector
db = SonicV2Connector(use_unix_socket_path=True)
db.connect(\"CONFIG_DB\")
neighbors = db.keys(db.CONFIG_DB, \"BGP_NEIGHBOR|*\")
print(len(neighbors), neighbors[:3])
"'
```

**32 BGP neighbors** configured on `10.0.0.x` addresses, one per `Ethernet*` data-plane
port — all ports are administratively DOWN.

---

## Root Cause

```
32 BGP neighbors configured on DOWN Ethernet* ports
  → bgpd's reconnect retry cycle fires ARP requests for 10.0.0.x on each port
    → BCM ASIC CPU-traps failed ARP events → IRQ 11 bursts to 5,000–6,000/sec
      → IRQ 11 is XT-PIC, hardwired to CPU0 (can't be moved)
        → eth0-TxRx-0 (IRQ 55, PCI-MSI-X) was also on CPU0 by default
          → CPU0 softirq queue saturates during burst
            → NET_RX_SOFTIRQ stalls → eth0 RX processing halts
              → All NEW TCP SYN / ICMP echo dropped for 30–50 s
                → ESTABLISHED connections (conntrack entries cached) survive
```

The blackout cycle period (~30–50 s) matches bgpd's exponential reconnect retry cadence.

**XT-PIC constraint:** With `noapic` in effect, all legacy IRQs (8259A) are delivered
exclusively to CPU0. BCM's IRQ 11 cannot be moved to another CPU. The previous R30 fix
assumed IO-APIC routing (IRQ 16 on any CPU) which was incorrect for this kernel config.

---

## Fixes Applied

### Fix 1: `_pin_bcm_irq()` corrected (R30 follow-up)

**File:** [accton_wedge100s_util.py](../../platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py)

Old implementation used hardcoded `/proc/irq/16/smp_affinity` — IRQ 16 never exists with
`noapic`; the function silently printed a WARNING and exited.

New implementation:
1. Dynamically discovers BCM IRQ by parsing `/proc/interrupts` for `linux-kernel-bde`
2. Logs that BCM is XT-PIC/CPU0 hardwired (informational — no affinity change possible)
3. Discovers `eth0-TxRx-0` IRQ by parsing `/proc/interrupts`
4. Moves `eth0-TxRx-0` (IRQ 55, PCI-MSI-X) to **CPU2** (`smp_affinity = 4`)

Moving eth0-TxRx-0 to CPU2 decouples management-plane RX from BCM's CPU0 interrupt load,
so SSH TCP SYN processing continues on CPU2 even while CPU0 is saturated by BCM storms.

**Verified on hardware (2026-03-12):**
```bash
echo 4 | sudo tee /proc/irq/55/smp_affinity
cat /proc/irq/55/smp_affinity   # → 4
```

After the move, new interrupts accumulate on CPU2 (confirmed from `/proc/interrupts`).

### Fix 2: BGP disabled for this platform (operational)

```bash
sudo config feature state bgp disabled
sudo config save
```

The 32 BGP neighbor configs on DOWN data-plane ports represent a broken arch that needs
separate resolution. BGP disabled eliminates the ARP storm source entirely.

The underlying arch issue (BGP configured on interfaces that are never UP) is tracked
separately and needs addressing at the ConfigDB/platform config level.

### Fix 3: `MGMT_PORT` added to CONFIG_DB and init template

**Problem:** `mgmt_oper_status.py` (run by monit) logged `No management interface found`
because `MGMT_PORT` table was absent from CONFIG_DB. The management interface `eth0`
existed and had a DHCP address, but was not registered in SONiC's config.

**Diagnostic:**
```bash
sudo python3 -c "
from swsscommon.swsscommon import SonicV2Connector
db = SonicV2Connector(use_unix_socket_path=True)
db.connect('CONFIG_DB')
print(db.keys(db.CONFIG_DB, 'MGMT_PORT|*'))
"
# Output: []
```

**On-hardware fix:**
```bash
sudo sonic-db-cli CONFIG_DB hset "MGMT_PORT|eth0" alias eth0 admin_status up
sudo python3 /usr/bin/mgmt_oper_status.py   # verify: no error output
sudo config save -y
```

Verification:
```python
# CONFIG_DB: ['MGMT_PORT|eth0']
# STATE_DB:  {'admin_status': 'up', 'alias': 'eth0', 'oper_status': 'up'}
```

**Build fix:** Added `MGMT_PORT` to [init_cfg.json.j2](../../files/build_templates/init_cfg.json.j2)
so fresh ONIE installs get it automatically — consistent with the existing `NTP.src_intf: eth0`
entry in the same file:

```json
"MGMT_PORT": {
    "eth0": {
        "alias": "eth0",
        "admin_status": "up"
    }
}
```

`MGMT_PORT` is in `KEEP_BASIC_TABLES` in `config-setup.conf`, so it is preserved across
`config reload` operations once set.

---

## Key Facts Established (verified on hardware 2026-03-12)

- BCM56960 (`linux-kernel-bde`) is on **XT-PIC IRQ 11**, CPU0 hardwired — confirmed
- With `noapic` kernel arg: IRQ 16 does not exist; `/proc/irq/16/smp_affinity` does not exist
- `eth0-TxRx-0` is **PCI-MSI-X IRQ 55**, movable via `smp_affinity`
- `irqbalance` is **inactive** on this system (won't override affinity changes)
- BCM baseline rate: **~148 IRQ/s**; during BGP ARP storm: **5,000–6,000 IRQ/s**
- Stopping BGP → 100% ping success, zero SSH blackouts (definitive isolation test)

## What Was Ruled Out

- **`wedge100s-bmc-poller`**: stopped → blackouts continued. False correlation.
- **`irqbalance` interference**: inactive.
- **CPU saturation from other processes**: BCM at baseline 148/s is not the issue; it's
  the BGP-triggered bursts.

## Root Cause of admin_status: up on All Ports

### Correction to earlier session analysis

An earlier note attributed `admin_status: up` on all Ethernet ports to a "framework
default". This is **incorrect**. The situation has two layers:

**Upstream framework** (`src/sonic-config-engine/config_samples.py`, unmodified):
The `--preset t1` path calls `generate_t1_sample_config()` which does
`setdefault('admin_status', 'up')` for every port. This is the upstream SONiC behavior
for T1 switch deployments where all ports are expected to face connected peers.

**Our local change** (`src/sonic-config-engine/portconfig.py`, commit `686719df0`):
Added `'admin_status': 'up'` to the `BreakoutCfg.gen_port_config_dict()` breakout path.
This was explicitly requested because after a DPB breakout operation, the newly-generated
sub-ports lacked `admin_status`, causing them to default to admin-down in SONiC. The
request was to match Cumulus/Arista EOS behavior where ports come up when cables are
present, without needing manual `config interface startup`.

### admin_status: up is correct — portconfig.py change RESTORED

The `admin_status: up` default for both non-breakout (config_samples.py) and breakout
(portconfig.py) is intentional and matches Cumulus/Arista EOS behavior. The portconfig.py
revert was itself reverted — both paths remain `admin_status: up`.

### FRR DOES use oper-status — via kernel linkdown routes (verified 2026-03-12)

FRR zebra correctly distinguishes admin-up/oper-down:

```
# In FRR:
Interface Ethernet0 is up, line protocol is down   ← FRR sees NO-CARRIER

# Kernel routing table for NO-CARRIER ports:
10.0.0.0/31 dev Ethernet0 proto kernel scope link src 10.0.0.0 linkdown
```

When `IFF_UP` but `!IFF_RUNNING` (admin-up, no physical carrier), the kernel marks the
connected route with `RTNH_F_LINKDOWN`. FRR zebra reads this flag and marks the neighbor's
nexthop as `unresolved` via Nexthop Tracking (NHT):

```
# vtysh: show ip nht 10.0.0.1
10.0.0.1(Connected)
  unresolved(Connected)
  Client list: bgp(fd 41)
```

With `unresolved` NHT, bgpd does NOT generate ARP for those neighbors. BCM IRQ rate
stays at baseline 148/s even with BGP enabled and all ports admin-up.

### Root cause of persistent storm — RESOLVED 2026-03-12 (session 2)

The boot-race hypothesis was **ruled out**. Diagnostic `watch -n0.5 'ip route show | grep -c linkdown'`
showed 27–29 linkdown routes present within 13 seconds of SSH becoming available after reboot —
no window where all routes were live. BCM IRQ rate was 147/s at that same point.

**Actual cause**: Two ports (Ethernet48, Ethernet112) had physical DAC cables inserted and were
reporting `LOWER_UP` (carrier present). Their connected routes were installed **without**
`RTNH_F_LINKDOWN`, so FRR NHT saw those 2 BGP neighbors as reachable and bgpd ARP'd them.

Even 2 ports generating failed ARP retries (every ~10s per bgpd reconnect timer) was enough to
spike BCM CPU-trap interrupts to **1,878–4,342/s** and cause 30s SSH blackouts — identical
cadence to the 32-port storm, just smaller magnitude.

**Proof by isolation (verified 2026-03-12)**:

```
State                                          BCM IRQ/s   SSH (15 attempts)
27 linkdown + 2 live-carrier (Eth48, Eth112)  burst 4342  30s blackouts
27 linkdown + 2 admin-down  (Eth48, Eth112)   ~145/s      15/15 ok
```

```bash
# Admin-down the two cable-connected ports
sudo config interface shutdown Ethernet48
sudo config interface shutdown Ethernet112
# → connected routes removed entirely, NHT marks nexthops unresolved, no ARP
```

**Conclusion**: FRR correctly uses oper-status via `RTNH_F_LINKDOWN`. The ARP storm occurs
**only** on ports with genuine carrier (live kernel routes, no linkdown flag). The previous
session's "BGP stop → storm stops" was correct isolation; this session confirms the per-port
mechanism. BGP on an L2 switch with data-plane IPs configured on admin-up ports will always
produce this pattern whenever any port has physical carrier without a real BGP peer.

### Current workaround

BGP disabled via `sudo config feature state bgp disabled; sudo config save` (applied
2026-03-12, persists across reboots).

BGP is a TODO if ever needed: either remove the 32 data-plane BGP neighbor entries from
ConfigDB, or bind each neighbor to its interface (`neighbor X interface EthernetX`) so
FRR only ARP-resolves when that specific interface is operationally up with a real peer.

### Fix 4: `_pin_bcm_irq()` grep ambiguity corrected

The original `'eth0-TxRx-0' in line or ('eth0' in line and 'TxRx' in line)` fallback
matched `eth0-TxRx-3` (IRQ 58) instead of `eth0-TxRx-0` (IRQ 55) because the fallback
was too broad. Fixed to use `re.search(r'\beth0-TxRx-0\b', line)`.

Also noted: this fix (moving eth0-TxRx-0 to CPU2) mitigates but does not eliminate blackouts
when BCM spikes, because BCM IRQ 11 is still XT-PIC on CPU0 and CPU0 softirq can still
saturate under heavy ARP storms even with eth0 RX on CPU2.
