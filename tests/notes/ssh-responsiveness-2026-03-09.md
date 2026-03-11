# SSH Responsiveness Fix — BCM IRQ Affinity

## Problem
BCM56960 fires ~150 HW interrupts/sec on IRQ 16 (`linux-kernel-bde`). These were landing on CPU1,
which shares physical core 0 with CPU0. The softirq burst handling on physical core 0 caused 15-30s
windows where sshd would not accept connections — making interactive SSH sessions nearly unusable.

## Root Cause
- Intel D1508: 2 physical cores × HT = 4 logical CPUs
  - Physical core 0: CPUs 0, 1
  - Physical core 1: CPUs 2, 3
- IRQ 16 was landing on CPU1 (83M+ counts, 0 on all others)
- sshd competes for physical core 0 with BCM softirq work

## Fix (verified on hardware 2026-03-09)

### 1. Pin BCM IRQ to physical core 1 (CPUs 2-3)
```bash
echo c | sudo tee /proc/irq/16/smp_affinity
# Verify: cat /proc/irq/16/smp_affinity_list  → 2-3
```

### 2. Persist with systemd service
`/etc/systemd/system/bcm-irq-affinity.service`:
```ini
[Unit]
Description=Pin BCM kernel-bde IRQ to CPU core 1 (CPUs 2-3)
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c "echo c > /proc/irq/16/smp_affinity"

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable bcm-irq-affinity.service
```

### 3. Raise sshd scheduling priority
```bash
sudo renice -n -10 $(pgrep -x sshd)
```

Persist via `/etc/systemd/system/ssh.service.d/nice.conf`:
```ini
[Service]
Nice=-10
```

### 4. Pin syncd and database containers to physical core 1
```bash
sudo docker update --cpuset-cpus "2-3" syncd
sudo docker update --cpuset-cpus "2-3" database
```

Persist via `/usr/local/bin/sonic-cpu-affinity.sh` + `/etc/systemd/system/sonic-cpu-affinity.service`:
- Script waits up to 2 minutes for each container then applies `docker update --cpuset-cpus "2-3"`
- Service enabled, `After=docker.service`
- Note: `docker update` survives stop/start but NOT `docker rm + docker run` (SONiC recreates on reboot)
  hence the service re-applies on every boot

## CPU Layout Summary
```
Physical core 0 (CPUs 0, 1) — sshd / interactive / SONiC mgmt plane
Physical core 1 (CPUs 2, 3) — BCM IRQ 16, syncd (SAI), redis-server (database)
```
syncd and redis co-located with BCM IRQ is intentional: they share cache for the
tight BCM event → SAI callback → redis publish loop.

## Result (verified on hardware 2026-03-09)
- Physical core 0 (CPUs 0&1) now handles all interactive/sshd work, zero BCM interrupts
- Physical core 1 (CPUs 2&3) handles BCM softirq, syncd, and redis — all co-located
- sshd parent and new sessions run at nice -10 (higher priority vs. userspace daemons)
- Interrupt counts confirmed shifting to CPU2 immediately after change
- syncd and redis-server affinity mask `c` (CPUs 2&3) confirmed
