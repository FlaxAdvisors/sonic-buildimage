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

Assign private /30 link addresses:
- Switch `usb0`: `169.254.100.1/30`
- BMC `usb0`: `169.254.100.2/30`

Both ends can be configured at boot:
- Switch: postinst or platform-init assigns IP
- BMC: BMC config or systemd-networkd assigns IP

Once IP is assigned, BMC commands can be sent via SSH or a lightweight TCP socket
instead of the blocking TTY path (`/dev/ttyACM0`), eliminating the 140s timeout
on SSH when BMC is slow to respond.

## UDC

BMC uses `ast-vhub.0` (Aspeed virtual hub) as the USB device controller — standard
for ASPEED AST2500/AST2600 BMCs.
