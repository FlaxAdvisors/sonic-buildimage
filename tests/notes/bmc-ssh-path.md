# BMC SSH Path — Implementation Notes

**Date:** 2026-03-23
**Result:** SSH-only bmc.py over IPv6 link-local confirmed working from both host and pmon.

## Design

- **Transport:** SSH over USB-CDC-Ethernet (usb0 on both switch and BMC)
- **Target:** `root@fe80::ff:fe00:1%usb0` — IPv6 link-local, auto-derived from
  fixed BMC usb0 MAC `02:00:00:00:00:01`. No IP address configuration needed;
  only `ip link set usb0 up` required.
- **Key:** `/etc/sonic/wedge100s-bmc-key` (ed25519) — accessible inside pmon
  because `/etc/sonic` is already bind-mounted into the container.
- **No TTY fallback at runtime.** `send_command()` returns `None` on SSH failure.
  TTY code is retained only in `provision_ssh_key()` for one-time bootstrap.

## Key Provisioning

postinst runs `bmc.provision_ssh_key()` which:
1. Generates `/etc/sonic/wedge100s-bmc-key` (ed25519) if absent.
2. Pushes the pubkey to BMC `/home/root/.ssh/authorized_keys` via TTY.
3. Copies to `/mnt/data/etc/authorized_keys` — survives BMC reboots (jffs2).
BMC also runs `/mnt/data/etc/rc.local` at boot which restores authorized_keys.

## openssh-client in pmon

- `dockers/docker-platform-monitor/Dockerfile.j2` now includes `openssh-client`
  in the apt-get install block — takes effect on next clean image build.
- For the deployed image: rebuilt manually by pulling the image from the switch,
  adding a thin layer (apt-get install openssh-client), and pushing back.
  Image saved at: tmp/docker-platform-monitor-ssh.tar.gz

## usb0 Persistence (switch side)

`/etc/systemd/network/10-usb0.network` (installed by postinst):
```ini
[Match]
Name=usb0

[Link]
RequiredForOnline=no
```
No IPv4 assignment — IPv6 link-local auto-configures from MAC.

## Verified on hardware 2026-03-23

- `docker exec pmon python3 -c "from sonic_platform import bmc; print(bmc.send_command('uptime'))"` → uptime from BMC in < 1s
- 49 regression tests pass (stage_03 through stage_06)
