# SONiC Wedge100S — Layer-2 Switch Setup Guide

How to configure the Accton Wedge100S-32X as a pure Layer-2 switch under SONiC.
Intended for development/testing phases where no routing is needed and management
access must stay isolated and reliable.

(verified on hardware 2026-03-19, SONiC hare-lorax, kernel 6.1.0-29-2-amd64)

---

## Background

A freshly installed SONiC image on the Wedge100S comes up as a `LeafRouter` with:
- 32 data-plane ports each with a `10.0.0.x/31` IP address
- 32 BGP neighbours configured
- A `Loopback0` at `10.1.0.1/32`
- `PortChannel1` (Ethernet16 + Ethernet32) also with an IP

For platform bring-up and driver testing none of this L3 config is needed. This
guide strips it to a flat L2 switch with one VLAN, matching SONiC's documented
`ToRRouter` (T0) topology pattern from
`doc/configuration/examples/config_db_t0.json`.

---

## What Changes

| Table | Before | After |
|---|---|---|
| `DEVICE_METADATA.type` | `LeafRouter` | `ToRRouter` |
| `DEVICE_METADATA.default_bgp_status` | `up` | `down` |
| `DEVICE_METADATA.bgp_asn` | `65100` | *(removed)* |
| `INTERFACE` | 54 entries (L3 IPs) | empty `{}` |
| `BGP_NEIGHBOR` | 32 entries | *(removed)* |
| `LOOPBACK_INTERFACE` | `Loopback0 10.1.0.1/32` | *(removed)* |
| `PORTCHANNEL` | `PortChannel1` | *(removed)* |
| `PORTCHANNEL_MEMBER` | Ethernet16, Ethernet32 | *(removed)* |
| `VLAN` | `Vlan999` | `Vlan1` |
| `VLAN_MEMBER` | 1 port | all 41 ports, untagged |

All other tables (`PORT`, `FEATURE`, `MGMT_PORT`, `MGMT_VRF_CONFIG`, `NTP`,
`LOGGER`, etc.) are preserved unchanged.

---

## Step 1 — Back up the existing config

```bash
ssh admin@192.168.88.12 'sudo cp /etc/sonic/config_db.json /etc/sonic/config_db.json.pre-l2'
```

---

## Step 2 — Generate the new config_db.json

Run this on the development host (requires the current config fetched via
`sonic-cfggen`):

```bash
ssh admin@192.168.88.12 'sudo sonic-cfggen -d --print-data 2>/dev/null' \
  > /tmp/current_config_db.json

python3 - <<'EOF'
import json

with open('/tmp/current_config_db.json') as f:
    d = json.load(f)

KEEP = {
    'AUTO_TECHSUPPORT', 'AUTO_TECHSUPPORT_FEATURE',
    'BANNER_MESSAGE', 'BGP_DEVICE_GLOBAL', 'BREAKOUT_CFG', 'CRM',
    'FEATURE', 'FLEX_COUNTER_TABLE',
    'KDUMP', 'LOGGER', 'MGMT_PORT', 'MGMT_VRF_CONFIG',
    'NTP', 'PASSW_HARDENING', 'PORT',
    'SNMP', 'SNMP_COMMUNITY', 'SYSLOG_CONFIG', 'SYSLOG_CONFIG_FEATURE',
    'SYSTEM_DEFAULTS', 'VERSIONS',
}

new = {}
for k in KEEP:
    if k in d:
        new[k] = d[k]

# Fix DEVICE_METADATA
meta = dict(d['DEVICE_METADATA']['localhost'])
meta['type'] = 'ToRRouter'
meta['default_bgp_status'] = 'down'
meta.pop('bgp_asn', None)
new['DEVICE_METADATA'] = {'localhost': meta}

# Empty INTERFACE (documented T0 pattern — no L3 on data-plane ports)
new['INTERFACE'] = {}

# Single flat VLAN
new['VLAN'] = {'Vlan1': {'vlanid': '1'}}

# All data-plane ports untagged in Vlan1
new['VLAN_MEMBER'] = {
    f'Vlan1|{port}': {'tagging_mode': 'untagged'}
    for port in sorted(d['PORT'].keys())
}

with open('/tmp/new_config_db.json', 'w') as f:
    json.dump(new, f, indent=4, sort_keys=True)

print(f"VLAN_MEMBER: {len(new['VLAN_MEMBER'])} ports")
print(f"type: {new['DEVICE_METADATA']['localhost']['type']}")
print("Written to /tmp/new_config_db.json")
EOF
```

---

## Step 3 — Install and reload

```bash
scp /tmp/new_config_db.json admin@192.168.88.12:/tmp/new_config_db.json
ssh admin@192.168.88.12 'sudo cp /tmp/new_config_db.json /etc/sonic/config_db.json'
ssh admin@192.168.88.12 'sudo config reload -y'
```

`config reload` takes ~50 seconds. SSH drops and returns automatically once
the management VRF drop-in (Step 4) is in place.

---

## Step 4 — Fix management VRF persistence (required)

### The problem

SONiC's `interfaces-config.service` restarts the `networking` service during
`config reload`, which tears down and recreates the `mgmt` VRF device. Two things
break:

1. **eth0 loses its `master mgmt` enslavement** — `ifupdown2` should re-apply
   `vrf mgmt` from `/etc/network/interfaces`, but the `lo-m` dummy interface
   already exists, causing the `iface mgmt` stanza's `up ip link add lo-m` to
   fail silently, which leaves eth0 unmastered.

2. **sshd has a stale socket** — the `mgmt` VRF device gets a new kernel interface
   index on each reload. sshd (started with `ip vrf exec mgmt`) keeps its socket
   bound to the old index. Until sshd is restarted it cannot accept new connections,
   producing "Connection refused" even though sshd is running.

### The fix

Two systemd drop-ins, applied once and persistent across reboots:

**A) Re-enslave eth0, restart sshd, and re-enable STP after every interfaces-config run:**

```bash
sudo mkdir -p /etc/systemd/system/interfaces-config.service.d
sudo tee /etc/systemd/system/interfaces-config.service.d/mgmt-vrf-eth0.conf <<'EOF'
[Service]
ExecStartPost=-/bin/ip link set eth0 master mgmt
ExecStartPost=-/bin/systemctl restart ssh
ExecStartPost=-/sbin/brctl stp Bridge on
EOF
sudo systemctl daemon-reload
```

**B) Run sshd inside the management VRF** (so outbound connections from sshd —
e.g. ProxyJump — use the mgmt routing table):

```bash
sudo tee /etc/systemd/system/ssh.service.d/override.conf <<'EOF'
[Service]
ExecStartPre=
ExecStartPre=/usr/local/bin/host-ssh-keygen.sh
ExecStartPre=/usr/sbin/sshd -t
ExecStart=
ExecStart=/usr/bin/ip vrf exec mgmt /usr/sbin/sshd -D $SSHD_OPTS
EOF
sudo systemctl daemon-reload
sudo systemctl restart ssh
```

After applying both drop-ins, `config reload` is fully self-healing: SSH
recovers automatically ~50 seconds after reload starts.

### Verification

```bash
# eth0 enslaved to mgmt
ip link show eth0 | grep "master mgmt"

# sshd bound to mgmt VRF
sudo ss -tlnp | grep sshd
# Expected: 0.0.0.0%mgmt:22

# mgmt routing table intact
ip route show table mgmt
# Expected: default via 192.168.88.2 dev eth0
#           192.168.88.0/24 dev eth0 ...

# No L3 on data-plane ports
ip route show table main | grep -v docker0
# Expected: empty (only docker0 line)
```

---

## Resulting configuration summary

```
Management:
  eth0  192.168.88.12/24  master mgmt  (DHCP, mgmt VRF table 5000)
  sshd  0.0.0.0%mgmt:22  (ip vrf exec mgmt)

Data plane:
  Vlan1  — all 41 Ethernet ports, untagged access
  No IPs, no routing, no BGP, no PortChannels

Routing tables:
  main:  docker0 only
  mgmt:  default via 192.168.88.2, 192.168.88.0/24 dev eth0
```

---

## Step 5 — Spanning Tree (loop protection)

### Background

The Wedge100S has 4 x 100G DAC ports connected to the same EOS peer (rabbit-lorax):

| SONiC Port | EOS Port | Role |
|---|---|---|
| Ethernet16 | Et13/1 | (was PortChannel1 member) |
| Ethernet32 | Et14/1 | (was PortChannel1 member) |
| Ethernet48 | Et15/1 | standalone |
| Ethernet112 | Et16/1 | standalone |

All four are in Vlan1 with no STP. Without loop protection, a broadcast on Vlan1
can circulate indefinitely between them.

EOS uses `spanning-tree mode mstp`. SONiC does not have an STP container in this
build (see below for how to add one). In the meantime the Linux bridge's built-in
802.1D STP provides basic loop protection.

### Enable Linux bridge STP (802.1D)

```bash
sudo brctl stp Bridge on
```

This is already included in the interfaces-config drop-in from Step 4, so it
re-applies automatically after every `config reload`.

### What STP does in this topology

hare-lorax becomes the **root bridge** (no other STP-speaking switch on the
management network). All active ports go to `forwarding` state immediately since
the switch is root — no ports need to be blocked. If a loop is ever introduced
(e.g. a cable connecting two of the EOS-peered ports back-to-back), STP will
detect it and block the redundant port within ~30 seconds.

```bash
# View port STP states
sudo brctl showstp Bridge | grep -A1 "^Ethernet"

# Quick summary: forwarding ports only
sudo bridge link show | grep -v NO-CARRIER | grep forwarding
```

Active forwarding ports on hare-lorax as of 2026-03-19:

| Port | Connected to | State |
|---|---|---|
| Ethernet0 | Unknown device (DAC 25G breakout) | forwarding |
| Ethernet16 | rabbit-lorax Et13/1 (100G DAC) | forwarding |
| Ethernet32 | rabbit-lorax Et14/1 (100G DAC) | forwarding |
| Ethernet48 | rabbit-lorax Et15/1 (100G DAC) | forwarding |
| Ethernet66 | Server ens1f1np1 (10G breakout lane) | forwarding |
| Ethernet67 | Server ens1f1np1 (10G breakout lane) | forwarding |
| Ethernet80 | localhost lan0 (25G breakout lane) | forwarding |
| Ethernet81 | localhost lan0 (25G breakout lane) | forwarding |
| Ethernet112 | rabbit-lorax Et16/1 (100G DAC) | forwarding |

### Limitation: 802.1D vs MSTP

The Linux bridge STP is classic 802.1D — single instance, 30-second convergence
on topology change. EOS uses MSTP (802.1s) which supports multiple spanning-tree
instances and fast RSTP convergence. The two protocols are not directly
interoperable for topology negotiation; each switch runs its own instance
independently. For development and testing this is acceptable.

For production or multi-VLAN topologies, replace the bridge STP with
`docker-stp` (see next section).

---

## Building SONiC with docker-stp

The SONiC STP container (`docker-stp`) provides proper PVST/RPVST support
through the `stpd` daemon and `stpctl` CLI. The submodule is present at
`src/sonic-stp` but the container is excluded from the default build.

### Enable in the build

Add to `rules/config.user` (gitignored, local overrides only):

```makefile
# Enable STP container
SONIC_INCLUDE_STP = y
```

Or pass on the make command line:

```bash
BLDENV=trixie SONIC_INCLUDE_STP=y make \
  target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

### How it hooks into the build

| File | Role |
|---|---|
| `rules/sonic-stp.mk` | Builds `stp_1.0.0_amd64.deb` from `src/sonic-stp` |
| `rules/docker-stp.mk` | Builds `docker-stp.gz`; gated on `INCLUDE_STP = y` |
| `slave.mk` line 248 | Sets `INCLUDE_STP = y` when `SONIC_INCLUDE_STP = y` |

The container depends on `$(STP)` (the deb), `$(SWSS)`, and
`$(SONIC_RSYSLOG_PLUGIN)`. It uses `$(DOCKER_CONFIG_ENGINE_BOOKWORM)` as its
base (bookworm container on a trixie host — this is normal for SONiC).

### After building

Once the image includes `docker-stp`, enable the feature and configure MSTP-style
RPVST on Vlan1:

```bash
# Enable the stp container
sudo config feature state stp enabled

# Set mode to PVST (SONiC supports pvst/rpvst, not mstp directly)
sudo config spanning-tree mode pvst

# Enable on Vlan1
sudo config spanning-tree vlan add 1

# Verify
show spanning-tree
```

The `docker-stp` container replaces `brctl stp Bridge on` — remove the
`ExecStartPost` line from the interfaces-config drop-in once `docker-stp` is
running.

---

## Restoring L3 / LeafRouter config

The original config is preserved at `/etc/sonic/config_db.json.pre-l2`.

```bash
ssh admin@192.168.88.12 '
  sudo cp /etc/sonic/config_db.json.pre-l2 /etc/sonic/config_db.json
  sudo config reload -y
'
```

The mgmt VRF drop-ins (Step 4) are harmless with the L3 config — leave them
in place.
