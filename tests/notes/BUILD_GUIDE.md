# Building the SONiC ONIE Installer (.bin) for Wedge 100S-32X

## Overview

The build produces `target/sonic-broadcom.bin` — an ONIE-compatible installer
that can be loaded via ONIE's install mechanism on the Wedge 100S-32X.

The build runs inside Docker containers ("sonic-slave"). There are **two build
passes** required:

1. **Bookworm pass** (`BLDENV=bookworm`): compiles packages, docker images,
   and python wheels. Outputs go to `target/debs/bookworm/` and `target/docker-*.gz`.
2. **Trixie pass** (`BLDENV=trixie`): builds runtime debs into `target/debs/trixie/`
   and assembles the final `.bin` installer.

The runtime image root filesystem is **trixie-based** (`IMAGE_DISTRO := trixie`
is hardcoded in `slave.mk:73`), so both passes are required.

## Build Host Prerequisites

| Resource       | Value                          | Required            |
|----------------|--------------------------------|---------------------|
| Docker         | 26.1.3                         | >= 20.10.10         |
| docker group   | current user in docker group   | yes                 |
| Disk free      | 5.1 TB on /export              | ~100 GB minimum     |
| RAM            | 376 GB                         | 8 GB minimum        |
| CPUs           | 80 (40 physical)               | more = faster       |
| overlay module | `sudo modprobe overlay`        | required            |
| j2 (jinja CLI) | `pip3 install jinjanator`      | required            |
| Python (host)  | 3.x                            | 3.x                 |

## Build Steps

### 1. Ensure submodules are initialized

```bash
make init
```

Runs `git submodule update --init --recursive`. Safe to re-run.

### 2. Configure platform

```bash
make configure PLATFORM=broadcom
```

Writes `broadcom` to `.platform` and `amd64` to `.arch`. Skip if already done.

### 3. Ensure Accton platform modules are included

In `platform/broadcom/rules.mk`, uncomment the Accton include:

```makefile
include $(PLATFORM_PATH)/platform-modules-accton.mk
```

All other `platform-modules-*.mk` lines can remain commented out if you only
need wedge100s.

### 4. Wedge100S-only Accton build (optional optimization)

In `platform/broadcom/platform-modules-accton.mk`, the wedge100s module is
configured as a standalone `SONIC_DPKG_DEBS` target (not an `add_extra_package`
off AS7712). All other Accton platform variables and targets are commented out.
This avoids building debs for platforms we don't need.

### 5. Build the ONIE installer image

```bash
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y \
     SONIC_IMAGE_VERSION=wedge100s-1.0 \
     target/sonic-broadcom.bin
```

#### Build variables

| Variable             | Description                              | Value       |
|----------------------|------------------------------------------|-------------|
| SONIC_BUILD_JOBS     | Parallel make jobs inside container      | 40          |
| BUILD_SKIP_TEST      | Skip unit tests (faster, avoids hangs)   | y           |
| NOTRIXIE             | **Must be 0** — trixie pass builds .bin  | 0           |
| NOBOOKWORM           | Must be 0 (default) — builds dockers     | 0           |
| SONIC_IMAGE_VERSION  | Override version string in image         | wedge100s-1.0 |
| SONIC_BUILD_MEMORY   | Docker memory limit                      | (unlimited) |

#### Critical: NOTRIXIE must be 0

The outer `Makefile` defaults to `NOTRIXIE ?= 1`, which **skips the trixie
build pass**. Without it:

- The bookworm pass builds docker images and wheels (target: `bookworm` in slave.mk)
- But the `.bin` assembly depends on `target/debs/trixie/` debs
- The bookworm pass target is just `bookworm` — it does NOT build the installer
- Only the trixie pass targets `$@` (the actual `target/sonic-broadcom.bin`)

Setting `NOTRIXIE=0` enables the trixie pass which builds runtime debs and
assembles the final image.

#### Image versioning

The version string comes from `functions.sh:sonic_get_version()`:

- On a tagged commit: `<tag>`
- On a branch: `<branch>.<BUILD_NUMBER>-<short-sha>`
- With uncommitted changes: appends `-dirty-<timestamp>`
- Override with `SONIC_IMAGE_VERSION=<string>` to set explicitly

### What happens during the build

1. **Bookworm pass** (BLDENV=bookworm, target=`bookworm`):
   - Builds sonic-slave-bookworm Docker container (first run only)
   - Compiles all SONiC packages → `target/debs/bookworm/`
   - Builds python wheels → `target/python-wheels/bookworm/`
   - Builds runtime Docker images → `target/docker-*.gz`

2. **Trixie pass** (BLDENV=trixie, target=`target/sonic-broadcom.bin`):
   - Builds sonic-slave-trixie Docker container (first run only)
   - Compiles runtime debs → `target/debs/trixie/`
   - Runs `build_debian.sh` — assembles Debian trixie root filesystem
   - Runs `build_image.sh` + `onie-mk-demo.sh` — wraps rootfs into ONIE installer

**Expected output:** `target/sonic-broadcom.bin` (~1.5–2 GB)

**Expected duration:** 2–4 hours clean build on 40-core host. Subsequent builds
with warm caches are faster.

## Building Just the Platform .deb (faster iteration)

```bash
make target/debs/bookworm/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Deploy directly:

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

### `make clean` fails with "j2: command not found"
```bash
pip3 install jinjanator
```
`make clean` runs inside the slave container, which requires `j2` to build.
Manual cleanup alternative: `rm -rf target/ fsroot*`

### "Nothing to be done for bookworm" but no .bin exists

The bookworm pass only builds docker images — it does NOT produce the `.bin`.
Ensure `NOTRIXIE=0` is set so the trixie pass runs and assembles the installer.

### `sonic_utilities` test hangs at ~50%

The test `test_get_portchannel_retry_count_timeout` in sonic-utilities hangs
and gets killed by `BUILD_PROCESS_TIMEOUT`. This is an upstream issue.
Use `BUILD_SKIP_TEST=y` to skip all unit tests.

### Missing `target/debs/trixie/*.deb`

The runtime image is trixie-based (`IMAGE_DISTRO := trixie` in `slave.mk:73`).
If `NOTRIXIE=1` (default), trixie debs are never built and the `.bin` target
fails with missing dependencies. Set `NOTRIXIE=0`.

### sonic-slave Docker image fails to build

Check Docker version (need >= 20.10.10 for bookworm/trixie). Also ensure:
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
# make clean requires the slave container; manual cleanup:
rm -rf target/ fsroot*
# Then re-run configure to recreate directory structure:
make configure PLATFORM=broadcom
```

### `make configure` must be re-run after cleaning

`rm -rf target/` removes the directory structure that `make configure` creates.
Without it, the build fails with "No such file or directory" for log files.
Always run `make configure PLATFORM=broadcom` after a manual clean.
