<img src="flaxlogo_SD_Blue.png" alt="Flax Advisors, LLC" height="52" style="display:block;margin-bottom:12px">

# SONiC Wedge 100S-32X — Initial Setup Guide

**Platform:** Accton Wedge 100S-32X (Facebook Wedge 100S, Broadcom Tomahawk BCM56960)
**SONiC Release:** wedge100s-2026.04.20
**Verified on hardware:** 2026-03-29 (hare-lorax)

---

## About This Guide

This guide covers initial deployment of the Wedge 100S-32X running SONiC, from
power-on through first-login and baseline configuration. It is written for
operators familiar with Cisco IOS or Arista EOS who are new to SONiC. Where
SONiC behavior differs significantly from IOS, those differences are called
out explicitly.

> **SONiC vs. IOS Key Differences:**
> SONiC is a Linux-based NOS. The CLI (`show`, `config`, `sonic-cfggen`) runs
> as commands in a Bash shell, not in an IOS-style modal EXEC/CONFIG hierarchy.
> Configuration is stored in a Redis database (`CONFIG_DB`) and persisted as
> `/etc/sonic/config_db.json`. There is no `write memory` — changes made with
> `sudo config` commands take effect immediately and persist across reboots
> automatically.

---

## Chapter 1 — Hardware Access: Console and BMC

### 1.1 Port Naming and Interface Aliases

The Wedge 100S-32X has 32 front-panel QSFP28 100G ports. SONiC names them
`Ethernet0`, `Ethernet4`, `Ethernet8`, ... `Ethernet124` (incrementing by 4,
because each port uses 4 SerDes lanes on the Tomahawk ASIC).

This naming is non-intuitive for IOS/EOS operators. **We strongly recommend
enabling alias mode for all CLI sessions.** Alias mode displays ports as
`Ethernet1/1` through `Ethernet32/1`, matching the physical panel numbering
printed on the chassis.

#### Enable Alias Mode — Persistent for Your Session

```bash
# Add to ~/.bashrc so alias mode is always active:
echo 'export SONIC_CLI_IFACE_MODE=alias' >> ~/.bashrc
source ~/.bashrc
```

Once set, all `show` commands display friendly aliases automatically:

```bash
show interfaces status
#  Interface     Lanes    Speed  ...  Alias       Vlan  Oper  Admin
# -----------  -------  -------  ...  ----------  ----  ----  -----
#  Ethernet1/1   ...     100G   ...  Ethernet1/1   ...   up    up
#  Ethernet2/1   ...     100G   ...  Ethernet2/1   ...  down   up

show lldp neighbors
# LocalPort      Capability    PortID        TTL
# -----------    ----------    ----------    ---
# Ethernet5/1    BR, R         Ethernet13/1  120
```

#### Using Aliases in config Commands

When alias mode is active, `config` commands also accept the alias form:

```bash
# These are equivalent when SONIC_CLI_IFACE_MODE=alias:
sudo config interface shutdown Ethernet5/1
sudo config interface fec Ethernet5/1 rs
show interfaces transceiver presence Ethernet5/1
```

> **Note:** If alias mode is not set, you must use the internal SONiC name.
> The conversion is: `EthernetN` where `N = (panel_port_number - 1) × 4`.
> Panel port 5 → `Ethernet16`, panel port 13 → `Ethernet48`, etc.
> See Appendix A for the complete mapping.

The alias-to-SONiC name mapping for quick reference:

| Panel Port | SONiC Name  | Alias (this guide uses) |
|------------|-------------|-------------------------|
| 1          | Ethernet0   | Ethernet1/1             |
| 2          | Ethernet4   | Ethernet2/1             |
| 3          | Ethernet8   | Ethernet3/1             |
| 4          | Ethernet12  | Ethernet4/1             |
| 5          | Ethernet16  | Ethernet5/1             |
| ...        | ...         | ...                     |
| 32         | Ethernet124 | Ethernet32/1            |

**The remainder of this guide uses alias names (`Ethernet1/1` style) in all
command examples.** Set `SONIC_CLI_IFACE_MODE=alias` before following them.

---

### 1.2 Host Serial Console

The switch CPU presents a standard RS-232 console port. Access it from any
terminal server or laptop serial adapter:

| Parameter | Value |
|-----------|-------|
| Device    | ttyS0 (console port on chassis rear) |
| Speed     | **57600** baud |
| Data bits | 8 |
| Parity    | None |
| Stop bits | 1 |
| Flow ctrl | None |

```
! Typical minicom or screen invocation:
screen /dev/ttyUSB0 57600
minicom -D /dev/ttyUSB0 -b 57600
```

> **Note:** GRUB uses the same 57600 baud rate. Boot messages and kernel output
> appear at 57600. If you connect at 9600 you will see garbled output during boot.

When the system is running, pressing Enter at the serial console produces:

```
Debian GNU/Linux 12 sonic ttyS0

sonic login:
```

Login: `admin` / Password: see §3.3.

---

### 1.3 OpenBMC Access

The Wedge 100S-32X has an independent BMC (Aspeed AST2400) running OpenBMC.
The BMC controls fans, PSUs, and thermal sensors, and provides remote power
control and serial console access to the x86 host.

**Direct SSH to BMC:**

```bash
ssh root@<bmc-ip>
# Password: 0penBmc
```

**BMC via USB from a running SONiC switch** (does not require network):

```bash
# The BMC presents a USB CDC-ACM serial device and a USB Ethernet interface
# to the x86 host. IPv6 link-local addressing is auto-configured.
ssh -i /etc/sonic/wedge100s-bmc-key root@fe80::ff:fe00:1%usb0
```

> **Note:** After every BMC reboot, `authorized_keys` is cleared from BMC RAM.
> If SSH key authentication fails after a BMC reboot:
> ```bash
> sshpass -p '0penBmc' ssh-copy-id -o StrictHostKeyChecking=no root@<bmc-ip>
> ```

**Fallback — Serial console to BMC from the switch:**

```bash
# /dev/ttyACM0 is the BMC ACM serial device (USB CDC-ACM)
# Speed: 57600 8N1, Login: root / 0penBmc
screen /dev/ttyACM0 57600
```

---

### 1.4 wedge_power.sh — Remote Power Control

`wedge_power.sh` runs on the BMC and controls power to the x86 CPU complex.
It is the standard Facebook Wedge power management tool.

```bash
# Check power status
ssh root@<bmc-ip> 'wedge_power.sh status'

# Power on the x86 CPU (microserver)
ssh root@<bmc-ip> 'wedge_power.sh on'

# Power off the x86 CPU ungracefully (hard power cut)
ssh root@<bmc-ip> 'wedge_power.sh off'

# Reset (cold reboot) the x86 CPU
ssh root@<bmc-ip> 'wedge_power.sh reset'

# Reset the entire system including BMC and ASIC (use with caution)
ssh root@<bmc-ip> 'wedge_power.sh reset -s'
```

> **Caution:** `wedge_power.sh off` is an ungraceful power cut — equivalent to
> pulling the power cord. Use `sudo reboot` from the SONiC CLI for a clean shutdown
> when SONiC is running. Reserve `wedge_power.sh` for recovery situations.

---

### 1.5 sol.sh — Serial Over LAN Console

`sol.sh` provides a serial console session to the x86 CPU from the BMC,
without requiring a physical cable to the console port. This is particularly
useful for accessing GRUB during boot or diagnosing a hung SONiC instance.

```bash
# Open a SOL session to the switch CPU
ssh root@<bmc-ip> 'sol.sh'

# Exit the SOL session
Ctrl-X
```

The SOL session connects at 57600 baud matching the console port speed.
It shares the physical serial port — if you have a physical cable attached to
the console port, SOL and the physical cable will fight for the same bytes.
Use one or the other, not both simultaneously.

---

## Chapter 2 — Installing SONiC via ONIE

### 2.1 ONIE Overview

ONIE (Open Network Install Environment) is a pre-installed Linux mini-OS on
the Wedge 100S-32X that provides bare-metal image installation. Think of it as
the equivalent of a boot ROM that can fetch and install a NOS image from the
network or a local file.

ONIE is always present in a separate GRUB boot entry. The switch boots SONiC
by default once SONiC is installed.

---

### 2.2 Booting into ONIE

**Method 1 — From a running SONiC image:**

```bash
# Sets next_entry=ONIE in SONiC grubenv AND onie_mode=install in the
# ONIE-BOOT partition grubenv, then reboots. Installed by platform deb.
sudo sonic-reboot-onie
```

**Method 2 — From the console during boot:**

1. Connect to the serial console (§1.2) or open a SOL session (§1.5).
2. Power-cycle the switch using `wedge_power.sh reset`.
3. At the GRUB menu, select **ONIE** → **ONIE: Install OS**.

**Method 3 — If SONiC is unresponsive:**

```bash
# From the BMC:
ssh root@<bmc-ip> 'wedge_power.sh reset'
# Then interrupt GRUB from the console/SOL session
```

---

### 2.3 Installing a SONiC .bin Image

#### Step 1 — Place the image where ONIE can reach it

The image filename ONIE uses for auto-discovery on this platform is:

```
onie-installer-x86_64-accton_wedge100s_32x-r0.bin
```

Copy it to your HTTP server's document root under that exact name.
ONIE discovers installation images via:
- **DHCP option 67** (boot file name): ONIE fetches the URL from your DHCP server — auto-discovery uses the filename above
- **Static URL**: manually typed at the ONIE shell
- **USB drive** mounted at `/mnt/usb/`

#### Step 2 — Install from a URL (most common method)

At the ONIE shell (if not auto-discovered via DHCP):

```bash
onie-nos-install http://<server>/onie-installer-x86_64-accton_wedge100s_32x-r0.bin
```

ONIE downloads the image, verifies it, and installs it to the eMMC. The
process takes approximately 5–10 minutes. The switch reboots automatically
into SONiC when installation completes.

> **Note:** ONIE on the Wedge 100S-32X does not load USB CDC-ACM or USB-CDC-Ethernet
> kernel modules. The BMC management path (usb0) is not available during ONIE.
> Use the physical management ethernet port (eth0) for network-based installation.

---

### 2.4 Removing a Stale SONiC Image

If you need to reinstall from scratch (e.g., to test a fresh-boot ZTP flow):

```bash
# From a running SONiC image — uninstall and reboot into ONIE
sudo sonic-installer remove <image-name>
# Or to immediately boot into ONIE for reinstall:
sudo sonic-reboot-onie
```

From the ONIE shell, to uninstall the current SONiC image without immediately
installing a new one (leaves the switch in ONIE-only state):

```bash
onie-nos-remove
```

> **Note:** `onie-nos-remove` is destructive. It wipes `/etc/sonic/config_db.json`
> and all locally stored configs. Back up your configuration before running it:
> ```bash
> scp admin@<switch-ip>:/etc/sonic/config_db.json ./config_db_backup.json
> ```

---

## Chapter 3 — First Login and Initial Configuration

### 3.1 Management Interface — Default Behavior

After a fresh install, the Wedge 100S-32X obtains its management IP address
via DHCP on `eth0`. The management interface is placed in a separate routing
VRF named `mgmt` (see §7 for VRF details).

```bash
# Check management IP (once connected via console or SOL):
show management_interface address
# or:
ip -br addr show eth0
```

Expected output after DHCP assignment:

```
eth0             UP             192.168.88.12/24
```

The system is then reachable via SSH:

```bash
ssh admin@192.168.88.12
```

---

### 3.2 First SSH Connection

```bash
ssh admin@<management-ip>
```

On first connection you will see a banner and be dropped into a standard Linux
Bash shell. The `admin` user can run `sudo` commands without a password.

```
Debian GNU/Linux 12 (SONiC) #2 SMP

admin@sonic:~$
```

After first login, set the system hostname: `sudo config hostname <your-hostname>`

---

### 3.3 Changing the Default Password

The default credentials are:

| Account | Default Password |
|---------|-----------------|
| `admin` | `YourPaSsWoRd` |

> **Security Warning:** Change the default password immediately on first login.
> The default password is well-known.

```bash
# Change password for the admin user
passwd
# You will be prompted for the current password, then the new password twice.
```

To add additional users:

```bash
# Add a new admin-level user
sudo useradd -m -G sudo,docker newuser
sudo passwd newuser
```

---

### 3.4 Verifying System State

Before making configuration changes, verify that SONiC has fully initialized:

```bash
# Check overall system readiness
show system status

# Check which services are running
show services

# Verify platform hardware detected correctly
show platform summary
```

Expected `show platform summary` output:

```
Platform: x86_64-accton_wedge100s_32x-r0
HwSKU: Accton-WEDGE100S-32X
ASIC: broadcom
ASIC Count: 1
Serial Number: AI09019591
Hardware Revision: N/A
```

---

## Chapter 4 — Zero Touch Provisioning (ZTP)

### 4.1 What ZTP Does

ZTP (Zero Touch Provisioning) automates the first-boot configuration of a
freshly installed switch. On first boot, if no `config_db.json` exists,
SONiC initiates a DHCP request on `eth0`. If the DHCP server returns a
boot file URL (option 67), SONiC fetches and applies a ZTP profile JSON
from that URL. This is equivalent to Cisco's ZTP or Arista's ZTP feature.

The ZTP flow:

```
ONIE installs SONiC → first boot → config-setup checks for config_db.json
    → not found → ZTP enabled? → yes → DHCP on eth0 → option 67 URL
        → fetch ZTP profile → apply config_db.json plugin → reboot
```

---

### 4.2 Checking ZTP Status

```bash
# Is ZTP currently running?
show ztp status

# Is ZTP enabled (admin mode)?
ztp status

# Verbose status with history:
show ztp status --verbose
```

---

### 4.3 Disabling ZTP

If you are configuring the switch manually (no ZTP server) and want to
prevent ZTP from repeatedly attempting to contact a server that does not exist:

```bash
# Disable ZTP (persists across reboots)
sudo ztp disable

# Verify
show ztp status
# Expected: ZTP: Disabled
```

Once ZTP is disabled, SONiC will use whatever `config_db.json` is present.
If none exists, it will generate a default config using `sonic-cfggen`.

---

### 4.4 Running ZTP Manually

If the switch already has a config and you want to re-run ZTP (for example,
to apply a new configuration from your provisioning server):

```bash
# Run ZTP once immediately (ignores admin-mode disable)
sudo ztp run -y

# Re-enable ZTP for next boot, then reboot
sudo ztp enable
sudo reboot
```

---

### 4.5 ZTP Profile Format

ZTP profiles are JSON files served via HTTP/TFTP from your provisioning server.
The DHCP server points to the profile via option 67.

**Minimal example — apply a config_db.json at provisioning time:**

```json
{
    "ztp": {
        "01-configdb-json": {
            "dynamic-url": {
                "source": {
                    "prefix": "http://provisioning-server.example.com/ztp/",
                    "identifier": "hostname",
                    "suffix": "_config_db.json"
                }
            }
        }
    }
}
```

This tells ZTP to fetch `http://provisioning-server.example.com/ztp/<hostname>_config_db.json`
where `<hostname>` is the switch's current hostname (typically the ONIE hostname
derived from the management MAC address). The downloaded file is then applied
as the new `config_db.json`.

**Reference profiles** (shipped with this image) are located on the switch at:

```
/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/ztp/
├── ztp-l2-sample.json      # L2 deployment profile
├── l2-config_db.json       # L2 config template
├── ztp-l3-sample.json      # L3 deployment profile (placeholder)
└── l3-config_db.json       # L3 config template (placeholder)
```

Copy these to your provisioning server and customize the `REPLACE-*` placeholders
before serving them.

---

### 4.6 ZTP L2 Config Template

The shipped `l2-config_db.json` configures:
- All 32 ports at 100G with RS-FEC, admin up
- BGP feature disabled (see §9)
- STP (spanning-tree) enabled
- Hostname, platform, and MAC set to placeholder values (replace before serving)

Minimum fields to replace for each switch:

| Field | Description |
|-------|-------------|
| `DEVICE_METADATA.localhost.hostname` | Switch hostname (e.g., `leaf-01`) |
| `DEVICE_METADATA.localhost.mac` | System MAC (from `show platform syseeprom`) |
| `MGMT_INTERFACE.eth0\|IP/PREFIX` | Static management IP (if not using DHCP) |
| `MGMT_INTERFACE.eth0\|IP/PREFIX.gwaddr` | Management gateway |

---

## Chapter 5 — Saving and Managing Configuration

### 5.1 How SONiC Configuration Persistence Works

Unlike IOS/EOS, SONiC does not have a `write memory` command. Configuration
changes made via `sudo config` commands are applied **immediately** to the
running CONFIG_DB (Redis) and are **automatically persisted** to
`/etc/sonic/config_db.json` via the `config save` command or automatically
on some operations.

The persistence model:

```
sudo config <command>  →  CONFIG_DB (running, Redis)
config save -y         →  /etc/sonic/config_db.json (saved to disk)
```

On the next boot, `config-setup` loads `/etc/sonic/config_db.json` back into
CONFIG_DB.

> **Important:** Some `sudo config` commands do NOT auto-save to disk. Always
> run `config save -y` after making configuration changes you want to persist
> across reboots.

---

### 5.2 Saving Configuration

```bash
# Save running configuration to disk (equivalent to "write memory")
config save -y

# Save to a named backup file
config save /etc/sonic/config_db.json.backup-$(date +%Y%m%d)

# Verify the saved config:
cat /etc/sonic/config_db.json | python3 -m json.tool | head -50
```

---

### 5.3 Viewing Running Configuration

```bash
# Show all configuration (equivalent to "show running-config")
sudo sonic-cfggen -d --print-data 2>/dev/null | python3 -m json.tool | less

# Show a specific config table:
sonic-db-cli CONFIG_DB HGETALL 'DEVICE_METADATA|localhost'
sonic-db-cli CONFIG_DB HGETALL 'PORT|Ethernet0'

# Show interface IP assignments:
show ip interfaces

# Show VLAN configuration:
show vlan brief
show vlan config

# Show platform EEPROM (serial number, MAC, part number):
show platform syseeprom
```

---

### 5.4 Reloading Configuration

`config reload` applies the saved `config_db.json` to the running system,
restarting all SONiC services. It is the SONiC equivalent of `copy startup-config running-config`.

```bash
# Reload configuration from disk (takes ~50 seconds):
sudo config reload -y

# Non-interactive (script-safe):
sudo config reload -y --no-service-restart    # does NOT restart containers
```

> **Warning — SSH Drop During config reload:** The `config reload` command
> restarts the `networking` service, which briefly tears down and recreates
> the `mgmt` VRF. SSH connections drop during this window. On this platform,
> the systemd drop-ins ensure SSH automatically recovers within ~60 seconds.
> Do not panic — wait 60 seconds and reconnect.

---

## Chapter 6 — Management Interface Configuration

### 6.1 OOB Management Interface

The out-of-band management interface is `eth0`, which maps to the RJ-45
management port on the front panel (labeled "MGMT"). All management traffic
— SSH, SNMP, NTP, syslog — traverses `eth0` and is isolated in the `mgmt`
VRF from data-plane traffic on the front-panel ports.

---

### 6.2 DHCP (Default)

The management interface is configured for DHCP by default. No action is
needed if DHCP is available and you are comfortable with a dynamic IP.

To verify DHCP assignment:

```bash
show management_interface address
```

To release and renew the DHCP lease (useful if DHCP server changes):

```bash
# If management VRF is enabled:
sudo ip vrf exec mgmt dhclient -r eth0   # release
sudo ip vrf exec mgmt dhclient eth0      # renew

# If management VRF is disabled (recommended — see §7.3):
sudo dhclient -r eth0
sudo dhclient eth0
```

---

### 6.3 Static IP Address

To assign a static management IP address:

```bash
# Step 1 — Configure static IP, gateway, and DNS
sudo config management_interface add eth0 <ip-address/prefix-length> <gateway-ip>

# Example:
sudo config management_interface add eth0 192.168.88.12/24 192.168.88.2

# Step 2 — Add DNS server(s)
sudo config dns_nameserver add 8.8.8.8
sudo config dns_nameserver add 8.8.4.4

# Step 3 — Verify
show management_interface address
ping 192.168.88.2    # ping gateway — must run in mgmt VRF (see §7)
sudo ip vrf exec mgmt ping 192.168.88.2
```

> **Note — VRF and Ping:** If the management VRF is enabled, `ping`
> and other tools must be prefixed with `sudo ip vrf exec mgmt` — without it,
> `eth0` is invisible to the command. See Chapter 7 for the full explanation
> and our recommendation to disable the VRF.

To verify DNS resolution:

```bash
# VRF enabled:
sudo ip vrf exec mgmt nslookup google.com
# VRF disabled:
nslookup google.com
```

---

### 6.4 Default Gateway

The default gateway for management traffic is set as part of the
`config management_interface add` command (§6.3). To change it separately:

```bash
sudo config management_interface delete eth0 <old-ip/prefix>
sudo config management_interface add eth0 <new-ip/prefix> <new-gateway>
```

---

### 6.5 NTP Configuration

```bash
# Add NTP servers
sudo config ntp add 0.pool.ntp.org
sudo config ntp add 1.pool.ntp.org
sudo config ntp add time.cloudflare.com

# Verify NTP synchronization
# If VRF enabled:  sudo ip vrf exec mgmt ntpq -p
# If VRF disabled:
ntpq -p

# Check system clock
show clock
```

NTP uses the management interface for outbound connections. If the clock is
not synchronizing, verify the NTP servers are reachable:

```bash
# VRF enabled:
sudo ip vrf exec mgmt ping time.cloudflare.com
# VRF disabled:
ping time.cloudflare.com
```

---

### 6.6 Hostname

```bash
# Set system hostname
sudo config hostname <new-hostname>

# Verify
hostname
# or:
show version | grep Hostname
```

The hostname is stored in `DEVICE_METADATA.localhost.hostname` in CONFIG_DB
and persists across reboots.

---

## Chapter 7 — Management VRF: What It Is and Why It Matters

### 7.1 What Is the Management VRF?

SONiC places `eth0` (the management port) into a Linux VRF (Virtual Routing
and Forwarding) instance named `mgmt`. This is functionally equivalent to
Cisco's `management vrf` or Arista's `ip routing vrf MGMT` — it creates a
separate routing table for management traffic, isolated from the data-plane
routing table.

**Why this matters in practice:**

Without VRF awareness, management-plane commands fail silently:

```bash
# These fail — eth0 is not in the default namespace:
ping 192.168.88.2          # "Network unreachable"
ssh user@192.168.88.100    # "No route to host"
curl http://10.0.0.1/      # Connection refused
```

**With VRF-aware execution:**

```bash
# These work:
sudo ip vrf exec mgmt ping 192.168.88.2
sudo ip vrf exec mgmt ssh user@192.168.88.100
sudo ip vrf exec mgmt curl http://10.0.0.1/
```

---

### 7.2 VRF Recovery from Console

If you lose SSH access to the switch because the management VRF is misconfigured,
use the serial console (§1.2) or SOL (§1.5) to recover:

```bash
# From the console, check VRF state:
ip vrf show                          # list all VRFs
ip link show eth0                    # should show "master mgmt"
ip route show table mgmt             # should show default route and local subnet

# If eth0 is not enslaved to mgmt:
sudo ip link set eth0 master mgmt

# If SSH is not responding (sshd bound to wrong VRF):
sudo systemctl restart ssh

# If mgmt routing table has no default route:
sudo ip route add default via <gateway> vrf mgmt
```

> **Warning — VRF Complexity:** The management VRF interacts with `config reload`
> in a non-obvious way. After a `config reload`, `eth0` may lose its VRF
> membership and SSH may stop accepting connections. This is a known issue on
> this platform. The permanent fix (systemd drop-ins) is applied at installation
> time; see the SONiC Wedge100S L2 Setup Guide for full details. If SSH drops
> after a `config reload` and does not return within 60 seconds, use the serial
> console to run `sudo ip link set eth0 master mgmt && sudo systemctl restart ssh`.

---

### 7.3 Our Recommendation: Do Not Use VRF for OOB Management

The management VRF adds significant operational complexity — every management-plane
command requires the `sudo ip vrf exec mgmt` prefix, `config reload` silently breaks
SSH until the systemd drop-ins re-enslave `eth0`, and any misconfiguration can strand
you from the switch with no recourse except the serial console.

**For most deployments, disable the management VRF:**

```bash
# Disable management VRF (takes effect after config reload)
sudo config management_vrf_config disable
config save -y
sudo config reload -y
```

After disabling, management traffic uses the default routing table and all
management-plane commands work without the `ip vrf exec mgmt` prefix:

```bash
# These work without ip vrf exec mgmt when VRF is disabled:
ping 192.168.88.2
ssh user@192.168.88.100
ntpq -p
```

**When to keep VRF enabled:** If your deployment requires strict separation
between management-plane and data-plane routing tables — for example, if
management and data interfaces share overlapping subnets — leave VRF enabled
and accept the operational overhead. In that case, the systemd drop-ins
applied at installation time make the system self-healing after `config reload`.

**VRF disable recovery** (if something goes wrong after disabling):

```bash
# From the console, re-enable VRF:
sudo config management_vrf_config enable
config save -y
sudo config reload -y
```

---

## Chapter 8 — Interface Configuration

### 8.1 Displaying Interface Status

```bash
# Brief status of all interfaces (IOS equivalent: "show interfaces status")
show interfaces status

# Detailed counters for a specific interface
show interfaces counters Ethernet0

# All counters
show interfaces counters

# Reset counters (zeroes the counter database)
sonic-clear counters
```

Sample `show interfaces status` output:

```
  Interface            Lanes    Speed    MTU    FEC        Alias    Vlan    Oper    Admin    Type         Asym PFC
-----------  ---------------  -------  -----  -----  ---------  ------  ------  -------  --------  ----------
  Ethernet0  117,118,119,120    100G    9100     rs  Ethernet1/1  trunk      up       up  QSFP28/QSFP              off
  Ethernet4  113,114,115,116    100G    9100     rs  Ethernet2/1  trunk    down       up  QSFP28/QSFP              off
```

---

### 8.2 LLDP Neighbor Discovery

LLDP is enabled by default on all SONiC ports.

```bash
# Show all LLDP neighbors (equivalent to: show lldp neighbors)
show lldp neighbors

# Show LLDP detail for a specific port
show lldp neighbors Ethernet0

# Show LLDP table (brief)
show lldp table
```

LLDP is an essential tool for verifying cabling: if a port is connected to
an LLDP-speaking neighbor, `show lldp neighbors` immediately shows the neighbor's
chassis ID, port ID, and system description.

---

### 8.3 Transceiver (Optics) Commands

```bash
# Show which ports have transceivers inserted
show interfaces transceiver presence

# Show transceiver EEPROM info (vendor, part number, serial)
show interfaces transceiver eeprom Ethernet100

# Show DOM sensor readings (Rx/Tx power, bias, temperature)
show interfaces transceiver eeprom --dom Ethernet100
show interfaces transceiver pm Ethernet100

# Show transceiver status (TX disable, fault, CDR lock)
show interfaces transceiver status Ethernet100

# Show low-power mode state
show interfaces transceiver lpmode Ethernet100
```

> **FEC Reference:** All 32 ports default to 100G with RS-FEC (`rs`). SONiC supports
> three FEC modes on this platform:
>
> | Speed | FEC Mode | `config` value | Standard |
> |-------|----------|----------------|----------|
> | 100G  | Reed-Solomon FEC | `rs` | IEEE 802.3bm CL91 — default; required for SR4, CWDM4, PSM4 |
> | 100G LR4 | No FEC | `none` | IEEE 802.3ba — use for LR4 single-mode optics |
> | 25G (breakout) | Reed-Solomon FEC | `rs` | IEEE 802.3bj CL91 — preferred for 25G DAC/optics |
> | 25G (breakout) | Fire Code / BASE-R | `fc` | IEEE 802.3bj CL74 — for DAC cables that don't support RS |
> | 10G (breakout) | No FEC | `none` | IEEE 802.3ae — standard for 10GBASE-SR/LR |
>
> See the SONiC Wedge100S Optics Setup Guide for detailed transceiver diagnostics.

---

### 8.4 Administratively Enabling/Disabling Ports

```bash
# Disable a port (equivalent to "shutdown" in IOS)
sudo config interface shutdown Ethernet0

# Enable a port (equivalent to "no shutdown")
sudo config interface startup Ethernet0

# Verify:
show interfaces status Ethernet0
```

---

### 8.5 Port Speed and FEC

All 32 ports default to 100G with RS-FEC. To change FEC:

```bash
# RS-FEC (default, correct for SR4/CWDM4)
sudo config interface fec Ethernet0 rs

# No FEC (required for LR4 in some configurations)
sudo config interface fec Ethernet104 none

# Verify:
show interfaces status Ethernet104 | grep -i fec
```

---

### 8.6 Port Counters

```bash
# Show packet and byte counters for all ports
show interfaces counters

# Show counters for a specific port
show interfaces counters Ethernet0

# Show error counters
show interfaces counters error

# Clear all counters
sonic-clear counters

# Watch counters update in real time (refresh every 2s)
watch -n 2 'show interfaces counters Ethernet1/1'
```

---

### 8.7 Port Breakout

The Wedge 100S-32X supports port breakout: a single 100G QSFP28 port can be
broken out into 4×25G sub-ports. This is useful for connecting to 25G servers.

```bash
# Check current breakout configuration
show interface breakout

# Break port 1 (Ethernet0) into 4×25G
sudo config interface breakout Ethernet0 '4x25G[10G]' -y -f

# Break port 1 back to 1×100G
sudo config interface breakout Ethernet0 '1x100G[40G]' -y -f

# Verify new sub-interfaces
show interfaces status Ethernet0 Ethernet1 Ethernet2 Ethernet3
```

> **Note:** After breakout, the port name changes: `Ethernet0` broken into
> 4×25G becomes `Ethernet0`, `Ethernet1`, `Ethernet2`, `Ethernet3`. The
> sub-ports do not follow the standard ×4 naming convention — they are
> consecutive integers starting from the parent port's number.

---

### 8.8 VLAN Configuration

#### Create a VLAN

```bash
# Create VLAN (equivalent to: vlan 100)
sudo config vlan add 100

# Verify:
show vlan brief
```

#### Add a Port to a VLAN

```bash
# Add access (untagged) port to VLAN 100  (alias mode active)
sudo config vlan member add 100 Ethernet1/1

# Add trunk (tagged) port to VLAN 100
sudo config vlan member add --tagging_mode tagged 100 Ethernet2/1

# Verify:
show vlan brief
show interfaces Ethernet1/1 | grep vlan
```

#### Configure IP on a VLAN (SVI — L3 VLAN interface)

```bash
# Create SVI for VLAN 100 and assign IP
sudo config interface ip add Vlan100 192.168.100.1/24

# Verify:
show ip interfaces
```

---

### 8.9 Assigning an IP Address to a Port (Routed Interface)

```bash
# Assign IP directly to a front-panel port (no VLAN)  (alias mode active)
# First remove port from any VLAN if it's currently a switchport:
sudo config vlan member del 1 Ethernet1/1    # remove from VLAN 1 if present

# Assign IP:
sudo config interface ip add Ethernet1/1 10.0.0.1/31

# Verify:
show ip interfaces
show interfaces Ethernet1/1
```

---

### 8.10 PortChannel (LAG) Configuration

```bash
# Create a PortChannel  (alias mode active)
sudo config portchannel add PortChannel1

# Add members (LACP is default)
sudo config portchannel member add PortChannel1 Ethernet5/1
sudo config portchannel member add PortChannel1 Ethernet9/1

# Set minimum links (equivalent to "port-channel min-links 1")
sudo config portchannel minimum-links PortChannel1 1

# Verify LACP state:
show interface portchannel
show interfaces PortChannel1

# Add IP to PortChannel:
sudo config interface ip add PortChannel1 10.0.1.1/31
```

---

## Chapter 9 — BGP: Default State and Recommended Configuration

### 9.1 Default First-Boot BGP Behavior

> **Warning:** On a fresh SONiC install without ZTP or a custom config_db.json,
> SONiC generates a `LeafRouter` topology by default. This configuration creates:
> - 32 BGP neighbor sessions (one per front-panel port, `10.0.0.x/31` addresses)
> - A BGP ASN of 65100
> - PortChannel1 with two members
>
> With no BGP peers present, the BGP container (`bgpd` + `zebra`) continuously
> attempts to establish sessions on all 32 ports and processes keepalive timeouts.
> This saturates the control-plane CPU, causing measurable SSH latency and
> reduced system responsiveness. **The first action after a fresh install should
> be to disable BGP if you are not deploying an L3 topology.**

---

### 9.2 Disabling BGP (Recommended for L2 Deployments)

```bash
# Method 1 — Shut down all BGP sessions (temporary, reverts on reboot):
sudo config bgp shutdown all

# Method 2 — Stop the BGP Docker container (survives config reload):
docker stop bgp

# Method 3 — Disable the BGP feature permanently in CONFIG_DB (survives reboot):
sudo config feature state bgp disabled
```

**Verification:**

```bash
# Confirm BGP container is stopped:
docker ps | grep bgp
# Should return no output when disabled

# Confirm no routes installed by BGP:
show ip bgp summary
# Expected: "BGP not configured" or empty output
```

---

### 9.3 BGP Design Issue: ARP on Operationally Down Ports

A significant SONiC behavior to be aware of: when BGP is configured with
L3 interface IPs on data-plane ports, SONiC installs ARP entries and sends
ARP requests on **all configured ports**, even those that are operationally
down (no cable connected). This has two consequences:

1. **Control-plane CPU saturation:** The kernel's ARP subsystem repeatedly
   sends ARP requests and processes timeouts for 32 ports simultaneously,
   even when none are connected.

2. **Unexpected ARP traffic on connected ports:** When a port comes up and
   is assigned to a BGP peering subnet, ARP probes begin immediately — before
   BGP sessions are established — which can be surprising when debugging
   bring-up issues.

**Mitigation:** Disable BGP globally (§9.2) or administratively shut down
individual BGP sessions for ports that are not connected:

```bash
# Shut down BGP session for a specific neighbor:
sudo config bgp shutdown neighbor <neighbor-ip>

# Or shut down BGP for all sessions (soft disable):
sudo config bgp shutdown all
```

---

### 9.4 Enabling and Configuring BGP (L3 Deployments)

If you are deploying an L3 topology with eBGP peering:

```bash
# Step 1 — Ensure BGP feature is enabled
sudo config feature state bgp enabled

# Step 2 — Enter the vtysh BGP configuration shell
sudo vtysh

# Inside vtysh (BGP CLI, similar to IOS/EOS BGP config):
sonic# configure terminal
sonic(config)# router bgp 65001
sonic(config-router)# bgp router-id 10.0.0.1
sonic(config-router)# neighbor 10.0.0.0 remote-as 65002
sonic(config-router)# address-family ipv4 unicast
sonic(config-router-af)# neighbor 10.0.0.0 activate
sonic(config-router-af)# end
sonic# write memory
```

> **Note:** SONiC BGP uses FRRouting (FRR), which provides a `vtysh` CLI that
> closely resembles Cisco IOS BGP configuration syntax. The `vtysh` shell is
> the correct place to configure BGP — do not edit FRR config files directly.

**Show BGP status:**

```bash
sudo vtysh -c "show bgp summary"
sudo vtysh -c "show bgp ipv4 unicast"
show ip bgp summary           # SONiC CLI equivalent
```

---

## Chapter 10 — System Verification and Health Monitoring

### 10.1 System Status

```bash
# Overall system health
show system status

# Docker container status (all SONiC services):
docker ps

# Show software versions:
show version

# Show platform hardware info:
show platform summary
```

---

### 10.2 Environmental Monitoring

```bash
# Fan status and speed
show platform fan

# Temperature sensors
show platform temperature

# PSU status and telemetry
show platform psu

# All environmental data in one view:
show environment
```

Expected `show platform temperature` output (normal operation):

```
         Sensor    Temperature    High TH    Low TH    Crit High TH    Crit Low TH    Warning
---------------  -------------  ---------  --------  --------------  -------------  ---------
        CPU Core         42.0°C      95.0°C      N/A         102.0°C            N/A  False
         TMP75-1         32.5°C      70.0°C      N/A          80.0°C            N/A  False
         TMP75-2         33.0°C      70.0°C      N/A          80.0°C            N/A  False
```

---

### 10.3 Logging

```bash
# Show recent system log (equivalent to "show logging")
show logging

# Follow log in real time:
sudo tail -f /var/log/syslog

# Show logs from a specific service:
sudo journalctl -u pmon -n 100
sudo journalctl -u bgp -n 100

# Platform-specific daemon logs:
sudo journalctl -u wedge100s-i2c-poller -n 50
sudo journalctl -u wedge100s-bmc-poller -n 50
```

---

### 10.4 Quick Diagnostic Checklist

After initial setup, verify:

```bash
show system status                      # SYSTEM_READY should be "up"
docker ps | grep -v Exited              # All containers should be Up
show interfaces status | grep -v down   # Confirm expected ports are up
show platform temperature               # All temps < 70°C
show platform fan                       # All fans present and spinning
show platform psu                       # Both PSUs present and OK
show interfaces transceiver presence    # Transceivers detected in expected slots
show lldp neighbors                     # LLDP peers visible on connected ports
```

---

## Chapter 11 — Troubleshooting

### 11.1 SSH Not Responding After `config reload`

**Symptom:** SSH connection drops after `sudo config reload -y` and does not
return after 60 seconds.

**Cause:** The `interfaces-config.service` restarts the Linux `networking`
service, which tears down and recreates the `mgmt` VRF device. `eth0` loses
its VRF membership and `sshd`'s socket becomes bound to a stale interface index.

**Recovery (from serial console or SOL):**

```bash
# Re-enslave eth0 to mgmt VRF and restart sshd:
sudo ip link set eth0 master mgmt
sudo systemctl restart ssh

# Verify SSH is listening:
sudo ss -tlnp | grep sshd
# Expected: 0.0.0.0%mgmt:22
```

**Prevention:** The platform systemd drop-ins should handle this automatically.
If they are missing, see the SONiC Wedge100S L2 Setup Guide §4.

---

### 11.2 High CPU / SSH Latency on Fresh Install

**Symptom:** SSH response is very slow (2–5 second latency); `top` shows
`bgpd` or `zebra` consuming 40–90% CPU.

**Cause:** The default SONiC `LeafRouter` topology configures 32 BGP neighbors.
With no actual BGP peers connected, the BGP daemon continuously retries sessions
and processes timeouts, saturating the control-plane CPU.

**Fix:**

```bash
sudo config feature state bgp disabled
config save -y
```

Immediate relief without saving (for diagnosis):

```bash
docker stop bgp
```

---

### 11.3 Optical Port Will Not Come Up

**Symptom:** `show interfaces status Ethernet26/1` shows `Oper: down` despite
fiber being connected and the peer reporting signal.

**Step 1 — Check transceiver is detected:**

```bash
show interfaces transceiver presence Ethernet26/1
# Expected: Ethernet26/1    Present
```

If absent: reseat the transceiver. Check `sfp_25_present` in `/run/wedge100s/`
(index = panel_port - 1 = 25 for port 26).

**Step 2 — Check TX_DISABLE:**

```bash
show interfaces transceiver status Ethernet26/1
# Look for: TX disable status on lane N: True
```

If TX is disabled, restart pmon:

```bash
sudo systemctl restart pmon && sleep 20
show interfaces transceiver status Ethernet26/1
```

**Step 3 — Check FEC mismatch:**

SR4 and CWDM4 require RS-FEC (`rs`). LR4 typically requires no FEC (`none`).
Mismatched FEC between peers causes permanent link-down.

```bash
sudo config interface fec Ethernet26/1 rs     # for SR4/CWDM4
sudo config interface fec Ethernet27/1 none   # for LR4
```

**Step 4 — Check LP_MODE:**

```bash
cat /run/wedge100s/sfp_25_lpmode   # 0 = normal; 1 = low-power (laser off)
```

If `1`, the platform daemon has not deasserted LP_MODE yet. Restart pmon.

---

### 11.4 Port LED Rainbow Not Clearing

**Symptom:** All port LEDs display a cycling rainbow animation more than 30
seconds after SONiC boot.

**Cause:** The SYSCPLD LED control register (BMC i2c-12/0x31 register 0x3c)
was not set to `0x02` by the platform BMC daemon.

**Diagnosis:**

```bash
cat /run/wedge100s/syscpld_led_ctrl
# Expected: 2  (BCM LEDUP enabled)
# Bad:      224 (0xe0 = ONIE rainbow mode)
```

**Fix — trigger the LED init manually:**

```bash
echo 0x02 | sudo tee /run/wedge100s/syscpld_led_ctrl.set
sudo systemctl start wedge100s-bmc-poller.service
sleep 2
cat /run/wedge100s/syscpld_led_ctrl   # should now be 2
```

If the BMC daemon is not running:

```bash
sudo systemctl status wedge100s-bmc-poller.timer
sudo journalctl -u wedge100s-bmc-poller -n 30
```

---

### 11.5 Management Connectivity Lost When PortChannel Comes Up

**Symptom:** SSH to the switch works, but the development host (or your laptop)
loses connectivity to the Arista/EOS peer's management IP immediately after a
PortChannel or L3 interface comes up on the peer.

**Cause:** The peer uses a shared system MAC for all interfaces (common on EOS).
When the PortChannel comes up with an IP, the peer sends a gratuitous ARP that
travels back through the data-plane path and poisons the management LAN switch's
MAC table. Unicast traffic destined for the peer's management IP is forwarded
to the wrong switch port.

**Immediate recovery:**

```bash
# Clear the stale ARP entry on the affected host:
sudo arp -d <peer-mgmt-ip>
# ARP will re-learn and temporarily restore reachability until the next GARP
```

**Permanent fix:** Place data-plane ports into a different VLAN than the
management network (e.g., native VLAN 10 on data ports, VLAN 1 for management).
This isolates the broadcast domains so gratuitous ARPs cannot cross over.
See `tests/notes/lacp-mgmt-reachability-root-cause.md` for full analysis.

---

### 11.6 Transceivers Show No DOM Data

**Symptom:** `show interfaces transceiver eeprom --dom Ethernet26/1` returns
`N/A` for Rx power, temperature, and bias.

**Step 1 — Verify the EEPROM cache exists:**

```bash
ls -la /run/wedge100s/sfp_25_eeprom
# Should exist and be 256 bytes
```

If missing: the i2c daemon has not yet read this port's EEPROM, or the
transceiver was recently inserted. Wait up to 3 seconds and retry. If still
missing after a minute:

```bash
sudo journalctl -u wedge100s-i2c-poller -n 20
```

**Step 2 — Verify xcvrd has populated STATE_DB:**

```bash
sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|Ethernet26/1'
```

If empty: xcvrd has not polled this port yet. Restart pmon:

```bash
sudo systemctl restart pmon && sleep 20
show interfaces transceiver eeprom --dom Ethernet26/1
```

**Note:** Some Arista SR4 modules do not implement temperature or voltage
registers — `N/A` for those fields on those modules is expected and correct.

---

### 11.7 ZTP Loops or Never Completes

**Symptom:** The switch repeatedly reboots, or `show ztp status` shows
`IN-PROGRESS` for more than 10 minutes.

**Step 1 — Check what ZTP is trying to fetch:**

```bash
sudo journalctl -u sonic-ztp -n 50
```

Look for HTTP 404 (profile URL not found), DNS resolution failures, or
repeated retries.

**Step 2 — Check DHCP option 67 is being returned:**

```bash
sudo ip vrf exec mgmt tcpdump -i eth0 -n port 67 or port 68
```

**Step 3 — Abort and configure manually:**

```bash
sudo ztp disable
# Now configure the switch manually using sudo config commands
# Then save:
config save -y
```

---

### 11.8 `show platform temperature` Shows All N/A

**Symptom:** All TMP75 sensor readings show `N/A` or `0.0°C`.

**Cause:** The `wedge100s-bmc-daemon` has not yet run, or the BMC SSH key
is not provisioned.

**Check daemon status:**

```bash
systemctl status wedge100s-bmc-poller.timer
cat /run/wedge100s/thermal_1   # should be a millidegree integer like 32500
```

**Check BMC SSH connectivity:**

```bash
sudo ssh -i /etc/sonic/wedge100s-bmc-key \
    -o ConnectTimeout=5 \
    root@fe80::ff:fe00:1%usb0 'uptime'
```

If SSH fails with "Permission denied":

```bash
# Re-provision the SSH key via password auth:
ssh-copy-id -i /etc/sonic/wedge100s-bmc-key root@<bmc-ip>
# Password: 0penBmc
sudo systemctl restart wedge100s-bmc-poller.timer
```

---

## Appendix A — Port Name Quick Reference

| Panel Port | SONiC Interface | Alias (EOS-style) | BCM Lanes |
|------------|-----------------|-------------------|-----------|
| 1          | Ethernet0       | Ethernet1/1       | 117-120   |
| 2          | Ethernet4       | Ethernet2/1       | 113-116   |
| 3          | Ethernet8       | Ethernet3/1       | 125-128   |
| 4          | Ethernet12      | Ethernet4/1       | 121-124   |
| 5          | Ethernet16      | Ethernet5/1       | 5-8       |
| 6          | Ethernet20      | Ethernet6/1       | 1-4       |
| 7          | Ethernet24      | Ethernet7/1       | 13-16     |
| 8          | Ethernet28      | Ethernet8/1       | 9-12      |
| 9          | Ethernet32      | Ethernet9/1       | 21-24     |
| 10         | Ethernet36      | Ethernet10/1      | 17-20     |
| 11         | Ethernet40      | Ethernet11/1      | 29-32     |
| 12         | Ethernet44      | Ethernet12/1      | 25-28     |
| 13         | Ethernet48      | Ethernet13/1      | 37-40     |
| 14         | Ethernet52      | Ethernet14/1      | 33-36     |
| 15         | Ethernet56      | Ethernet15/1      | 45-48     |
| 16         | Ethernet60      | Ethernet16/1      | 41-44     |
| 17         | Ethernet64      | Ethernet17/1      | 53-56     |
| 18         | Ethernet68      | Ethernet18/1      | 49-52     |
| 19         | Ethernet72      | Ethernet19/1      | 61-64     |
| 20         | Ethernet76      | Ethernet20/1      | 57-60     |
| 21         | Ethernet80      | Ethernet21/1      | 69-72     |
| 22         | Ethernet84      | Ethernet22/1      | 65-68     |
| 23         | Ethernet88      | Ethernet23/1      | 77-80     |
| 24         | Ethernet92      | Ethernet24/1      | 73-76     |
| 25         | Ethernet96      | Ethernet25/1      | 85-88     |
| 26         | Ethernet100     | Ethernet26/1      | 81-84     |
| 27         | Ethernet104     | Ethernet27/1      | 93-96     |
| 28         | Ethernet108     | Ethernet28/1      | 89-92     |
| 29         | Ethernet112     | Ethernet29/1      | 101-104   |
| 30         | Ethernet116     | Ethernet30/1      | 97-100    |
| 31         | Ethernet120     | Ethernet31/1      | 109-112   |
| 32         | Ethernet124     | Ethernet32/1      | 105-108   |

---

## Appendix B — IOS-to-SONiC Command Mapping

| Cisco IOS Command | SONiC Equivalent |
|-------------------|-----------------|
| `show version` | `show version` |
| `show running-config` | `sudo sonic-cfggen -d --print-data 2>/dev/null` |
| `write memory` | `config save -y` |
| `show interfaces status` | `show interfaces status` |
| `show interfaces Gi0/1` | `show interfaces Ethernet0` |
| `show interfaces counters` | `show interfaces counters` |
| `clear counters` | `sonic-clear counters` |
| `show lldp neighbors` | `show lldp neighbors` |
| `show ip bgp summary` | `show ip bgp summary` |
| `show vlan brief` | `show vlan brief` |
| `show logging` | `show logging` |
| `show clock` | `show clock` |
| `show environment` | `show environment` |
| `interface Gi0/1` + `shutdown` | `sudo config interface shutdown Ethernet0` |
| `interface Gi0/1` + `no shutdown` | `sudo config interface startup Ethernet0` |
| `interface Gi0/1` + `ip address X.X.X.X Y.Y.Y.Y` | `sudo config interface ip add Ethernet0 X.X.X.X/PP` |
| `vlan 100` | `sudo config vlan add 100` |
| `ip route X.X.X.X Y.Y.Y.Y next-hop` | `sudo config route add prefix X.X.X.X/PP nexthop N.N.N.N` |
| `hostname myswitch` | `sudo config hostname myswitch` |
| `ntp server X.X.X.X` | `sudo config ntp add X.X.X.X` |
| `reload` | `sudo reboot` |
| `copy run start` | `config save -y` |
| `no shutdown` (interface) | `sudo config interface startup EthernetN` |
| `shutdown` (interface) | `sudo config interface shutdown EthernetN` |
| `spanning-tree mode pvst` | `sudo config spanning-tree mode pvst` |
| `spanning-tree vlan 1` | `sudo config spanning-tree vlan add 1` |

---

## Appendix C — Platform-Specific Notes

### Management Architecture

The Wedge 100S-32X uses a split management architecture:

- **x86 host (SONiC):** Front-panel ports (Ethernet0–Ethernet124), management
  ethernet (eth0), and CPLD (PSU presence, SYS LED)
- **OpenBMC (Aspeed AST2400):** Fan speed control, thermal sensors (7× TMP75),
  PSU telemetry (PMBus), port LED chain direction

The BMC communicates with SONiC via a USB CDC-Ethernet link (usb0). All
BMC-side sensors are polled by the `wedge100s-bmc-daemon` and cached in
`/run/wedge100s/` for the SONiC platform API.

### Port LED Behavior

During ONIE installation and for approximately 15 seconds after first SONiC
boot, the port LEDs display a "rainbow" animation. This is normal behavior:
the SYSCPLD (controlled from the BMC) remains in its ONIE test mode until
the SONiC platform daemon writes the enable register. The rainbow clears
automatically within 15 seconds of a healthy boot.

### BCM Serial Interface (SI) Settings

The Broadcom Tomahawk ASIC on this platform uses default SerDes settings
(`NPU_SI_SETTINGS_DEFAULT`). For most SR4 optical connections this is
sufficient. For LR4 and some CWDM4 optics that require higher-amplitude
SerDes output, per-port TXAMP settings in the BCM config file may be needed.
Consult the SONiC Wedge100S Optics Setup Guide if optical ports fail to
link up despite correct fiber and FEC settings.

### I2C Bus Safety

The QSFP I2C bus (via CP2112 USB-HID bridge) is exclusively managed by the
`wedge100s-i2c-daemon`. Do not run `i2cget`, `i2cset`, or `i2cdetect` while
`pmon` is running — concurrent access corrupts EEPROM cache files. All
QSFP/SFP data is safely accessible through the `/run/wedge100s/` sysfs files
or via the standard `show interfaces transceiver` CLI commands.

---

### Platform Runtime State Files — `/run/wedge100s/`

All platform hardware state is surfaced as plain-text files under `/run/wedge100s/`
(tmpfs, recreated each boot). These files are the safe, read-only interface for
operators and scripts — they are written exclusively by the platform daemons and
never require direct I2C access.

#### QSFP/SFP Files (written by `wedge100s-i2c-daemon`, every 3 s)

| File | Content | Notes |
|------|---------|-------|
| `sfp_N_present` (N=0–31) | `"0"` or `"1"` | 1 = transceiver physically present |
| `sfp_N_eeprom` (N=0–31) | 256 bytes binary (SFF page 0) | Written on insertion; deleted on removal |
| `sfp_N_lpmode` (N=0–31) | `"0"` or `"1"` | 0 = low-power mode deasserted (normal) |
| `syseeprom` | 8192 bytes binary (ONIE TlvInfo) | Written once at first boot |

Port index N maps to panel port N+1 (0-based). Port 0 = panel port 1 = `Ethernet1/1`.

```bash
# Check presence for all ports:
for i in $(seq 0 31); do
  p=$(cat /run/wedge100s/sfp_${i}_present 2>/dev/null)
  [ "$p" = "1" ] && echo "Port $((i+1)) (Ethernet$((i*4+1))/1): present"
done

# Read LP_MODE for port 26 (Ethernet26/1 = sfp index 25):
cat /run/wedge100s/sfp_25_lpmode    # 0 = normal, 1 = low-power
```

#### Thermal Files (written by `wedge100s-bmc-daemon`, every 10 s)

| File | BMC Source | Unit |
|------|-----------|------|
| `thermal_1` | TMP75 on BMC i2c-3 addr 0x48 | millidegrees C |
| `thermal_2` | TMP75 on BMC i2c-3 addr 0x49 | millidegrees C |
| `thermal_3` | TMP75 on BMC i2c-3 addr 0x4a | millidegrees C |
| `thermal_4` | TMP75 on BMC i2c-3 addr 0x4b | millidegrees C |
| `thermal_5` | TMP75 on BMC i2c-3 addr 0x4c | millidegrees C |
| `thermal_6` | TMP75 on BMC i2c-8 addr 0x48 | millidegrees C |
| `thermal_7` | TMP75 on BMC i2c-8 addr 0x49 | millidegrees C |

```bash
# Read all temperatures in Celsius:
for i in $(seq 1 7); do
  raw=$(cat /run/wedge100s/thermal_$i 2>/dev/null)
  echo "TMP75-$i: $(echo "scale=1; $raw / 1000" | bc)°C"
done
```

#### Fan Files (written by `wedge100s-bmc-daemon`, every 10 s)

| File | Content |
|------|---------|
| `fan_present` | Bitmask; bit N=1 means tray N+1 **absent**; `0` = all 5 trays present |
| `fan_N_front` (N=1–5) | Front-rotor RPM for fan tray N |
| `fan_N_rear` (N=1–5) | Rear-rotor RPM for fan tray N |

```bash
cat /run/wedge100s/fan_present       # 0x00 = all present
cat /run/wedge100s/fan_1_front       # e.g. 12500 RPM
```

#### PSU Files (written by `wedge100s-bmc-daemon`, every 10 s)

| File | PMBus Register | Description |
|------|---------------|-------------|
| `psu_1_vin` | 0x88 READ_VIN | PSU1 AC input voltage (raw LINEAR11) |
| `psu_1_iin` | 0x89 READ_IIN | PSU1 AC input current (raw LINEAR11) |
| `psu_1_iout` | 0x8c READ_IOUT | PSU1 DC output current (raw LINEAR11) |
| `psu_1_pout` | 0x96 READ_POUT | PSU1 DC output power (raw LINEAR11) |
| `psu_2_*` | (same) | PSU2 telemetry |

Values are raw PMBus LINEAR11 16-bit integers. The platform API decodes them
to SI units; direct file reads require LINEAR11 decoding.

#### Other Control Files

| File | Written by | Content |
|------|-----------|---------|
| `syscpld_led_ctrl` | bmc-daemon (read) | syscpld register 0x3c; `2` = BCM LEDUP enabled (normal) |
| `syscpld_led_ctrl.set` | Platform API (write request) | Write desired register value; daemon consumes and removes |
| `qsfp_int` | bmc-daemon | BMC GPIO31; `0` = QSFP interrupt asserted |
| `qsfp_led_position` | bmc-daemon (once at boot) | Board strap; `1` on this hardware |