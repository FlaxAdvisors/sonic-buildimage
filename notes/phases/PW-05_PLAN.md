# PW-05 — Streaming Telemetry: Plan

## Problem Statement

SONiC ships with a `gnmi` container that exposes a gNMI (gRPC Network Management Interface)
server on TCP port 8080 (insecure) and 9339 (TLS). This provides streaming telemetry
subscriptions over gRPC for interface counters, platform sensors, and other YANG-modeled data.

The `src/sonic-gnmi` submodule is present and built into standard SONiC images. No
platform-specific code is required for gNMI — it reads from Redis (CONFIG_DB, COUNTERS_DB,
STATE_DB) and does not call platform APIs directly.

This phase is **validation only**: confirm that gNMI subscriptions return valid data
for the Wedge 100S-32X, particularly for interface counters and platform sensor paths
that rely on the custom platform code implemented in prior phases.

## Current State

- `sonic-gnmi` submodule is present at `src/sonic-gnmi` with full source
- The `gnmi` container is included in the standard SONiC broadcom image
- No Wedge 100S-specific configuration of gNMI is needed
- The `gnmi_cli` or `gnmic` client tool is needed on the management host for testing

## Proposed Approach

### Step 1: Verify gnmi container is running

```bash
ssh admin@192.168.88.12 docker ps | grep gnmi
```

If the container is not present in the running image, it may need to be added to the
`docker_image_names` list, but this is not expected to be necessary.

### Step 2: Test interface counter subscription

Use `gnmic` (install via `go install github.com/karimra/gnmic@latest` or package manager)
from the management host or SONiC itself:

```bash
gnmic -a 192.168.88.12:8080 --insecure subscribe \
  --path '/interfaces/interface[name=Ethernet0]/state/counters' \
  --mode stream --stream-mode sample --sample-interval 10s
```

Expected: JSON-encoded counter values updating every 10 seconds.

### Step 3: Test platform component subscription

```bash
gnmic -a 192.168.88.12:8080 --insecure subscribe \
  --path '/platform/components' \
  --mode once
```

Expected: Returns fan, PSU, and thermal component data sourced from the custom
`sonic_platform` package.

### Step 4: Test YANG model coverage

Identify which YANG paths are supported on this platform:
```bash
gnmic -a 192.168.88.12:8080 --insecure capabilities
```

### Files to Change

None expected. This is validation only. If a gNMI configuration file is required
(e.g., `gnmi_config.json`), it would go in:
- `device/accton/x86_64-accton_wedge100s_32x-r0/gnmi_config.json`

But no such file is expected to be needed.

## Acceptance Criteria

- `gnmi` container is running and listening on port 8080
- Subscribe to `/interfaces/interface[name=Ethernet0]/state/counters` returns valid counter data
- Subscribe to `/platform/components` returns fan, PSU, and thermal data
- Data updates at the configured sample interval
- No gRPC errors or stream disconnections during a 60-second subscription

## Risks

- **TLS**: Production gNMI requires TLS certificates. The insecure port 8080 is sufficient
  for lab validation but is not suitable for production.
- **YANG path availability**: Not all OpenConfig YANG paths are implemented in SONiC gNMI.
  The interface counter path is well-tested; platform paths may have gaps.
- **Platform data dependency**: `/platform/components` data quality depends on prior phases
  (thermal, fan, PSU). If a phase is incomplete, those component values will be missing or
  show as N/A.
- **gnmic client**: Must be installed on the management host or inside SONiC's shell.
  The `gnmi_cli` binary may be available inside the `gnmi` container.
