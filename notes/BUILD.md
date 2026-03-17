# Build and Deploy Guide — Accton Wedge 100S-32X SONiC Port

Platform: `x86_64-accton_wedge100s_32x-r0`
Branch: `wedge100s`
Target deb: `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`

---

## 1. Prerequisites

### Host requirements

| Resource | Minimum | Notes |
|---|---|---|
| Docker | >= 20.10.10 | Must be running; user in `docker` group |
| Disk free | ~100 GB | Full image; ~50 GB for deb-only builds |
| RAM | 8 GB | More is faster; swap not a substitute |
| CPUs | Any | `SONIC_BUILD_JOBS=40` only helps with many cores |
| `overlay` kernel module | loaded | `sudo modprobe overlay` |
| `jinjanator` (j2 CLI) | installed | `pip3 install jinjanator` |
| Python | 3.x | Host-side only; build runs in Docker |

### One-time setup

```bash
# Clone all git submodules (required before any build)
make init

# Configure platform — writes .platform and .arch, creates target/ directory tree
make configure PLATFORM=broadcom
```

`make configure` must be re-run after any `rm -rf target/` or `make distclean`.

### Verify the Accton include is active

In `platform/broadcom/rules.mk`, confirm this line is **not** commented out:

```makefile
include $(PLATFORM_PATH)/platform-modules-accton.mk
```

All other Accton platform entries in `platform-modules-accton.mk` are commented
out — only the `ACCTON_WEDGE100S_32X` entries are active.

---

## 2. Platform .deb Build (fast iteration)

Use this for every day code iteration. Builds in ~5–10 minutes on a warm cache.

```bash
make SONIC_BUILD_JOBS=40 target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

Output: `target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`

The deb depends on `linux-headers`, `linux-headers-common`, and
`pddf-platform-module`. Those are fetched or built automatically by the
dependency chain defined in `platform/broadcom/platform-modules-accton.mk`.

---

## 3. Full ONIE Image Build

Produces `target/sonic-broadcom.bin` — the ONIE-compatible installer (~1–2 GB).
Expect 2–4 hours on a 40-core host for a clean build.

### Build passes

The image build runs two sequential passes, each in its own Docker slave
container:

| Pass | BLDENV | Slave container | Produces |
|---|---|---|---|
| 1 | bookworm | sonic-slave-bookworm | `target/debs/bookworm/`, `target/docker-*.gz` |
| 2 | trixie | sonic-slave-trixie | `target/debs/trixie/`, `target/sonic-broadcom.bin` |

The runtime root filesystem is trixie (`IMAGE_DISTRO := trixie` in `slave.mk`).
Bookworm service Docker images are bundled unchanged into the trixie installer.

### Build command

```bash
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y target/sonic-broadcom.bin
```

**Do not set `NOTRIXIE=1`** — the top-level `Makefile` defaults to
`NOTRIXIE ?= 1`, which skips the trixie pass and the `.bin` is never assembled.
Leave it at the default `0` or explicitly pass `NOTRIXIE=0`.

**Do not set `NOBOOKWORM=1`** for full image builds — the bookworm pass builds
37 Docker service images required by the installer. `NOBOOKWORM=1` is only safe
for isolated deb-only builds with `BLDENV=trixie` set explicitly.

### Optional: set image version string

```bash
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y \
     SONIC_IMAGE_VERSION=wedge100s-1.0 \
     target/sonic-broadcom.bin
```

Without this, the version is derived from git tag/branch/SHA automatically.

---

## 4. Deploy Platform .deb to Target

Use when the full ONIE image does not need to be reinstalled — only platform
module code changed.

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x*.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 sudo systemctl stop pmon
ssh admin@192.168.88.12 sudo dpkg -i sonic-platform-accton-wedge100s-32x*.deb
ssh admin@192.168.88.12 sudo systemctl start pmon
```

**Never `docker rm -f pmon`** while xcvrd may be running — it hangs the I2C bus
and requires a power cycle. Always use `systemctl stop pmon`.

---

## 5. Deploy Full Image via ONIE

### Get the switch into ONIE Install mode

From a running SONiC:

```bash
sudo onie-select -i
sudo reboot
```

### Install options

**Option A — HTTP (recommended):** Serve the `.bin` from a web server reachable
from ONIE, then from the ONIE shell:

```bash
onie-nos-install http://<server>/sonic-broadcom.bin
```

**Option B — SCP to ONIE:**

```bash
# From build host:
scp target/sonic-broadcom.bin root@192.168.88.12:/tmp/
# From ONIE shell:
onie-nos-install /tmp/sonic-broadcom.bin
```

**Option C — USB:** Copy the `.bin` to a FAT32 USB drive named
`onie-installer-x86_64` (no extension). ONIE auto-discovers it on boot.

### Post-install

Default credentials after first boot: `admin` / `YourPaSsWoRd`

The lazy-install mechanism in `platform/broadcom/one-image.mk` selects the
correct platform `.deb` from the ONIE platform string at install time — no
manual deb selection needed.

---

## 6. `rules/config.user` — Development Overrides

Create `rules/config.user` (gitignored) to avoid repeating flags on every
`make` invocation:

```makefile
# rules/config.user — local overrides, not committed

# Slave container Debian version
BLDENV = trixie

# Parallel package builds (set to core count)
SONIC_BUILD_JOBS = 40

# Cache built .deb packages between runs (rwcache = read+write, rcache = read-only)
SONIC_DPKG_CACHE_METHOD = rwcache

# Skip unit tests (avoids upstream hangs, saves time)
BUILD_SKIP_TEST = y
```

Key variables reference:

| Variable | Default | Effect |
|---|---|---|
| `BLDENV` | trixie | Slave container Debian version |
| `NOBOOKWORM` | 0 | `1` skips bookworm pass (deb-only builds only) |
| `NOTRIXIE` | 0 | `0` enables trixie pass; required for `.bin` |
| `SONIC_BUILD_JOBS` | 1 | Parallel package builds |
| `SONIC_CONFIG_MAKE_JOBS` | nproc | Parallelism inside each package |
| `SONIC_DPKG_CACHE_METHOD` | none | `rwcache` caches debs across builds |
| `BUILD_SKIP_TEST` | n | `y` skips unit tests |
| `SONIC_IMAGE_VERSION` | (from git) | Explicit version string in installer |

---

## 7. Debugging the Build

### Interactive shell inside the build container

```bash
make sonic-slave-bash
```

Drops into the same Docker container that `make` uses. Useful for running
`dpkg-buildpackage` manually or inspecting build state.

### Keep container alive after a failed build

```bash
KEEP_SLAVE_ON=yes BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

After the failure the container keeps running; attach with `docker exec -it`.

### Rebuild the slave container image

```bash
make sonic-slave-build
```

Run this if `Dockerfile.j2` changes or after a Docker upgrade.

---

## 8. Clean Targets

### Clean one package (forces rebuild of just that deb)

```bash
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb-clean
```

Appending `-clean` to any deb target removes its build artifacts and stamps.

### Manual full clean (more reliable than `make clean`)

```bash
rm -rf target/ fsroot*
make configure PLATFORM=broadcom   # Must re-run — recreates target/ subdirs
```

`make configure` is required after any `rm -rf target/` — the build fails with
"No such file or directory" for log files if the directory structure is absent.

---

## 9. Common Failures and Fixes

### `j2: command not found` during clean

```bash
pip3 install jinjanator
```

### `NOTRIXIE=1` — `.bin` never assembled

Symptom: bookworm pass completes successfully but `target/sonic-broadcom.bin`
does not exist.

Fix: ensure `NOTRIXIE` is unset or `NOTRIXIE=0`. The outer `Makefile` defaults
to `NOTRIXIE ?= 1`.

### `NOBOOKWORM=1` — missing docker targets

Symptom:
```
make: *** No rule to make target 'target/docker-bmp-watchdog.gz',
      needed by 'target/sonic-broadcom.bin'. Stop.
```

Fix: remove `NOBOOKWORM=1`. It is only valid for isolated deb builds.

### `sonic_utilities` test hangs

Symptom: build stalls at ~50% in the sonic-utilities package test.

Fix: `BUILD_SKIP_TEST=y`

### `dpkg` Half-Configured on first boot (postinst exits 1)

Symptom: syslog shows `post-installation script subprocess returned error exit status 1`

Root cause: `/bin/sh` on Debian Trixie is `dash`; `set -e` aborts the postinst
when a command substitution exits non-zero (CLI exits non-zero because Redis is
not yet running on first boot).

Fix already committed: `|| true` appended inside the command substitution in
`platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`.

Live fix on a target stuck in Half-Configured:

```bash
sudo dpkg --configure sonic-platform-accton-wedge100s-32x
```

### `syncd` crash — `swss`/`bgp` fail — no interfaces

Symptom: `show interfaces status` returns empty; syslog shows:

```
SAI_API_SWITCH:platform_config_file_set: Invalid YAML configuration file:
/usr/share/sonic/hwsku/th-wedge100s-32-flex.config.bcm
```

Root cause: `sai.profile` referenced `th-wedge100s-32-flex.config.bcm` (missing
the `x`). Correct filename is `th-wedge100s-32x-flex.config.bcm`.

Fix already committed in
`device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile`.

Live fix:

```bash
sudo sed -i "s/th-wedge100s-32-flex/th-wedge100s-32x-flex/" \
  /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile
sudo systemctl reset-failed swss syncd bgp radv teamd
sudo systemctl start swss
```

### `make configure` must be re-run after cleaning

After `rm -rf target/`, always run `make configure PLATFORM=broadcom` before the
next build.

### Build slave Docker image fails to build

Check Docker version (must be >= 20.10.10) and ensure the overlay module is
loaded:

```bash
sudo modprobe overlay
```

### Disk space exhausted

```bash
df -h /export/sonic/
# Need ~100 GB free for a full image build
```

Remove stale Docker images if needed:

```bash
docker image prune -f
```

---

## 10. Build System File Map

| File | Role |
|---|---|
| `platform/broadcom/platform-modules-accton.mk` | Defines the wedge100s deb target (version 1.1) |
| `platform/broadcom/rules.mk` | Includes `platform-modules-accton.mk` |
| `platform/broadcom/one-image.mk` | Adds wedge100s to `_LAZY_INSTALLS` |
| `installer/platforms/x86_64-accton_wedge100s_32x-r0` | GRUB console params (ttyS0, 57600) |
| `installer/platforms_asic` | Maps platform string to ASIC vendor (broadcom) |
| `rules/config` | Default build variables (`SONIC_USE_PDDF_FRAMEWORK=y`) |
| `rules/config.user` | Local overrides — gitignored |
