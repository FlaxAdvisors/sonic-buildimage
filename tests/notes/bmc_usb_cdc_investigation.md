# BMC USB CDC-ECM Investigation

**Date:** 2026-03-23
**Result:** CDC-ECM confirmed available on both sides. Task 8 (4d implementation) PROCEEDS.

## USB device

```
Bus 001 Device 006: ID 0525:a4aa Netchip Technology, Inc.
  iProduct: CDC Composite Gadget
  iManufacturer: Linux 4.1.51 with ast-vhub
  bFunctionSubClass 6 = Ethernet Networking  ← CDC-ECM
  bFunctionSubClass 2 = ACM (serial/ttyACM0)
```

The BMC exports a CDC Composite Gadget with **both** CDC-ECM and CDC-ACM interfaces.

## Interface state (verified on hardware 2026-03-23)

**Switch side:**
- Interface: `usb0`
- State: DOWN (no IP assigned)
- MAC: `02:00:00:00:00:02`

**BMC side:**
- Interface: `usb0`
- State: UP
- MAC: `02:00:00:00:00:01`
- IPs: IPv6 link-local only (`fe80::ff:fe00:1/64`, `fe80::1/64`)
- No IPv4 assigned

## Plan for implementation (Task 8)

Do not mess with private addresses - use IPv6LL:
- Switch `usb0`: `fe80::ff:fe00:2%usb0`
- BMC `usb0`: `fe80::ff:fe00:1%usb0`

For example:

```bash
admin@hare-lorax:~$ ssh root@fe80::ff:fe00:1%usb0
The authenticity of host 'fe80::ff:fe00:1%usb0 (fe80::ff:fe00:1%usb0)' can't be established.
ED25519 key fingerprint is SHA256:FxC6WPXnMC9iMre2DUbx7JCHC6F1sMEux6qSbYzUEbU.
This key is not known by any other names.
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
Warning: Permanently added 'fe80::ff:fe00:1%usb0' (ED25519) to the list of known hosts.
root@fe80::ff:fe00:1%usb0's password:
Last login: Mon Mar 23 12:11:45 2026
root@hare-lorax-bmc:~# logout
Connection to fe80::ff:fe00:1%usb0 closed.
```

Both ends can be configured at boot:
- Use the /dev/ttySCM0 to push the sonic ssh key 

Once IP is assigned, BMC commands can be sent via SSH or a lightweight TCP socket
instead of the blocking TTY path (`/dev/ttyACM0`), eliminating the 140s timeout
on SSH when BMC is slow to respond.

## UDC

BMC uses `ast-vhub.0` (Aspeed virtual hub) as the USB device controller — standard
for ASPEED AST2500/AST2600 BMCs.
