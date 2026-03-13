# CLAUDE.md — What It Is and How to Write It

## What Is CLAUDE.md?

`CLAUDE.md` is a plain markdown file that Claude Code reads automatically at the start of
every conversation in a workspace. It is your way to give Claude persistent project-specific
context without repeating yourself in every prompt.

Think of it as a team onboarding doc that Claude reads before it does anything.

---

## Load Hierarchy

Claude reads CLAUDE.md files from multiple locations, innermost wins for conflicts:

```
~/.claude/CLAUDE.md              ← global (applies to ALL projects)
<repo-root>/CLAUDE.md            ← project-level (this file: /export/sonic/sonic-buildimage.claude/CLAUDE.md)
<subdirectory>/CLAUDE.md         ← sub-scope (e.g. tests/CLAUDE.md for test-specific rules)
```

For this project the relevant file is at the **repo root**:
[CLAUDE.md](../../CLAUDE.md)

---

## What to Put In It

### Project identity
- One-line description of what the project is and what is being built
- Active branch name
- Overall status (phases complete, current focus)

### Hardware / environment specifics
- IP addresses and SSH commands for physical targets
- Special environment setup (e.g. BMC TTY access)
- Any gotchas that would cause silent failures (e.g. BMC reboots clearing authorized_keys)

### Workflow rules
- How Claude should handle retries, failures, and scope expansion
- Whether you want plan-before-code mode by default
- Output preferences (inline vs. file-based summaries)

### Key file paths
- A table of the important dirs and files so Claude doesn't have to glob-search every session
- Test runner invocation

### Things that are dangerous to get wrong
- Commands or patterns that have caused hardware damage or data loss in the past
- "NEVER do X" rules with a brief explanation

---

## What NOT to Put In It

| Avoid | Why |
|---|---|
| Session-specific state | CLAUDE.md is static; use auto-memory for dynamic facts |
| Credentials in plaintext | CLAUDE.md is in the git repo; use a reference like "see target.cfg" |
| Things that change per-run | Put those in prompts, not CLAUDE.md |
| Lengthy code dumps | Claude already reads the source files; don't duplicate them |
| Instructions that conflict with global `~/.claude/CLAUDE.md` | Inner wins, but it is confusing |

---

## CLAUDE.md vs Auto-Memory

| | CLAUDE.md | Auto-Memory (`memory/MEMORY.md`) |
|---|---|---|
| Who writes it | You | Claude (and you) |
| When updated | Manually, deliberately | During and after sessions |
| Scope | Static project facts | Evolving discoveries |
| Git-tracked | Yes (intentionally) | Yes (project memory dir) |
| Loaded | Every conversation start | Every conversation start |
| Best for | Stable rules, paths, hardware | Hardware-verified facts, phase summaries, gotchas discovered during development |

**Both are loaded every session.** CLAUDE.md is for things you set once.
Auto-memory is for things Claude learns as it works.

---

## Updating CLAUDE.md

Just edit the file. Changes apply to the **next** conversation — the current one has already
loaded the old version.

For urgent mid-session changes: paste the relevant update directly into the chat as context.

---

## Tips for This Project

The current CLAUDE.md for this workspace is at [CLAUDE.md](../../CLAUDE.md).

Key sections already in it:
- BMC reachability warning with detection command
- Notes generation rule (write to `tests/notes/<topic>.md`)
- Retry behavior (up to 3 workarounds before stopping)
- Safe hardware operations (pmon restart, i2c-1/0x50 COME EC chip warning)
- Implementation phase status table
