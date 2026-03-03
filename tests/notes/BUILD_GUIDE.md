# Building the SONiC ONIE Installer (.bin) for Wedge 100S-32X

## Overview

The build produces `target/sonic-broadcom.bin` — an ONIE-compatible installer
that can be loaded via ONIE's install mechanism on the Wedge 100S-32X.

The entire build runs inside a Docker container ("sonic-slave-bookworm").
The host machine provides Docker, disk, and CPU — everything else is
containerized.

## Current Build Host Status

| Resource       | Value                          | Required            |
|----------------|--------------------------------|---------------------|
| Docker         | 26.1.3                         | >= 20.10.10         |
| docker group   | yes                            | yes                 |
| Disk free      | 5.1 TB on /export              | ~100 GB minimum     |
| RAM            | 376 GB                         | 8 GB minimum        |
| CPUs           | 80                             | more = faster       |
| overlay module | loaded                         | required            |
| j2 (jinja CLI) | **MISSING**                    | required            |
| Python (host)  | 3.8.10                         | 3.x                 |
| .platform      | broadcom (already configured)  | broadcom            |
| .arch          | amd64 (already configured)     | amd64               |

## Prerequisites to Fix

### Install jinjanator (provides `j2` command)

```bash
pip3 install jinjanator
# verify:
j2 --version
```

This was the cause of the `make clean` failure — `scripts/build_mirror_config.sh`
calls `j2` to template mirror configs. It's needed for slave container builds too.

## Build Steps

### 1. Ensure submodules are initialized

```bash
make init
```

This runs `git submodule update --init --recursive`. Already done if submodules
are populated, but safe to re-run.

### 2. Configure platform (already done)

```bash
make configure PLATFORM=broadcom
```

Writes `broadcom` to `.platform` and `amd64` to `.arch`. These files already
exist in the working tree from a previous configure, so this step can be skipped.

### 3. Ensure Accton platform modules are included in the build

Line 11 of `platform/broadcom/rules.mk` is currently **commented out**:

```makefile
#include $(PLATFORM_PATH)/platform-modules-accton.mk
```

This must be uncommented for the Wedge 100S platform .deb to be built and
included in the image:

```makefile
include $(PLATFORM_PATH)/platform-modules-accton.mk
```

**Note:** This will also build all other Accton platform modules (AS7712, AS5712,
etc.) as extra packages off the AS7712 primary build. They're bundled into the
image and only the matching one gets installed at first boot based on platform
detection.

### 4. Build the ONIE installer image

```bash
make SONIC_BUILD_JOBS=16 target/sonic-broadcom.bin
```

| Variable            | Description                              | Suggested   |
|---------------------|------------------------------------------|-------------|
| SONIC_BUILD_JOBS    | Parallel make jobs inside container      | 16 (of 80)  |
| SONIC_BUILD_MEMORY  | Docker memory limit                      | (unlimited) |
| NOSTRETCH/NOBUSTER  | Skip old Debian versions (default: 1)    | leave as-is |
| NOBOOKWORM          | Must be 0 (default) for bookworm build   | 0           |

**Expected output:** `target/sonic-broadcom.bin` (~1.5–2 GB)

**Expected duration:** 2–4 hours for a clean build on this hardware. Subsequent
builds with warm caches are faster.

### What happens during the build

1. **sonic-slave container** — Docker image is built from `sonic-slave-bookworm/`
   Dockerfile if it doesn't exist. This installs all build toolchains.
2. **Packages** — All SONiC components are compiled: swss, syncd, database,
   platform modules, kernel modules, etc. Outputs go to `target/debs/bookworm/`.
3. **Docker images** — Runtime containers (docker-database, docker-swss,
   docker-syncd-brcm, docker-platform-monitor, etc.) are built and saved as
   `.gz` archives in `target/`.
4. **Root filesystem** — `build_debian.sh` assembles a Debian bookworm rootfs
   with all packages and docker images installed.
5. **ONIE installer** — `build_image.sh` + `onie-mk-demo.sh` wraps the rootfs
   into a self-extracting ONIE installer binary.

## Building Just the Platform .deb (faster iteration)

If you only need to rebuild the platform module (not the full image):

```bash
make target/debs/bookworm/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Then deploy directly:

```bash
scp target/debs/bookworm/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

## Deploying via ONIE

### 1. Get the switch into ONIE Install mode

From a running SONiC:
```bash
sudo onie-select -i
sudo reboot
```

Or from OpenBMC serial console, interrupt U-Boot and select ONIE Install.

Or from ONIE itself:
```bash
onie-discovery-stop    # stop auto-discovery
```

### 2. Install the image

**Option A: HTTP/TFTP server** — Place the .bin on a server reachable from ONIE:
```bash
# From ONIE shell:
onie-nos-install http://<server>/sonic-broadcom.bin
```

**Option B: SCP from build host** — Copy the .bin to ONIE and run locally:
```bash
# From build host:
scp target/sonic-broadcom.bin root@192.168.88.12:/tmp/
# From ONIE shell:
onie-nos-install /tmp/sonic-broadcom.bin
```

**Option C: USB** — Copy the .bin to a FAT32 USB drive as
`onie-installer-x86_64` (no extension). ONIE auto-discovers it.

### 3. Post-install

After ONIE installs the image, the switch reboots into SONiC. First boot:
- Platform detection runs (`sonic_platform` package for wedge100s-32x loads)
- Services start (database, swss, syncd, pmon, etc.)
- Default credentials: `admin` / `YourPaSsWoRd`

## Troubleshooting

### `j2: command not found`
```bash
pip3 install jinjanator
```

### sonic-slave Docker image fails to build
Check Docker version (need >= 20.10.10 for bookworm). Also ensure:
```bash
sudo modprobe overlay
```

### Build runs out of disk
```bash
df -h /export/sonic/
# Need ~100 GB free for a clean build
```

### Rebuilding the slave container
```bash
make sonic-slave-build
# Or for interactive debugging:
make sonic-slave-bash
```

### Cleaning build artifacts
```bash
# Since make clean requires the slave container, do it manually:
rm -rf target/ fsroot*
```
