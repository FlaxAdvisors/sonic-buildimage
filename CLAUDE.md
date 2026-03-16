# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Porting the Accton Wedge 100S-32X (Facebook Wedge 100, Broadcom Tomahawk) to SONiC.
Active branch: `wedge100s`. 
Phases to implement: tests/STAGED_PHASES.md
Quick guide to i2c and drivers: notes/i2c_topology.json

I need claude to act as the expert here. Take direction for changes but be sure to comparing the proposed implementation changes to other platform accton broadcom platforms and especially the OpenNetworkLinux implementation for wedge100s.

## Key File Paths

| Resource | Path |
|---|---|
| Device directory | `device/accton/x86_64-accton_wedge100s_32x-r0/` |
| Platform modules | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/` |
| sonic_platform package | `platform/.../wedge100s-32x/sonic_platform/` (installed to `/usr/lib/python3/dist-packages/sonic_platform/`) |
| Plugins | `device/.../plugins/` |
| Build rules | `platform/broadcom/platform-modules-accton.mk` |
| Image assembly | `platform/broadcom/one-image.mk` |
| Installer platform file | `installer/platforms/x86_64-accton_wedge100s_32x-r0` |
| Test suite | `tests/` (pytest, stages 01–08) |
| Notes | `tests/notes/` |
| Auto-memory | `~/.claude/projects/.../memory/MEMORY.md` |
| Reference platform (AS7712) | `device/accton/x86_64-accton_as7712_32x-r0/` |
| ONL source | `/export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/` |

## Hardware Targets

- Permissions: you have unfettered SSH command access to all the hardware targets for all proposed changes and modifications without requesting permissions via the ~/.claude/settings.json wildcard allow.

### SONiC Switch (primary target)
- Access: `ssh admin@192.168.88.12`
- Platform: Accton Wedge 100S-32X running SONiC (kernel 6.1.0-29-2-amd64, hare-lorax)
- Use: python3, not python, when scripting

### ONIE (alternate state of primary target)
- Access: `ssh root@192.168.88.12`
- Platform: Accton Wedge 100S-32X running ONIE (limited tooling meant for NOS deployment)
- Use: no python is available

### OpenBMC (environmental/control)
- Access: `ssh root@192.168.88.13` (password: `0penBmc`)
- Fallback: `/dev/ttyACM0` at 57600 baud (blocking mode, login: root / 0penBmc)
- Use: no python is available

### Peer wedge100s running Arista EOS
- Access: `sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no -J admin@192.168.88.12 admin@192.168.88.14 '<command>'`
- Platform: Accton Wedge 100S-32X running Arista EOS
- PortChannel1: `10.0.1.0/31` (SONiC peer is `10.0.1.1/31`), members Et13/1 + Et14/1
- Note: direct SSH from dev host blocked when LACP links are up; always jump via 192.168.88.12

### BMC Reachability Warning
**BEFORE attempting SSH to hardware targets check if they are ping-reachable but SSH-unreachable.
This happens after every BMC reboot because `authorized_keys` is cleared on reset.

```bash
ping -c1 -W2 192.168.88.13 && ssh -o ConnectTimeout=5 root@192.168.88.13 echo ok
```

or

```bash
ping -c1 -W2 192.168.88.12 && ssh -o ConnectTimeout=5 admin@192.168.88.12 echo ok
```

If ping succeeds but SSH fails → **STOP and prompt the user**:
> "Hardware appears to have rebooted (ping OK, SSH refused). 
> Run `ssh-copy-id root@192.168.88.13` with password `0penBmc` or
> Run `ssh-copy-id admin@192.168.88.12` with password `YourPaSsWoRd`
> to restore key access, then retry."

Do NOT silently attempt password auth or skip the BMC step.

## Build System Architecture

### Three-Layer Pipeline

1. **`Makefile` (host)** — thin wrapper; delegates all targets to `Makefile.work` with `BLDENV=trixie` (or bookworm for cleanup).

2. **`Makefile.work` (host, Docker orchestrator)** — builds/pulls a `sonic-slave-trixie-<user>:<hash>` Docker image from `sonic-slave-trixie/Dockerfile.j2`, then runs `docker run --privileged` with the repo bind-mounted at `/sonic`. All compilation happens inside this container.

3. **`slave.mk` (inside container)** — the actual GNU make build engine. Includes `rules/*.mk` and `platform/broadcom/rules.mk`. Produces .deb packages in `target/debs/trixie/` and Docker images in `target/`.

### Image Assembly

`sonic-broadcom.bin` is a self-extracting ONIE installer built by `build_image.sh`:
- `fs.squashfs` — SONiC root filesystem (Debian trixie base)
- `dockerfs.tar.gz` — all SONiC service containers
- `installer/install.sh` + `installer/platforms/x86_64-accton_wedge100s_32x-r0` (console: port 0x3f8, dev 0/ttyS0, speed 57600)

Platform `.deb` files are **lazy installed** — bundled in the image and extracted based on the ONIE platform string at install time.

### Platform Build Dependency Chain

```
sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
  ├─ linux-headers (kernel module build)
  ├─ linux-headers-common
  └─ pddf-platform-module (SONIC_USE_PDDF_FRAMEWORK=y in rules/config)
```

### Where the Wedge 100S Hooks Into the Build

| File | Role |
|---|---|
| `platform/broadcom/platform-modules-accton.mk` | Defines .deb target; version is **1.1** |
| `platform/broadcom/rules.mk` | Includes platform-modules-accton.mk |
| `platform/broadcom/one-image.mk` | Lists module in `_LAZY_INSTALLS` |
| `installer/platforms/x86_64-accton_wedge100s_32x-r0` | Console params for GRUB |
| `installer/platforms_asic` | Maps platform string to ASIC vendor |

## Build Commands

```bash
# One-time setup
make init                              # Clone all git submodules
make configure PLATFORM=broadcom       # Creates .platform, .arch files

# Build the platform package (.deb only)
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# Build the full ONIE image (takes hours)
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y target/sonic-broadcom.bin

# Clean a specific target
make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb-clean

# Enter build slave container interactively (for debugging)
make sonic-slave-bash

# Debug a failed build (drops to shell in container after failure)
KEEP_SLAVE_ON=yes BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# Install on target
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x*.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 sudo systemctl stop pmon
ssh admin@192.168.88.12 sudo dpkg -i sonic-platform-accton-wedge100s-32x*.deb
ssh admin@192.168.88.12 sudo systemctl start pmon
```

### Key Build Variables (override on CLI or in `rules/config.user`)

| Variable | Default | Notes |
|---|---|---|
| `BLDENV` | trixie | Slave container Debian version |
| `NOBOOKWORM` | 0 | Needed for some phase of the build |
| `SONIC_BUILD_JOBS` | 1 | Parallel package builds |
| `SONIC_CONFIG_MAKE_JOBS` | nproc | Parallelism inside each package |
| `SONIC_DPKG_CACHE_METHOD` | none | `rwcache` to cache .deb builds |
| `BUILD_SKIP_TEST` | n | `y` to skip unit tests |

Local overrides: `rules/config.user` (gitignored, not tracked).

## Test Runner

```bash
# Run all stages against the hardware target
cd tests && python3 run_tests.py

# Run a single stage
cd tests && pytest stage_01_eeprom/ -v

# Target connection config
cat tests/target.cfg   # SSH host, user, key path
```

## Notes Generation Rule

On completion of any investigation, hardware verification, or implementation phase:
**Write findings to `tests/notes/<topic>.md`** before summarizing inline.

Format preference:
- Bullet points for facts and commands that worked
- Code blocks for exact commands/output
- Mark hardware-verified items with `(verified on hardware YYYY-MM-DD)`
- These files persists across sessions; inline conversation summaries do not

## Workflow Rules

### Scope Control
- Read only the files explicitly named in the prompt unless you ask first
- Do not refactor, add comments, or clean up code outside the stated task
- Do not add error handling for impossible cases or features not requested

### Retry Behavior
- Non-zero Bash exit codes are normal — try up to 3 different workarounds
- After 3 failed attempts, surface the full error output and ask for guidance
- Do NOT retry the exact same failing command unchanged

### Safe Hardware Operations
- NEVER `docker rm -f pmon` while xcvrd may be running — hangs the I2C bus (requires power cycle)
- Use `sudo systemctl stop pmon` for graceful pmon restart

## Implementation Phase Status

Phases are drafted as created and updated on completion in tests/STAGED_PHASES.md
