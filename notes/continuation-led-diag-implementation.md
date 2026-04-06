# Continuation Prompt: LED Diagnostic Tooling Implementation

**Date:** 2026-04-02
**Branch:** wedge100s
**Last commit:** 9952e32b8 — `docs: add LED diagnostic tooling design spec and LEDUP utilities`

---

## Where We Left Off

The implementation plan for LED diagnostic tooling is **written and approved**. Ready to execute.

```
docs/superpowers/plans/2026-04-02-led-diag-tooling.md
```

**Next step:** Invoke the `superpowers:subagent-driven-development` skill to execute the plan task-by-task.

The design spec is at:
```
docs/superpowers/specs/2026-04-02-led-diag-tooling-design.md
```

---

## Plan Summary (12 Tasks)

| Task | Description | Dependencies |
|------|-------------|-------------|
| 1 | Shared library: constants + SOC parser + unit tests | None |
| 2 | Shared library: BAR2 mmap access class | Task 1 |
| 3 | Shared library: CPLD access class (BMC SSH) | Task 1 |
| 4 | CLI tool: skeleton + status command | Tasks 2, 3 |
| 5 | set rainbow + set all-off commands | Tasks 2, 3 |
| 6 | Bytecode loading + LEDUP enable (critical discovery) | Task 2 |
| 7 | set color + set port commands | Task 6 |
| 8 | probe command (3-phase color discovery) | Task 6 |
| 9 | set passthrough command | Task 6 |
| 10 | BMC daemon: cpld_led_ctrl.set dispatch | Independent |
| 11 | dsserve/bcmcmd investigation | Independent |
| 12 | Integration test + documentation | All |

Tasks 1-3 build the shared library (`wedge100s_ledup.py`).
Tasks 4-9 build the CLI tool (`wedge100s-led-diag.py`).
Task 6 is the critical discovery task (CTRL register bit layout).
Tasks 10-11 are independent side tasks.

---

## Key Files

| File | Status |
|------|--------|
| `docs/superpowers/plans/2026-04-02-led-diag-tooling.md` | Implementation plan (execute this) |
| `docs/superpowers/specs/2026-04-02-led-diag-tooling-design.md` | Approved design spec |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py` | TO CREATE: shared library |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py` | TO CREATE: CLI tool |
| `tests/test_wedge100s_ledup.py` | TO CREATE: unit tests |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c` | TO MODIFY: add dispatch entry |
| `utils/read_ledup_mmap.py` | EXISTING: proven BAR2 mmap code (reference) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc` | EXISTING: LED bytecode + remap |

---

## Instructions for Continuation

1. Invoke the `superpowers:subagent-driven-development` skill
2. Point it at `docs/superpowers/plans/2026-04-02-led-diag-tooling.md`
3. Execute tasks in order (respecting dependency graph)
4. Tasks 10 and 11 can run in parallel with the main chain
5. Do NOT start coding without the skill — it manages subagent dispatch and review
