# PW-05 — Streaming Telemetry: Test Plan

## Overview

Validate that the SONiC gNMI server on Wedge 100S-32X returns valid streaming
telemetry data for interface counters and platform sensor paths.

## Required Hardware State

- SONiC running on Wedge 100S-32X (`192.168.88.12`)
- At least one port operationally up (Ethernet16, 32, 48, or 112 — DAC ports)
- Management access from test host to port 8080 on `192.168.88.12`
- `gnmic` or `gnmi_cli` available on the management host

## Install gnmic (if not present)

```bash
# On the management host (Linux)
go install github.com/karimra/gnmic@latest
# or: curl -sL https://get-gnmic.kmrd.dev | bash
```

Alternatively, use `gnmi_cli` inside the SONiC `gnmi` container:
```bash
ssh admin@192.168.88.12 docker exec gnmi gnmi_cli ...
```

## Dependencies

- Platform phases (thermal, fan, PSU) should be complete for full `/platform/components` coverage
- No dependency on PW-03 or PW-04

---

## Test Actions

### T1: gnmi container is running

```bash
ssh admin@192.168.88.12 docker ps --format '{{.Names}}' | grep gnmi
```

**Pass:** `gnmi` appears in the output.
**Fail:** Container not present — check image build includes gnmi.

### T2: gRPC port is listening

```bash
nc -z -w 3 192.168.88.12 8080 && echo "OPEN" || echo "CLOSED"
```

**Pass:** Prints `OPEN`.

### T3: Interface counter subscription — one sample

```bash
gnmic -a 192.168.88.12:8080 --insecure get \
  --path '/interfaces/interface[name=Ethernet0]/state/counters'
```

**Pass:** Returns a JSON response containing `in-octets`, `out-octets`, and related fields.
**Fail:** gRPC error, connection refused, or empty response.

### T4: Interface counter stream — 30-second subscription

```bash
timeout 35 gnmic -a 192.168.88.12:8080 --insecure subscribe \
  --path '/interfaces/interface[name=Ethernet0]/state/counters' \
  --mode stream --stream-mode sample --sample-interval 10s \
  2>&1 | head -50
```

**Pass:** At least 3 update messages received (one per 10-second interval) with
non-zero `in-octets` or `out-octets` values on the Ethernet0 port.

### T5: Platform fan data via gNMI

```bash
gnmic -a 192.168.88.12:8080 --insecure get \
  --path '/platform/components/component[name=FAN-1]/state'
```

**Pass:** Returns speed, status, or presence data for the fan component.
**Note:** Component name format may differ; explore with `get --path '/platform/components'`
first.

### T6: Platform thermal data via gNMI

```bash
gnmic -a 192.168.88.12:8080 --insecure get \
  --path '/platform/components/component[name=TEMP_1]/state/temperature/instant'
```

**Pass:** Returns a numeric temperature value consistent with `show platform temperature`.

### T7: gNMI capabilities

```bash
gnmic -a 192.168.88.12:8080 --insecure capabilities
```

**Pass:** Returns a list of supported YANG models including `openconfig-interfaces` and
`openconfig-platform` (or their SONiC equivalents).

### T8: No stream errors over 60 seconds

```bash
timeout 65 gnmic -a 192.168.88.12:8080 --insecure subscribe \
  --path '/interfaces/interface[name=Ethernet0]/state/counters' \
  --mode stream --stream-mode sample --sample-interval 5s 2>&1 | grep -i error
```

**Pass:** No error lines printed during 60-second subscription.

---

## Pass/Fail Criteria Summary

| Test | Pass condition |
|---|---|
| T1 | `gnmi` container running |
| T2 | Port 8080 is open |
| T3 | Interface counter GET returns counter fields |
| T4 | Stream delivers at least 3 samples in 30 s |
| T5 | Fan component data returned |
| T6 | Thermal temperature value returned |
| T7 | Capabilities include interface and platform models |
| T8 | No gRPC errors during 60-second stream |

T1–T4 are required for phase completion. T5–T8 are quality validation.
