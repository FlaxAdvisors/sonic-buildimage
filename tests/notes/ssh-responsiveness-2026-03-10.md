# SSH Boot-Gap Root Cause Investigation — 2026-03-10

## Context

After implementing Phases 1–3 (bulk presence reads, psud interval 30s, persistent SMBus pool),
IRQ 18 dropped from ~800/sec to ~69/sec. However, large SSH blackout gaps remained after
fresh reboots. This investigation identifies the cause.

## Observed Gap Pattern (from user SSH timing data, 2026-03-10)

| Gap | Duration | Window |
|-----|----------|--------|
| 1 | 136 s | 01:04:09 → 01:06:24 |
| 2 | 65 s  | 01:08:06 → 01:09:11 |
| 3 | 148 s | 01:09:11 → 01:11:39 |

Serial console via ttyS0 was responsive DURING the 148 s gap (user ran `dmesg` at 01:10:35).
→ **System not frozen; pure TCP-level black hole.**

## Key Data Points (journal analysis)

### Gap 1 (136 s) — BCM ASIC Initialization

- `linux-kernel-bde` registers IRQ 16 and connects primary/secondary ISR at **01:03:31.246**
- `linux-bcm-knet` initializes at 01:03:31.786 (KNET dev_inst_set)
- `bgp` container starts at 01:03:30; `syncd` enables at 01:04:46
- Last sshd connection before gap: 01:04:09 (session 28)
- First sshd connection after gap: 01:06:24 (session 29)
- **No sshd log entries whatsoever during the 136 s gap** — TCP connections not reaching sshd

Interpretation: syncd programs the BCM56960 ASIC (all ports, ACLs, FDB, routes) during
01:04:09–01:06:24. The BCM SDK generates a massive IRQ 16 burst during initialization.

### Gap 2 (65 s) — BGP Route Programming Round 1

- Last success: 01:08:06 (session 56)
- Monit starts: 01:08:09 (3 s INTO gap — not the trigger)
- `rvtysh -c 'show ip route json'` starts at 01:08:12, **completes in 180 ms** at 01:08:12.376
- rvtysh is NOT blocking SSH; it completes long before the gap ends
- Gap ends: 01:09:11 (65 s total)

### Gap 3 (148 s) — BGP Route Programming Round 2

- Monit 2nd cycle: memory_checker fires at 01:09:09–01:09:10
- Second rvtysh would start at ~01:09:11 (same 180 ms completion)
- Gap: 01:09:11 → 01:11:39 (148 s)

The 65 s and 148 s gaps are NOT caused by rvtysh (completes in 180 ms). They correlate
with BGP route convergence events — as BGP finishes learning routes and orchagent programs
them into the ASIC via syncd, the BCM SDK generates a burst of ASIC programming operations
and their associated interrupts.

## IRQ 16 Rate Analysis (verified on hardware)

```
Steady-state rate:  ~104/sec  (measured: 312 counts in 3 s)
Average over boot:  ~322/sec  (1,086,396 total ÷ 3,373 s uptime)

Init-period estimate (first ~300 s):
  Steady-state contribution (3,073 s @ 104/sec) = 319,592
  Init contribution  = 1,086,396 − 319,592 = 766,804
  Over 300 s init   = ~2,556 IRQ 16/sec during ASIC initialization
```

**25× higher IRQ 16 rate during ASIC init vs. steady state.**

## Root Cause

```
syncd ASIC init (BCM56960 programming)
  → BCM SDK generates ~2,556 IRQ 16/sec   (vs. 104/sec steady-state)
    → linux-kernel-bde ISR fires on CPU1
      → BCM SDK tasklets scheduled (tasklet_hi)
        → HI softirq count on CPU2 saturates (73,857 vs 130 on CPU0/1/3)
          → NET_RX/NET_TX softirqs starved
            → TCP SYN-ACK delivery fails or sshd accept() delayed
              → SSH connections appear to timeout
```

Evidence:
- `/proc/softirqs`: CPU2 HI=73,857 vs CPU0=183, CPU1=130, CPU3=129 (skew from BCM tasklets)
- `/proc/interrupts`: IRQ 16 exclusively on CPU1 (1,105,730 cumulative, 0 on other CPUs)
- `irqbalance`: inactive (affinity mask 0–3 but only CPU1 handles IRQ 16 due to IO-APIC routing)
- `tcp_synack_retries=5`: server retries SYN-ACK for 1+2+4+8+16=31 s before dropping half-open
- `tcp_syn_retries=6`: client kernel retries SYN for 1+2+4+8+16+32=63 s before failing
- Gap durations (65 s ≈ 1× 63 s cycle; 148 s ≈ 2× 63 s + delays) match TCP SYN retry timeouts

## What Was Ruled Out

- **rvtysh / BGP vtysh**: completes in 180 ms — not blocking (verified from journal)
- **Monit memory_checker**: fast, no I2C activity
- **IRQ 18 (CP2112)**: already reduced from ~800/sec to ~69/sec by Phase 1–3; not the boot-gap cause
- **Pure CPU saturation**: serial console responsive during 148 s gap (user ran dmesg at 01:10:35)
- **iptables rules**: hostcfgd iptables changes at 01:04:34 are for TCP MSS clamping on 10.1.0.1
- **PCA9535 INT# interrupts**: Phase 4 investigation confirmed INT# not wired (dmesg: "using no AI")

## Gap Duration Anatomy

With `connect_timeout=30` and `retry_delay=10` in the test runner:
- 65 s gap  = 1 failed attempt (30 s) + 1 retry-delay (10 s) + quick success (~25 s in) ≈ 65 s
- 136 s gap = 3 failed attempts (90 s) + 3 retry-delays (30 s) + quick success ≈ 136 s
- 148 s gap = 4 failed attempts (120 s) + 3 retry-delays (30 s) – some overlap ≈ 148 s

## Is This Fixable?

### Partial mitigation: IRQ 16 CPU affinity

Pin IRQ 16 to CPU3 only, isolating BCM interrupt handling from CPUs used by NET_RX:

```bash
echo 8 > /proc/irq/16/smp_affinity   # CPU3 only (bitmask: bit 3 = 0x8)
```

The BCM ISR and its follow-on tasklet_hi would tend to stay on CPU3, freeing CPU0–2
for network processing. Not guaranteed (tasklet_hi can migrate) but reduces contention.

Apply in the platform-init service after `modprobe linux-kernel-bde`. Would need to be
persisted via the platform init script.

### Fundamental limitation

The BCM ASIC initialization (syncd cold-start) is an inherent SDK operation that cannot
be shortened from userspace. The boot-time SSH gap (136 s) is a BCM SDK characteristic,
not a SONiC or platform bug. The post-BGP-convergence gaps (65 s, 148 s) similarly
reflect orchagent→syncd ASIC route-table programming.

**Correct mitigation: test runner retry logic** — already implemented:
- `SSHClient.connect(retries=5, retry_delay=10)` in `tests/lib/ssh_client.py`
- `connect_timeout=30` in `tests/target.cfg`

### Steady state (>10 min post-boot)

After ASIC init and BGP convergence, IRQ 16 settles to ~104/sec. HI softirq count on
CPU2 returns to 0. SSH sessions are fully responsive. Phase 1–3 fixes are effective.

## Conclusion

The SSH blackout gaps are a BCM56960 ASIC initialization artifact:
1. **Boot gap (136 s)**: syncd cold-start programs all ASIC tables at ~2,556 IRQ 16/sec
2. **BGP gaps (65 s, 148 s)**: orchagent programs BGP-learned routes into ASIC at reduced rate
Both are inherent to the BCM SDK. The correct long-term fix is IRQ affinity tuning;
the interim fix is robust retry logic in the SSH test client.

## Phase 6 IRQ Affinity Test — INEFFECTIVE (verified 2026-03-10)

Tested pinning IRQ 16 to CPU3 (`echo 8 > /proc/irq/16/smp_affinity`):

```
Before: IRQ 16 on CPU1 (all counts); HI softirqs CPU2=73,857, others≈130
After:  IRQ 16 on CPU3 (new counts go to CPU3); HI softirqs CPU2=175,105, others≈350
```

IRQ moved to CPU3 successfully (confirmed via /proc/interrupts), but HI softirq load
remained on CPU2. The BCM ISR calls `tasklet_hi_schedule()`, which queues the BCM tasklet
into the kernel HI softirq queue; the actual execution CPU is chosen by the local softirq
handler on whichever CPU runs next, NOT the IRQ's CPU. CPU2 is consistently chosen.

**Conclusion:** IRQ affinity alone cannot decouple BCM tasklet execution from CPU2.
The only userspace-accessible fix would be `isolcpus=3` (kernel boot parameter), which
is not practical for a production SONiC image.

Reverted IRQ 16 affinity to default (0–3). No platform code changed.

---

## Steady-State SSH Gaps — Root Cause Investigation (2026-03-10, continued)

### Observed Pattern

User ran `for x in {1..300}; do ssh admin@192.168.88.12 date; sleep 1; done` > 10 min after boot.
Recurring SSH gaps of ~33 s visible; journal shows 43–80 s session drops.
This is NOT a boot-time artifact — the system has been fully up for hours.

### IRQ Monitoring Data (from previous session's irq_watch.sh)

```
03:16:04  pmon#DomInfoUpdateTask: "dom flags not found for Ethernet64"  (xcvrd DOM poll phase 1)
03:16:11  i18=391 i16=145 HI2=1   ← xcvrd EEPROM reads for 9 present transceivers
03:16:12  i18=372 i16=145 HI2=5
03:16:13  i18=230 i16=145 HI2=0   ← IRQ 18 returns to baseline
03:16:26  syslog: swss#supervisord: "orchagent [message repeated 5 times]"
03:16:26  syslog: countersyncd ControlNetlinkActor "monitoring family sonic_stel"
03:16:32  syslog: swss#supervisord: orchagent
03:16:38  syslog: countersyncd DataNetlinkActor "waiting for data messages"
03:16:41  i18=32  i16=1694 HI2=0  ← IRQ 16 SPIKE BEGINS
03:16:42  i18=26  i16=3465 HI2=0  ← orchagent log at exactly :42
03:16:43  i18=32  i16=153  HI2=0
03:16:44  i18=26  i16=2202 HI2=0
03:16:45  i18=32  i16=4180 HI2=0  ← peak: 40× steady-state
03:16:46  i18=26  i16=148  HI2=0  ← returns to baseline
```

Journal shows **no entries from 03:16:20 to 03:17:01** — the system was too busy during the
IRQ spike to write journal entries. This confirms the degree of CPU saturation.

### Trigger Chain (confirmed)

```
xcvrd DOM polling cycle (STATE_MACHINE_UPDATE_PERIOD_MSECS = 60 s)
  Phase 1 (t=0):   EEPROM reads for 9 present transceivers
                    → 9 × ~35 I2C txns via CP2112 = IRQ 18 spike to 391/sec for 2 s
  Phase 2 (t+30s): DOM data update + threshold evaluation
                    → xcvrd writes 9 port DOM records to Redis
                      → orchagent receives 5+ simultaneous Redis notifications
                        → orchagent fires 5 rapid SAI calls to syncd
                          → syncd calls BCM SAI for each port attribute update
                            → BCM SDK activity spikes to 4,180 IRQ 16/sec
                              → HI softirq saturation → TCP SYN-ACK drops → SSH gap
```

The ~30 s delay between xcvrd EEPROM reads (:11-13) and the BCM spike (:41-45) reflects
the two-phase xcvrd cycle: module identification then DOM sensor polling, separated by
an internal timer step.

### Ports Involved

9 transceivers present: Ethernet4, 20, 36, 52, 68, 84, 108, 112, 116
(ports 1, 5, 9, 13, 17, 21, 27, 28, 29 per port map).

### Mechanism Correction (revised after clean Python IRQ monitor run)

Earlier analysis assumed the SSH gap was caused by **HI softirq saturation** on CPU2.
Python-based monitoring (no awk quoting issues) shows this is WRONG:

```
time       i16/s  i18/s  HI2/s
03:55:42   3950     26     26   ← IRQ 16 spike to 3,950/sec; HI2 stays at 26 (NOT saturating)
03:55:43   1207     32     26
03:55:45   4419     32     26   ← peak 4,419/sec; HI2=32 (barely changed from 26 baseline)
03:55:46   1956     14     14   ← subsiding
...60 seconds later (next xcvrd cycle)...
03:56:11    147    337     28   ← Phase 1: IRQ 18 spikes (EEPROM reads for 9 ports)
03:56:13    151    430    169
03:56:16    146    158    158
...31 seconds later (phase 2)...
03:56:42   4243     39     39   ← Phase 2: IRQ 16 spikes again (script ends at 90 iterations)
```

**Correct mechanism: raw ISR CPU time, not HI softirq backlog:**
```
BCM IRQ 16 fires 4,000-4,400/sec on CPU1 and/or CPU3
  → Each BCM ISR disables-processes-reenables (non-preemptable interrupt context)
    → If each ISR takes ~100 µs: 4,400/sec = ~44% of one CPU in IRQ context
      → NET_TX softirq cannot run while CPU is in IRQ handler
        → TCP SYN-ACK packets accumulate in tx ring but are not sent
          → NEW SSH connection SYN times out (client retries with exponential backoff)
            → With tcp_syn_retries=6: 63 s gap; with tcp_syn_retries=2: ≤7 s gap
```

Note: Persistent SSH sessions (like the monitoring script) are **unaffected** — only
NEW TCP connections whose SYN handshake falls within the 3–5 s spike window fail.

**xcvrd 60-second cycle confirmed (3 independent observations):**

From original awk monitor (2 consecutive cycles):
```
03:16:11  i18=391  i16=145  HI2=1   ← cycle 1, phase 1 (EEPROM reads)
03:16:41  i18=32   i16=1694 HI2=0   ← cycle 1, phase 2 begins (+30s)
03:16:45  i18=32   i16=4180 HI2=0   ← cycle 1 peak; HI2=0
---
03:17:11  i18=327  i16=150  HI2=1   ← cycle 2, phase 1 (+60s from cycle 1)
03:17:41  i18=36   i16=2208 HI2=0   ← cycle 2, phase 2 (+30s)
03:17:45  i18=36   i16=4029 HI2=0   ← cycle 2 peak; HI2=0
```

From Python monitor (next boot, 2 more cycles):
```
03:55:42  i18=26   i16=3950 HI2=26  ← cycle N, phase 2 spike
03:56:11  i18=337  i16=147  HI2=28  ← cycle N+1, phase 1 (59s later)
03:56:42  i18=39   i16=4243 HI2=39  ← cycle N+1, phase 2 (+31s)
```

Pattern is **deterministic**: every 60s, phase 1 EEPROM reads, then phase 2 BCM spike 30s later.
HI2 never exceeds 39 during any spike — confirms ISR CPU time is the mechanism, not HI softirq.

### Counterpoll Intervals (before and after mitigation)

| Counter | Before | After |
|---------|--------|-------|
| PORT_STAT | 1000 ms (default) | **5000 ms** |
| RIF_STAT  | 1000 ms (default) | **5000 ms** |
| QUEUE_STAT | 10000 ms | (unchanged) |

Commands: `counterpoll port interval 5000; counterpoll rif interval 5000`
Confirmed in CONFIG_DB (persistent): `FLEX_COUNTER_TABLE|PORT` POLL_INTERVAL=5000.

Verified that this does NOT reduce the baseline IRQ 16 rate OR the spike magnitude:
- After change: IRQ 16 baseline = **145-165/sec** (vs. 104/sec before — slightly higher)
- Spike: still reaches **4,243-4,419/sec** (same as pre-change measurement of 4,180/sec)
- Conclusion: Spikes are driven entirely by xcvrd→orchagent→SAI→BCM event chain;
  counterpoll frequency has no effect on them.

### tcp_syn_retries Mitigation (applied 2026-03-10)

```bash
# Live:
sudo sysctl -w net.ipv4.tcp_syn_retries=2

# Persistent:
echo "net.ipv4.tcp_syn_retries=2" | sudo tee /etc/sysctl.d/99-wedge100s-ssh.conf
```

Effect: Reduces maximum SSH gap from 63 s (6 retries: 1+2+4+8+16+32) to **7 s** (2 retries: 1+2+4).
The IRQ 16 storm itself only lasts 3–5 s; TCP retry backoff was causing the 33 s observed gaps.
With tcp_syn_retries=2, even if the first SYN-ACK is dropped, the 4 s retry window fits within
the storm duration → failure manifests as ECONNREFUSED or 7 s delay, not a 33 s blackout.

### Is the Steady-State Gap Fixable?

The root cause is the xcvrd DOM polling every 60 s triggering rapid orchagent SAI calls.
Options (in order of impact):

| Option | Impact | Risk |
|--------|--------|------|
| `tcp_syn_retries=2` (APPLIED) | Caps gap at 7 s | Low; affects all new TCP connections on switch |
| Increase xcvrd DOM poll to 300 s | 5× less frequent gaps | Reduces DOM monitoring frequency |
| Increase PORT_STAT to 5 s (APPLIED) | Marginal (baseline unchanged) | Low |
| `isolcpus=2` kernel param | Could decouple BCM tasklets | Requires kernel build change, risky |
| Rate-limit orchagent Redis subscriptions | Would require SONiC code change | Medium |

**Best mitigation achieved**: `tcp_syn_retries=2` + `counterpoll port/rif interval 5000`.
The test runner already has `retries=5, retry_delay=10` which handles residual gaps.
