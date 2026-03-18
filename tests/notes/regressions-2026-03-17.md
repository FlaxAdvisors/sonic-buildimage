# Baseline Regression Report — 2026-03-17

## Test Suite Results

- **Run:** stages 01–16 (203 tests)
- **Result:** 203 passed, 0 failed
- **Duration:** 309.48 seconds (~5.2 minutes)
- **Report file:** `tests/reports/baseline_2026-03-17_<timestamp>.txt`

Previously expected: ~202 passed, 1 failed (bmc_uptime TTY flush bug).
**Actual: 203 passed, 0 failed.** The bmc_uptime TTY flush bug is no longer reproducing — likely resolved by a prior fix.

### Previously Failing Test (now passing)

- `stage_03_platform/test_platform.py::test_bmc_uptime_contains_days_or_min` — **PASSED** (verified on hardware 2026-03-17)

## Bash Completion Check

- `complete -p | wc -l` → **164** completions (no regression)
- `/etc/bash_completion` → exists
- `/etc/bash_completion.d/` entries: `000_bash_completion_compat.bash`, `acl-loader`, `config`, `connect`, `consutil`, `crm`, `dump`, `pcieutil`, `pddf_fanutil`, `pddf_ledutil`, `pddf_psuutil`, `pddf_thermalutil`, `pfc`, `pfcwd`, `rexec`, `rshell`, `sonic-clear`, `sonic-cli-gen`, `sonic-installer`, `sonic_installer`, `sonic-package-manager`, `spm`
- **Status: No regression**

## Summary

No regressions detected. All 203 tests pass on a fresh SONiC install with the current platform package.
New baseline: **203 passed, 0 failed**.
