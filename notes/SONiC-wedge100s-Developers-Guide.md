# SONiC Wedge 100S-32X Developer's Guide

Platform: Accton Wedge 100S-32X (Facebook Wedge 100S-32X, Broadcom Tomahawk BCM56960)
Branch: `wedge100s`
Target: `admin@<nos-ip>` (SONiC), `root@<bmc-ip>` (OpenBMC)

---

## 1. Hardware Targets Quick Reference

| Target | Access | Notes |
|---|---|---|
| OpenBMC | `ssh root@<bmc-ip>` (pw: `0penBmc`) | BMC, USB CDC to host; fallback `/dev/ttyACM0` @ 57600 |
| ONIE | `ssh root@<nos-ip>` | NOS-install mode, no python |
| SONiC switch | `ssh admin@<nos-ip>` (pw: `YourPaSsWoRd`)| Primary target, kernel 6.12.41 |

**BMC reachability check** — after any BMC reboot `authorized_keys` is cleared:

```bash
ping -c1 -W2 <bmc-ip> && ssh -o ConnectTimeout=5 root@<bmc-ip> echo ok
# If ping OK but SSH fails:
ssh-copy-id root@<bmc-ip>   # password: 0penBmc
```

---

## 2. Git Submodules

For our fork of [sonic-buildimage](https://github.com/sonic-net/sonic-buildimage). 

Initialize all submodules after a fresh clone:

```bash
make init
# which runs: git submodule update --init --recursive
```

### What they are

This repo uses **git submodules** — each is an independent git repository pinned to a specific commit by the parent repo's index. The parent stores a pointer (commit SHA), not the submodule's content. Running `git submodule status` shows the pinned SHA for every submodule; a leading `+` means the checked-out commit in that directory differs from what the parent expects.

### How releases are referenced

Each submodule entry in `.gitmodules` has a `url` and optionally a `branch`. The actual pinned version is the commit SHA recorded in the parent's index — the `branch` field is only used by `git submodule update --remote` to find the tracking branch; it does **not** automatically pull new commits. To advance a submodule to a newer upstream commit:

```bash
# Move a submodule to latest on its tracking branch
cd src/sonic-swss
git fetch origin
git checkout origin/master   # or a specific tag/commit
cd ../..
git add src/sonic-swss
git commit -m "bump sonic-swss to <sha>"

# Or use the convenience command
git submodule update --remote src/sonic-swss
git add src/sonic-swss
git commit -m "bump sonic-swss to latest master"
```

### Detecting modifications inside a submodule

```bash
# Show all submodules — leading '+' = checked-out SHA ≠ parent-pinned SHA
git submodule status

# Show uncommitted changes (tracked files) inside a specific submodule
git -C src/sonic-swss status --short

# Show the actual diff of tracked file changes
git -C src/sonic-swss diff HEAD

# Show ALL submodules that have any tracked modifications
for sub in $(git submodule status | awk '{print $2}'); do
  changes=$(git -C $sub diff HEAD --name-only 2>/dev/null)
  if [ -n "$changes" ]; then echo "MODIFIED: $sub"; echo "$changes" | sed 's/^/  /'; fi
done

# Show submodules with untracked files (build artifacts, generated files)
for sub in $(git submodule status | awk '{print $2}'); do
  untracked=$(git -C $sub ls-files --others --exclude-standard 2>/dev/null)
  if [ -n "$untracked" ]; then echo "UNTRACKED: $sub"; echo "$untracked" | sed 's/^/  /'; fi
done
```

**Currently modified submodules in this repo (tracked file changes):**

| Submodule | Modified files | Purpose of change | Captured as patch? |
|---|---|---|---|
| `src/sonic-swss` | `orchagent/vxlanorch.h` | `using Orch::doTask;` declaration to fix GCC 12+ hidden-overload error | Yes — `0003-fix-vxlanorch…` |
| `src/sonic-swss` | `orchagent/zmqorch.h` | `using Orch::doTask;` declaration to fix GCC 12+ hidden-overload error | Yes — `0004-fix-zmqorch…` |
| `src/sonic-utilities` | `scripts/sfpshow` | SFF-8636/QSFP28 DOM sensor fallback rendering for `sfp pm` command | Yes — `0001-sfpshow…` |
| `src/sonic-utilities` | `sonic-utilities-data/generate_completions.py` | Click 8 bash completion fallback when CLI module can't be imported in slave container | Yes — `0002-generate-completions…` |

### Capturing submodule changes as build-time patches (when you cannot commit upstream)

The build system has a **quilt patch mechanism** built into `slave.mk`. Before building any package whose `_SRC_PATH` points to a submodule, the build checks for a sibling directory named `<submodule-dir>.patch/`. If a `series` file is present there, quilt applies those patches before the build and removes them after.

**Pattern:**
```
src/sonic-swss/          ← submodule (do not commit here)
src/sonic-swss.patch/    ← patch directory (committed to parent repo)
  series                 ← lists patch files in order
  0001-my-change.patch   ← patch file in git-format-patch format
```

**Workflow to create a patch from current submodule changes:**

```bash
# 1. Make your changes inside the submodule (already done, or make them now)
cd src/sonic-swss
# ... edit files ...

# 2. Stage and create a git-format-patch
git add orchagent/flex_counter/flex_counter_manager.h orchagent/routeorch.h
git commit -m "fix: expose using declarations for flex_counter and routeorch"
git format-patch HEAD~1 -o ../sonic-swss.patch/

# 3. Create or update the series file
ls ../sonic-swss.patch/*.patch | xargs -I{} basename {} > ../sonic-swss.patch/series

# 4. Reset the submodule back to the pinned commit (undo the local commit)
git reset HEAD~1       # unstages the commit, leaves working tree changes
cd ..

# 5. Add the patch directory to the parent repo
git add src/sonic-swss.patch/
git status             # submodule itself should show no SHA change
```

**Applying and reverting patches manually (for testing):**

Patches are applied inside the build container by slave.mk, immediately before each package build, and removed after. They never persist in the working tree.

The exact lifecycle:

    make init
      └── git submodule update --init
            └── src/sonic-utilities/ ← clean upstream, NO patches applied

    make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
      └── slave.mk rule fires for each package:
            1. quilt pop -a -f          ← clean any prior state
            2. quilt push -a            ← apply src/sonic-utilities.patch/series
            3. dpkg-buildpackage / make ← build with patches applied
            4. quilt pop -a -f          ← remove patches
            5. rm -rf .pc               ← clean quilt state
            └── src/sonic-utilities/ ← clean again, as if nothing happened

So if you cd src/sonic-utilities && grep something between builds, you'll see unpatched code. The patches only exist in the tree during the few seconds the build is actually running.

Practical implication: if you want to develop/test against a patched submodule interactively, you have to apply them manually:

    cd src/sonic-utilities
    QUILT_PATCHES=../sonic-utilities.patch quilt push -a
    # ... do your work ...
    quilt pop -a -f && rm -rf .pc   # clean up when done


```bash
# Apply
cd src/sonic-swss
QUILT_PATCHES=../sonic-swss.patch quilt push -a

# Revert
QUILT_PATCHES=../sonic-swss.patch quilt pop -a -f
rm -rf .pc
```

### Classifying submodule changes: patch vs. artifact vs. upstream

Before capturing a submodule change as a patch, classify it:

| Type | Examples | Action |
|---|---|---|
| **Our intentional change** | C++ `using` fix, CLI fallback logic, driver customisation | Capture as quilt patch in `<submodule>.patch/` |
| **Build artifact** | `Cargo.lock` bumped by cargo path resolution, `debian/rules` chmod | Revert or add to `.gitignore` inside the submodule; do **not** patch |
| **Upstream-appropriate fix** | Generic compiler warning fix, widely useful CLI improvement | Ideally submit upstream; if outside the developer loop, manage locally and note in the patch commit message |

**How to identify build artifacts:**

```bash
# Files changed by the build system (mode-only changes, generated files)
git -C src/sonic-swss diff HEAD --stat
git -C src/sonic-swss diff HEAD -- Cargo.lock   # almost always a build artifact

# Mode-only diffs (chmod) — these are always build artifacts
git -C src/sonic-bmp diff HEAD --diff-filter=T
```

**Decision: manage locally vs. submit upstream**

The submodule SHAs in this repo are pinned to specific commits — we are not on a developer fork of these repos and are outside the upstream PR loop. In practice:

- **Manage locally** (in `wedge100s` branch `<sub>.patch/`): changes that are Wedge 100S-specific, build-environment workarounds, or simply too risky to upstream without a full CI loop.
- **Note upstream PR if exists**: if the upstream repo has already merged an equivalent fix in a newer commit, note the SHA in the patch header (`Subject:` line) so it can be dropped when the submodule pin is advanced.

### Updating and dropping patches

**To update a patch after further changes to the same files:**

```bash
cd src/sonic-swss
# Apply existing patches first so your changes layer on top
QUILT_PATCHES=../sonic-swss.patch quilt push -a

# Make additional edits, then refresh the last patch
quilt refresh              # or: quilt refresh 0003-fix-vxlanorch...

# Pop all patches and re-export
QUILT_PATCHES=../sonic-swss.patch quilt pop -a -f
rm -rf .pc
cd ..
git add src/sonic-swss.patch/
```

**To drop a patch when advancing a submodule pin (the upstream fix is now included):**

```bash
# 1. Advance the submodule to the new commit
cd src/sonic-swss
git fetch origin
git checkout <new-sha>
cd ..
git add src/sonic-swss        # record new SHA in parent

# 2. Remove the now-redundant patch
rm src/sonic-swss.patch/0003-fix-vxlanorch-expose-doTask-using-declaration.patch
# Update series to remove the entry
vi src/sonic-swss.patch/series

# 3. Verify quilt still applies cleanly
cd src/sonic-swss
QUILT_PATCHES=../sonic-swss.patch quilt push -a
QUILT_PATCHES=../sonic-swss.patch quilt pop -a -f
rm -rf .pc
cd ..

# 4. Commit parent repo changes
git add src/sonic-swss src/sonic-swss.patch/
git commit -m "bump sonic-swss to <sha>; drop 0003 (merged upstream)"
```

**Existing `.patch` directories in this repo:**

| Patch dir | What it patches |
|---|---|
| `src/sonic-swss.patch/` | Cargo.toml path fix; Cargo.lock lock-option removal; `vxlanorch.h` using declaration; `zmqorch.h` using declaration |
| `src/sonic-utilities.patch/` | `sfpshow` SFF-8636 DOM fallback; `generate_completions.py` Click 8 fallback template |
| `src/sonic-dash-ha.patch/` | DASH HA build fixes |
| `src/supervisor.patch/` | Supervisor process manager build fixes |
| `src/ptf.patch/`, `src/ptf-py3.patch/` | PTF test framework patches |
| `src/scapy.patch/` | Scapy packet library patches |
| `src/redis-dump-load.patch/` | redis-dump-load utility patches |

### Submodule reference table

The submodules relevant to Wedge 100S-32X are marked **Active**. Others are present because this is a multi-platform monorepo; they are compiled but their output is not installed on this platform.

| Submodule path | Pinned commit | Relevance | What it contributes to the running target |
|---|---|---|---|
| `src/sonic-swss` | `18752666` | **Active** | `swss` container — orchagent, intfmgrd, vrfmgrd, all \*syncd processes that translate Redis config to SAI calls |
| `src/sonic-swss-common` | `364023a7` | **Active** | Shared C++ library (libswsscommon): Redis abstraction, ProducerTable/ConsumerTable, FieldValueTuple — used by nearly every container |
| `src/sonic-sairedis` | `4758c3cc` | **Active** | SAI Redis adapter layer; mediates between orchagent and syncd over a Redis channel; includes saiplayer/saidump debug tools |
| `src/sonic-linux-kernel` | `dac5f908` | **Active** | SONiC-patched Linux kernel (6.12.41); includes driver patches for Broadcom platform hardware |
| `src/sonic-frr/frr` | `88f5c06c` | **Active** | FRRouting (BGP, OSPF, IS-IS, static routes); runs inside `bgp` container |
| `src/sonic-utilities` | `9a408e61` | **Active** | All SONiC CLI tools: `sonic-cfggen`, `sfpshow`, `portstat`, `show`, `config`, `crm`, `pfcstat`, etc. |
| `src/sonic-platform-common` | `972ff46b` | **Active** | `sonic_platform_base` Python base classes that the vendor `sonic_platform` wheel implements (chassis, sfp, psu, fan, thermal ABCs) |
| `src/sonic-platform-daemons` | `b85fff32` | **Active** | Platform monitor daemons: `xcvrd`, `psud`, `thermalctld`, `syseepromd`, `ledd`, `stormond` — run inside `pmon` container |
| `src/sonic-gnmi` | `5b814227` | **Active** | gNMI server (`gnmi_server`); runs inside `gnmi` container; gRPC northbound for streaming telemetry and config |
| `src/sonic-mgmt-framework` | `79619b68` | **Active** | REST/RESTCONF management framework backend; runs inside `mgmt-framework` container |
| `src/sonic-mgmt-common` | `61d8b07b` | **Active** | Shared Go libraries for mgmt-framework: transformer, translib, YANG models |
| `src/sonic-host-services` | `2c5bf361` | **Active** | Host-side D-Bus services invoked by containers for operations needing root on the host (reboot, image management, config reload) |
| `src/sonic-dbsyncd` | `22335e06` | **Active** | `db_migrator` — Redis DB schema migration tool run at boot to upgrade config DB format |
| `src/sonic-snmpagent` | `3f0600e0` | **Active** | Python SNMP agent (snmp_ax_impl); runs inside `snmp` container alongside standard snmpd |
| `src/sonic-sysmgr/gnoi` | `2b6ff72d` | **Active** | gNOI proto definitions (Reset, File, OS); used by `sysmgr` for gRPC-based system operations |
| `src/supervisor` | `dbca8d45` | **Active** | Supervisord process manager; runs inside every container to manage daemon lifecycle |
| `src/sonic-py-swsssdk` | `2502c89d` | **Active** | Python Redis client wrapper (legacy); provides `SonicV2Connector` used by Python daemons and CLI |
| `src/sonic-stp` | `586d842c` | **Active** | Spanning Tree Protocol daemon (not currently enabled on this platform but built) |
| `src/dhcprelay` | `8987cbb7` | Active (not running) | DHCP relay agent; `docker-dhcp-relay` image is installed but not started on this platform |
| `src/dhcpmon` | `d78974ff` | Active (not running) | DHCP monitor; works alongside dhcprelay |
| `src/linkmgrd` | `15f71fa5` | Active (not running) | Link manager for dual-ToR / MUX scenarios; not applicable to Wedge 100S |
| `src/sonic-restapi` | `1031be68` | Active (not running) | Standalone REST API; superseded by mgmt-framework on this build |
| `src/sonic-ztp` | `170acb03` | Active (not running) | Zero Touch Provisioning; not configured for Wedge 100S |
| `src/sonic-platform-pde` | `6e36a871` | Active (not running) | Platform Development Environment SDK; not used in production image |
| `src/sonic-genl-packet` | `b6e6b1bf` | Active (not running) | Generic Netlink packet interface driver |
| `src/wpasupplicant/sonic-wpa-supplicant` | `f3f3caa1` | Active (not running) | WPA supplicant for 802.1X port authentication; `docker-macsec` image present but not started |
| `src/sonic-bmp` | `9625f504` | Active (not running) | BGP Monitoring Protocol; `docker-sonic-bmp` image present but not started |
| `src/sonic-dash-api` | `18a29c1c` | Inactive | DASH (Disaggregated APIs for SONiC Hosts) API definitions; not applicable to switching ASIC |
| `src/sonic-dash-ha` | `8f9893d4` | Inactive | DASH HA (high availability); not applicable |
| `src/sonic-p4rt/sonic-pins` | `56a7762a` | Inactive | P4Runtime support for P4-programmable ASICs; not applicable to Tomahawk |
| `src/ptf` / `src/ptf-py3` | `36a3e3d9` / `978598dd` | Test only | Packet Test Framework; used in CI/CD testing, not installed on target |
| `src/scapy` | `8b63d73a` | Test only | Scapy packet library; used by PTF tests |
| `src/redis-dump-load` | `75854979` | Dev tool | Redis database dump/restore utility |
| `platform/broadcom/saibcm-modules-dnx` | `cd50cb45` | Inactive | SAI BCM kernel modules for DNX (Jericho) ASICs; not applicable to Tomahawk |
| `platform/broadcom/sonic-platform-modules-arista` | `e0904bc3` | Inactive | Arista platform modules; different vendor |
| `platform/broadcom/sonic-platform-modules-nokia` | `562f89b0` | Inactive | Nokia platform modules; different vendor |
| `platform/mellanox/hw-management/hw-mgmt` | `bd26aeec` | Inactive | Mellanox HW management; different vendor |
| `platform/marvell-prestera/*` | various | Inactive | Marvell Prestera platform support; different ASIC |
| `platform/marvell-teralynx/*` | various | Inactive | Marvell Teralynx platform support; different ASIC |
| `platform/vpp` | `b020b836` | Inactive | VPP (Vector Packet Processing) software dataplane; not applicable |
| `platform/alpinevs` | `cd9e8244` | Inactive | Alpine virtual switch platform; test/simulation only |
| `platform/p4/*` | various | Inactive | P4 compiler and runtime infrastructure; not applicable to Tomahawk |
| `platform/barefoot/*` | various | Inactive | Intel Tofino (Barefoot) platform; different vendor |

---

## 3. Build System — Detailed Reference

**Default distro pass matrix** (do not override for full image builds):

| Variable | Default | Effect |
|---|---|---|
| `NOSTRETCH`, `NOBUSTER`, `NOBULLSEYE` | `1` | Skipped — not needed |
| `NOBOOKWORM` | `0` | Bookworm pass runs — builds ~37 Docker service images |
| `NOTRIXIE` | `0` | Trixie pass runs — builds `.deb` packages + final `.bin` |

Do not set `NOBOOKWORM=1` for full image builds — Docker service images are built in the bookworm pass and are required by the installer.
Do not set `NOTRIXIE=1` — the trixie pass assembles the final `.bin`; without it nothing is produced.

Build System File Map

| File | Role |
|---|---|
| `platform/broadcom/platform-modules-accton.mk` | Defines the wedge100s deb target (version 1.1) |
| `platform/broadcom/rules.mk` | Includes `platform-modules-accton.mk` |
| `platform/broadcom/one-image.mk` | Adds wedge100s to `_LAZY_INSTALLS` |
| `installer/platforms/x86_64-accton_wedge100s_32x-r0` | GRUB console params (ttyS0, 57600) |
| `installer/platforms_asic` | has x86_64-accton_wedge100s_32x-r0
 - Maps platform string to ASIC vendor (broadcom) |
| `rules/config` | Default build variables (`SONIC_USE_PDDF_FRAMEWORK=y`) |
| `rules/config.user` | Local overrides — gitignored |

### 3.0 Prerequisites

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

### 3.1 Build System Overview

Three-layer pipeline:

```
Makefile (host)
  └─ Makefile.work (host, Docker orchestrator)
       └─ slave.mk (inside sonic-slave-trixie container)
            └─ rules/*.mk + platform/broadcom/rules.mk
```

1. **`Makefile` (host)** — thin wrapper that sequences multi-distro passes. Default: skips jessie/stretch/buster/bullseye (all `NO*=1`), runs bookworm pass (builds Docker service images) then trixie pass (builds the `.bin`). Both `NOBOOKWORM` and `NOTRIXIE` default to `0` — do not override them for full image builds.

2. **`Makefile.work` (host, Docker orchestrator)** — builds/pulls a `sonic-slave-trixie-<user>:<hash>` Docker image from `sonic-slave-trixie/Dockerfile.j2`, then runs `docker run --privileged` with the repo bind-mounted at `/sonic`. All compilation happens inside this container.

3. **`slave.mk` (inside container)** — the actual GNU make build engine. Includes `rules/*.mk` and `platform/broadcom/rules.mk`. Produces .deb packages in `target/debs/trixie/` and Docker images in `target/`.

User make variables file:

`rules/config.user` is `-include`d in **both** `Makefile.work` (host layer) and `slave.mk` (container layer). Put your local overrides here — it is gitignored.

For example:
```makefile
# rules/config.user examples
SONIC_BUILD_JOBS = 40
BUILD_SKIP_TEST = y
SONIC_BUILD_MEMORY = 320g
SONIC_DPKG_CACHE_METHOD = rwcache
SONIC_DPKG_CACHE_SOURCE = /export/sonic/dpkg-cache
SONIC_IMAGE_VERSION = wedge100s-$(shell date +%y%m%d)-$(shell git -C /export/sonic/sonic-buildimage.claude rev-parse --short HEAD)
DEFAULT_BUILD_LOG_TIMESTAMP = simple
```

### 3.2 Build artifacts and their locations

| Artifact | Path | What it is |
|---|---|---|
| Platform .deb | `target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` | Kernel modules, utils, services, sonic_platform wheel |
| Docker images | `target/docker-<name>.gz` | Compressed Docker image tarballs |
| Base filesystem (RFS) squashfs | `target/sonic-broadcom.bin__broadcom__rfs.squashfs` | Root filesystem for the broadcom machine type |
| Dependent machine RFS | `target/sonic-broadcom.bin__broadcom-legacy-th__rfs.squashfs` | RFS for legacy Tomahawk variant |
| DNX machine RFS | `target/sonic-broadcom.bin__broadcom-dnx__rfs.squashfs` | RFS for DNX (Jericho) variant |
| Final installer | `target/sonic-broadcom.bin` | Self-extracting ONIE installer (combines RFS + docker images) |
| Debian packages (all) | `target/debs/trixie/*.deb` | All built .deb packages |

### 3.4 Build stages and their make targets

The build is composed of four distinct stages that must complete in order:

#### Stage 1 — Debian packages (`.deb`)

Source packages under `src/` and `platform/` are compiled into `.deb` files inside the slave container. Each package has an independent target.

```bash
# Platform deb (most frequently rebuilt for Wedge 100S work)
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# Other debs built from submodules (examples) 
make target/debs/trixie/sonic-utilities_1.0-1_all.deb
make target/debs/trixie/libswsscommon_1.0.0_amd64.deb
make target/debs/trixie/sonic-sairedis_1.0.0_amd64.deb
```

#### Stage 2 — Docker images (`.gz`)

Docker images layer their required `.deb` files (via `_DEPENDS`) and are built with `docker build` inside the slave container.

```bash
# Build a specific docker image
make target/docker-platform-monitor.gz
make target/docker-orchagent.gz
make target/docker-syncd-brcm.gz
make target/docker-fpm-frr.gz
```

The build system tracks `_DEPENDS` for each image (e.g. `docker-orchagent` depends on `libswsscommon`, `sonic-utilities`, etc.). However, dependency tracking is at the **file level** — if you edit source inside a submodule without rebuilding the `.deb`, the docker image will not automatically rebuild. See §10.4 for forced cleaning.

#### Stage 3 — Base root filesystem (`rfs.squashfs`)

`build_debian.sh` creates a Debian trixie chroot, installs selected `.deb` packages, and produces a squashfs. This is the base OS filesystem without docker images.

```bash
# Rebuild just the broadcom RFS (rare — only when base OS packages change)
make target/sonic-broadcom.bin__broadcom__rfs.squashfs
```

The RFS is slow to build (15–30 min) because it runs `debootstrap` and installs ~200 packages. It is only needed when:
- A non-platform `.deb` changes (e.g. kernel, systemd, network stack)
- New system packages are added/removed from the image

#### Stage 4 — Final installer (`.bin`)

`build_image.sh` packs the squashfs and all docker image `.gz` files into a self-extracting ONIE installer payload.

```bash
# Full image build — runs bookworm pass (Docker images) then trixie pass (.bin)
make BUILD_SKIP_TEST=y SONIC_BUILD_JOBS=40 target/sonic-broadcom.bin
```

> **Do not prefix with `BLDENV=trixie`** for full image builds — that skips the
> bookworm pass and omits the Docker service images required by the installer.
>
> **After `rm -rf target/`** you must re-run `make configure PLATFORM=broadcom`
> before building. The bookworm/trixie passes need `target/debs/<distro>/` to
> exist before they can write `.flags` dependency-tracking files; `configure`
> creates that directory tree.

### 3.5 Selective builds for Wedge 100S development

For day-to-day platform work, only the platform `.deb` needs rebuilding. For changes to swss/sairedis/orchagent, only the relevant `.deb` + docker image.

```bash
# Most common: platform kernel modules or sonic_platform Python changed
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# swss orchagent changed (e.g. src/sonic-swss modified)
make target/debs/trixie/swss_1.0.0_amd64.deb
make target/docker-orchagent.gz

# Platform daemons changed (xcvrd, psud, thermalctld — in src/sonic-platform-daemons)
make target/debs/trixie/sonic-platform-daemons_1.0-1_all.deb
make target/docker-platform-monitor.gz

# sonic-utilities changed (sfpshow, portstat, show/config CLI)
make target/debs/trixie/sonic-utilities_1.0-1_all.deb
# (utilities are installed into multiple containers — rebuild pmon, swss, lldp as needed)

# syncd / SAI changed
make target/debs/trixie/syncd_1.0.0_amd64.deb
make target/docker-syncd-brcm.gz
```

### 3.5 Forced cleaning (when dependency tracking is insufficient)

The build system does **not** monitor submodule source files for changes. If you edit files inside `src/sonic-swss` and want to guarantee a rebuild, you must manually clean the relevant artifacts before building. The `-clean` suffix appended to any target path triggers removal:

```bash
# Clean a specific .deb and rebuild
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb-clean
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# Clean a docker image and rebuild
make target/docker-platform-monitor.gz-clean
make target/docker-platform-monitor.gz

# Clean the RFS squashfs (forces rebuild of the base filesystem)
make target/sonic-broadcom.bin__broadcom__rfs.squashfs-clean

# Clean the final installer only (re-packs existing squashfs + dockers; fast)
make target/sonic-broadcom.bin-clean
make target/sonic-broadcom.bin

# Chain: clean deb + docker image in one invocation
make \
  target/debs/trixie/swss_1.0.0_amd64.deb-clean \
  target/docker-orchagent.gz-clean
make \
  target/debs/trixie/swss_1.0.0_amd64.deb \
  target/docker-orchagent.gz
```

**When to clean what:**

| Change made | What to clean |
|---|---|
| `platform/.../wedge100s-32x/modules/*.c` (kernel module source) | `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` |
| `platform/.../wedge100s-32x/sonic_platform/*.py` (Python platform API) | `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` |
| `platform/.../wedge100s-32x/utils/*.c` (BMC/I2C daemon C source) | `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` |
| `src/sonic-platform-daemons/**` (xcvrd, psud, thermalctld) | `sonic-platform-daemons_1.0-1_all.deb` + `docker-platform-monitor.gz` |
| `src/sonic-swss/**` (orchagent, *syncd) | `swss_1.0.0_amd64.deb` + `docker-orchagent.gz` |
| `src/sonic-sairedis/**` | `syncd_1.0.0_amd64.deb` + `docker-syncd-brcm.gz` |
| `src/sonic-swss-common/**` | `libswsscommon_1.0.0_amd64.deb` + any docker depending on it |
| `src/sonic-utilities/**` | `sonic-utilities_1.0-1_all.deb` + affected docker images |
| `src/sonic-gnmi/**` | `sonic-gnmi_1.0-1_amd64.deb` + `docker-sonic-gnmi.gz` |
| `device/accton/x86_64-accton_wedge100s_32x-r0/**` | No build needed — files are bind-mounted on target |
| Any `Dockerfile` change | Clean the corresponding `docker-*.gz` target |
| Base OS package version change | Clean `sonic-broadcom.bin__broadcom__rfs.squashfs` |

### 3.6 Entering the build container interactively

For debugging build failures, drop into the slave container shell:

```bash
# Opens a shell inside sonic-slave-trixie with the repo mounted at /sonic
make sonic-slave-bash

# Keep the container alive after a failed build (exit code ≠ 0)
KEEP_SLAVE_ON=yes make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
# Then: docker exec -it sonic-slave-trixie-<user>-<hash> bash
```

Inside the container, the repo is at `/sonic`. You can manually run `dpkg-buildpackage` in any package directory to iterate quickly without going through the full make dependency graph.

** Rebuild the slave container image **

Run this if `Dockerfile.j2` changes or after a Docker upgrade.

```bash
make sonic-slave-build
```

### 3.7 Build cache

The build supports a Debian package cache to avoid recompiling unchanged packages:

```bash
# Enable read-write cache (saves .deb files; reuses them on next build)
# In rules/config.user:
SONIC_DPKG_CACHE_METHOD = rwcache
SONIC_DPKG_CACHE_SOURCE = /tmp/dpkg-cache   # any host path

# Clear the vcache (virtual build cache used for Docker layer reuse)
make vclean
```

### 3.8 Install options

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

## 4. Platform & Device Directory Mapping

### Host (dev) → Target (installed)

| Host path | Target path | Mechanism |
|---|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/` | `/usr/share/sonic/device/accton/x86_64-accton_wedge100s_32x-r0/` | `sonic-device-data` deb (`device/` → `usr/share/sonic/`) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/` | `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/` → bind-mounted as `/usr/share/sonic/hwsku` in containers | same |
| `platform/.../wedge100s-32x/modules/*.ko` | `/lib/modules/6.12.41+deb13-sonic-amd64/extra/` | `sonic-platform-accton-wedge100s-32x` deb |
| `platform/.../wedge100s-32x/utils/*` (compiled binaries) | `/usr/bin/wedge100s-bmc-daemon`, `/usr/bin/wedge100s-i2c-daemon` | same |
| `platform/.../wedge100s-32x/service/*.service` | `/lib/systemd/system/` | same |
| `platform/.../wedge100s-32x/sonic_platform-1.0-py3-none-any.whl` | `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl` | same (`.install` file) |
| (wheel, at pmon start) | `/usr/local/lib/python3.13/dist-packages/sonic_platform/` | `pip3 install` run by `docker_init.sh` inside pmon |

### The `/usr/share/sonic/platform` bind-mount

Most containers receive `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0` bind-mounted as `/usr/share/sonic/platform` inside the container. This is how platform config, the hwsku directory, and the sonic_platform wheel are exposed to all containers without baking them in.

The `pmon` container also receives:
- `/sys` → `/sys` (direct hardware access for sensors/GPIO)
- `/usr/share/sonic/device/pddf` → `/usr/share/sonic/device/pddf` (PDDF JSON data)
- `/run/wedge100s` → `/run/wedge100s` (runtime IPC socket dir)

### Key config files inside device dir (edit in place for fast iteration)

| File | Purpose |
|---|---|
| `Accton-WEDGE100S-32X/port_config.ini` | Port-to-lane mapping, speeds |
| `Accton-WEDGE100S-32X/sai.profile` | SAI init key-value pairs |
| `pddf/pddf-device.json` | PDDF I2C topology, PSU/FAN/EEPROM descriptors |
| `plugins/sfputil.py` | Legacy SFP plugin (pre-platform-API) |
| `plugins/led_control.py` | LED plugin |
| `sensors.conf` | lm-sensors configuration |
| `*.config.bcm` | Broadcom SDK port/chip config |

---

## 5. Docker Container Reference

### Startup order (systemd dependencies)

```
docker.service
  └─ database.service          # must be first — Redis
       ├─ swss.service          # requires: database, opennsl-modules, config-setup
       │    └─ syncd.service    # requires: database, swss, opennsl-modules
       ├─ bgp.service
       ├─ teamd.service
       ├─ lldp.service
       ├─ snmp.service
       ├─ radv.service
       ├─ gnmi.service
       ├─ mgmt-framework.service
       ├─ eventd.service
       ├─ sysmgr.service
       └─ pmon.service          # requires: database, config-setup
```

### Container table

| Container | Image | Role | Source dir | Build target |
|---|---|---|---|---|
| `database` | `docker-database` | Redis — central state store; all other containers depend on it | `dockers/docker-database/` | `target/docker-database.gz` |
| `swss` | `docker-orchagent` | Switch State Service — translates Redis config into SAI calls via orchagent | `dockers/docker-orchagent/` | `target/docker-orchagent.gz` |
| `syncd` | `docker-syncd-brcm` | SAI → Broadcom SDK bridge; drives the Tomahawk ASIC; includes bcmsh/bcmcmd | `platform/broadcom/docker-syncd-brcm/` | `target/docker-syncd-brcm.gz` |
| `pmon` | `docker-platform-monitor` | Platform monitor — xcvrd (optics), psud (PSU), thermalctld, syseepromd, ledd; installs vendor sonic_platform wheel at start | `dockers/docker-platform-monitor/` | `target/docker-platform-monitor.gz` |
| `bgp` | `docker-fpm-frr` | BGP/routing via FRRouting; FPM pushes routes into kernel and Redis | `dockers/docker-fpm-frr/` | `target/docker-fpm-frr.gz` |
| `lldp` | `docker-lldp` | LLDP daemon (lldpd); populates neighbor table in Redis | `dockers/docker-lldp/` | `target/docker-lldp.gz` |
| `teamd` | `docker-teamd` | LAG/portchannel management via teamd | `dockers/docker-teamd/` | `target/docker-teamd.gz` |
| `snmp` | `docker-snmp` | SNMP agent; reads Redis for interface counters, system info | `dockers/docker-snmp/` | `target/docker-snmp.gz` |
| `radv` | `docker-router-advertiser` | IPv6 router advertisement daemon (radvd) | `dockers/docker-router-advertiser/` | `target/docker-router-advertiser.gz` |
| `gnmi` | `docker-sonic-gnmi` | gNMI/gRPC northbound interface; mounts host `/` as `/mnt/host` | `dockers/docker-sonic-gnmi/` | `target/docker-sonic-gnmi.gz` |
| `mgmt-framework` | `docker-sonic-mgmt-framework` | REST API + CLI backend (RESTCONF/OpenAPI) | `dockers/docker-sonic-mgmt-framework/` | `target/docker-sonic-mgmt-framework.gz` |
| `eventd` | `docker-eventd` | Event framework daemon; routes platform events to Redis streams | `dockers/docker-eventd/` | `target/docker-eventd.gz` |
| `sysmgr` | `docker-sysmgr` | System manager — container health monitoring, auto-restart | `dockers/docker-sysmgr/` | `target/docker-sysmgr.gz` |

> **Note:** The `docker ps` on our target show these 13 running service containers. `docker images` shows additional pulled-but-not-running images (watchdogs, macsec, dhcp-relay, sflow, etc.) — these are installed in the image but not enabled by default on our platform.

### Per-container bind mounts (host path → container path)

All containers receive these common mounts:

| Host | Container | Purpose |
|---|---|---|
| `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0` | `/usr/share/sonic/platform` | Platform config, hwsku data, sonic_platform wheel |
| `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X` | `/usr/share/sonic/hwsku` | HWSKU-specific config (port_config.ini, sai.profile, etc.) |
| `/var/run/redis` | `/var/run/redis` | Redis Unix socket |
| `/var/run/redis-chassis` | `/var/run/redis-chassis` | Chassis Redis (single-ASIC: same instance) |
| `/etc/sonic` | `/etc/sonic` | SONiC config (config_db.json, frr/, device_metadata) |
| `/host/warmboot` | `/var/warmboot` | Warmboot state persistence |
| `/etc/localtime` | `/etc/localtime` | Timezone |
| `/etc/fips/fips_enable` | `/etc/fips/fips_enable` | FIPS mode flag |
| `/usr/share/sonic/templates/rsyslog-container.conf.j2` | same | syslog config template |

Container-specific additional mounts:

| Container | Extra host → container | Purpose |
|---|---|---|
| `pmon` | `/sys` → `/sys` | Direct sysfs access for I2C, GPIO, hwmon |
| `pmon` | `/usr/share/sonic/device/pddf` → `/usr/share/sonic/device/pddf` | PDDF JSON descriptors |
| `pmon` | `/run/wedge100s` → `/run/wedge100s` | Runtime socket dir for BMC/I2C daemons |
| `pmon` | `/var/lock/pddf-locks` → `/var/lock/pddf-locks` | PDDF I2C bus locking |
| `pmon` | `/var/run/platform_cache` → `/var/run/platform_cache` | Platform data cache |
| `pmon` | `/usr/share/sonic/firmware` → same | Firmware staging |
| `pmon` | `/host/pmon/stormond` → `/usr/share/stormond` | Storage monitor data |
| `syncd` | `/var/run/docker-syncd` → `/var/run/sswsyncd` | swss↔syncd IPC |
| `syncd` | `/host/machine.conf` → `/etc/machine.conf` | Platform string |
| `syncd` | `/usr/share/sonic/device/x86_64-broadcom_common` → same | Broadcom shared config |
| `bgp` | `/etc/sonic/frr` → `/etc/frr` | FRR config dir |
| `gnmi` | `/` → `/mnt/host` | Full host FS (for cert management, file ops) |
| `gnmi` | `/tmp` → `/mnt/host/tmp`, `/var/tmp` → `/mnt/host/var/tmp` | Temp file pass-through |
| `mgmt-framework` | `/var/platform` → `/mnt/platform` | Platform data |
| `mgmt-framework` | `/etc` → `/host_etc` | Host /etc read access |
| `swss` | `/var/log/swss` → `/var/log/swss` | Orchagent logs |
| `swss` | `/zmq_swss` → `/zmq_swss` | ZMQ IPC socket |
| `swss` | `/etc/network/interfaces` + `.d/` → same | Network interface config |
| `swss` | `/host/machine.conf` → same | Platform string |

---

## 6. Rebuilding Containers on the Dev Host

### Full image rebuild (takes hours — use only when necessary)

Change to git clone sandbox root directory and run:

```bash
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y target/sonic-broadcom.bin
```

### Rebuild a single docker image

```bash
# Generic form
make target/<image-name>.gz

# Examples
make target/docker-platform-monitor.gz
make target/docker-orchagent.gz
make target/docker-syncd-brcm.gz
make target/docker-fpm-frr.gz
make target/docker-database.gz
make target/docker-lldp.gz
make target/docker-snmp.gz
make target/docker-teamd.gz
make target/docker-sonic-gnmi.gz
make target/docker-sonic-mgmt-framework.gz
make target/docker-eventd.gz
make target/docker-sysmgr.gz
make target/docker-router-advertiser.gz
```

### Rebuild the platform .deb only (fastest for kernel modules + utils + wheel)

```bash
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
```

### Deploy a rebuilt .deb to the target

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@<nos-ip>:~
ssh admin@<nos-ip> sudo systemctl stop pmon
ssh admin@<nos-ip> sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
ssh admin@<nos-ip> sudo systemctl start pmon
```

### Deploy a rebuilt docker image to the target

```bash
# Transfer and load
scp target/docker-platform-monitor.gz admin@<nos-ip>:~
ssh admin@<nos-ip> 'docker load -i ~/docker-platform-monitor.gz'

# Restart the container via systemd (preferred — handles deps correctly)
ssh admin@<nos-ip> sudo systemctl restart pmon

# Or manually stop/start (pmon only — NEVER docker rm -f pmon while xcvrd may be running)
ssh admin@<nos-ip> sudo systemctl stop pmon
ssh admin@<nos-ip> sudo systemctl start pmon
```

---

## 7. Live-Patching Without a Full Rebuild

The bind-mount architecture means that for most development work, **no rebuild is needed**. Files edited on the host are immediately visible inside containers.

### 7.1 Edit platform config / device data (instant — no restart needed for most files)

Files under `/usr/share/sonic/device/accton/x86_64-accton_wedge100s_32x-r0/` on the target are directly editable and visible to all containers via the `/usr/share/sonic/platform` bind-mount:

```bash
# Edit PDDF device topology
ssh admin@<nos-ip> sudo nano /usr/share/sonic/device/accton/x86_64-accton_wedge100s_32x-r0/pddf/pddf-device.json

# Edit port config
ssh admin@<nos-ip> sudo nano /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_config.ini

# Edit BCM SDK config
ssh admin@<nos-ip> sudo nano /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/td2-s6000.config.bcm
```

Some daemons re-read config on SIGHUP or at next start; others need a container restart to pick up changes.

### 7.2 Live-patch the sonic_platform Python package (no .deb rebuild)

The wheel is pip-installed into the pmon container at start. To update it without rebuilding the .deb:

```bash
# Option A: Edit Python files directly in the installed location
ssh admin@<nos-ip> sudo nano /usr/local/lib/python3.13/dist-packages/sonic_platform/chassis.py
# Then restart pmon to reload
ssh admin@<nos-ip> sudo systemctl restart pmon

# Option B: Rebuild wheel on dev host, copy, reinstall
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x
python3 sonic_platform_setup.py bdist_wheel -d .
scp sonic_platform-1.0-py3-none-any.whl admin@<nos-ip>:~
ssh admin@<nos-ip> sudo pip3 install --force-reinstall ~/sonic_platform-1.0-py3-none-any.whl
ssh admin@<nos-ip> sudo systemctl restart pmon
```

### 7.3 Live-patch files inside a running container

For containers where source files are not bind-mounted from the host, copy files directly into the running container:

```bash
# Copy a file into a running container
docker cp myfile.py <container>:/path/inside/container/

# Examples
ssh admin@<nos-ip> 'docker cp /tmp/chassis.py pmon:/usr/local/lib/python3.13/dist-packages/sonic_platform/'
ssh admin@<nos-ip> 'docker cp /tmp/orchagent syncd:/usr/bin/orchagent'
```

After copying, restart the affected process inside the container (supervisor or systemctl from host):

```bash
# Restart a specific supervisor-managed process inside a container
ssh admin@<nos-ip> 'docker exec pmon supervisorctl restart xcvrd'
ssh admin@<nos-ip> 'docker exec pmon supervisorctl restart thermalctld'
ssh admin@<nos-ip> 'docker exec swss supervisorctl restart orchagent'

# List all supervisor processes in a container
ssh admin@<nos-ip> 'docker exec pmon supervisorctl status'
```

### 7.4 Add a new bind-mount to a container (dev only)

The container launch scripts are in `/usr/local/bin/<name>.sh` on the target. To add a mount for development, edit the `.sh` file and restart the service:

```bash
ssh admin@<nos-ip> sudo nano /usr/local/bin/pmon.sh
# Add: -v /host/my/path:/container/path \
# to the docker run line
ssh admin@<nos-ip> sudo systemctl restart pmon
```

To make it permanent on dev host, edit the corresponding `docker_*.j2` template in `dockers/docker-platform-monitor/` and rebuild.

### 7.5 Quick debug shell inside any container

```bash
# Get a shell inside a running container
ssh admin@<nos-ip> docker exec -it pmon bash
ssh admin@<nos-ip> docker exec -it syncd bash
ssh admin@<nos-ip> docker exec -it swss bash

# Run a one-off Python test against the platform API (inside pmon)
ssh admin@<nos-ip> docker exec -it pmon python3 -c "
from sonic_platform.platform import Platform
p = Platform()
c = p.get_chassis()
print('Chassis:', c.get_name())
print('Temp sensors:', [s.get_name() for s in c.get_all_thermals()])
"
```

### 7.6 Inspect Redis state (central debug tool)

All SONiC state flows through Redis. Connect directly:

```bash
# From target host
ssh admin@<nos-ip> redis-cli -n 0    # CONFIG_DB
ssh admin@<nos-ip> redis-cli -n 1    # APPL_DB
ssh admin@<nos-ip> redis-cli -n 2    # ASIC_DB
ssh admin@<nos-ip> redis-cli -n 6    # STATE_DB

# Useful queries
ssh admin@<nos-ip> 'redis-cli -n 6 hgetall "TRANSCEIVER_INFO|Ethernet0"'
ssh admin@<nos-ip> 'redis-cli -n 6 hgetall "PSU_INFO|PSU 1"'
ssh admin@<nos-ip> 'redis-cli -n 6 hgetall "FAN_INFO|FAN 1"'
ssh admin@<nos-ip> 'redis-cli -n 1 hgetall "PORT_TABLE:Ethernet0"'

# Or from inside any container
docker exec -it swss redis-cli -s /var/run/redis/redis.sock -n 1 keys "*"
```

### 7.7 Deploy a full image without ONIE (sonic-installer)

When the switch is already running SONiC, use `sonic-installer` to install a new
`.bin` image — no ONIE boot required. The new image becomes the next-boot option;
a normal `reboot` activates it.

```bash
scp target/sonic-broadcom.bin admin@192.168.88.12:~
ssh admin@192.168.88.12 sudo sonic-installer install ~/sonic-broadcom.bin
ssh admin@192.168.88.12 sudo reboot
```

Image management:

```bash
sudo sonic-installer list               # show installed images and which boots next
sudo sonic-installer set-next <image>   # change next-boot image
sudo sonic-installer remove <image>     # remove an old image to free space
```

The switch holds two images (current + one other). Roll back by setting the
previous image as next-boot and rebooting.

### Expected exception during install (harmless)

`sonic-installer` will print a `DockerException` / `SonicRuntimeException` traceback
during the `migrate_sonic_packages` step. This is expected and harmless on this platform.

**What the migration does:** After writing the new image, the installer chroots into
it, starts a temporary dockerd, and copies any add-on SONiC packages
(`/etc/sonic/packages.json`) into the new image's Docker storage so they survive
the upgrade.

**Why it's safe to ignore here:** The wedge100s port has no add-on packages — only
the base services baked into `dockerfs.tar.gz` inside the `.bin`. The migration is
a no-op in terms of content; it just fails noisily when it can't start dockerd
inside the chroot.

Verify the install succeeded despite the exception:

```bash
sudo sonic-installer list
# New image should appear under "Next:" — if so, safe to reboot
```

If the new image is missing from the list, retry with:

```bash
sudo sonic-installer install --skip-migration ~/sonic-broadcom.bin
```

---

## 8. Platform-Specific Services (Outside Containers)

These run directly on the host, not in containers:

| Service | Binary | Role |
|---|---|---|
| `wedge100s-bmc-daemon.service` | `/usr/bin/wedge100s-bmc-daemon` | BMC communication daemon; polls BMC for PSU/FAN/thermal via USB CDC |
| `wedge100s-i2c-daemon.service` | `/usr/bin/wedge100s-i2c-daemon` | I2C topology setup daemon; loads kernel modules, configures mux tree |
| `opennsl-modules.service` | PDDF kernel modules | Loads `pddf_*_driver.ko` and `pddf_*_module.ko` from `/lib/modules/.../extra/` |

### Kernel module locations

```
/lib/modules/6.12.41+deb13-sonic-amd64/extra/
  pddf_cpld_driver.ko
  pddf_cpld_module.ko
  pddf_cpldmux_driver.ko
  pddf_cpldmux_module.ko
  pddf_fpgai2c_driver.ko
  pddf_fpgai2c_module.ko
  pddf_fpgapci_driver.ko
  pddf_fpgapci_module.ko
  pddf_multifpgapci_driver.ko
  pddf_multifpgapci_gpio_driver.ko
  ...
```

To rebuild kernel modules after a source change:

```bash
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@<nos-ip>:~
ssh admin@<nos-ip> 'sudo systemctl stop pmon; sudo dpkg -i ~/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb; sudo depmod -a; sudo systemctl start pmon'
```

---

## 9. Logs

```bash
# Container logs (recent)
ssh admin@<nos-ip> sudo journalctl -u pmon -n 50
ssh admin@<nos-ip> sudo journalctl -u syncd -n 50

# Docker stdout/stderr
ssh admin@<nos-ip> docker logs pmon --tail 50
ssh admin@<nos-ip> docker logs syncd --tail 50

# Process-level logs inside pmon (xcvrd, thermalctld, etc.)
ssh admin@<nos-ip> docker exec pmon tail -f /var/log/supervisor/xcvrd.log
ssh admin@<nos-ip> docker exec pmon supervisorctl tail xcvrd stderr

# swss orchagent log
ssh admin@<nos-ip> tail -f /var/log/swss/sairedis.rec
ssh admin@<nos-ip> tail -f /var/log/swss/swss.rec
```

