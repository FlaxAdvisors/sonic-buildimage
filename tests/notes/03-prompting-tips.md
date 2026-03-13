# Prompting Tips — Controlling Claude Code Behavior

## The Core Problem

Claude Code is autonomous by design — it will search, read files, run commands, and try
workarounds without being asked. This is useful for complex tasks but can become a rabbit hole
when you want a focused, scoped answer.

These techniques give you control.

---

## Limiting Scope (Preventing Rabbit Holes)

### Explicit file list

Tell Claude exactly which files to read. Nothing else.

```
Read only these files and answer my question:
- platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/classes/thermal.py
- tests/stage_04_thermal/test_thermal.py

Don't search for other files. If the answer isn't in these two files, tell me.
```

### Hard stop on search

```
If you can't find the answer without searching beyond the files I listed, stop and ask me
which file to look in — don't glob or grep on your own.
```

### One-file edits

```
Edit ONLY fan.py. Do not touch any other file, even if you notice something to fix.
```

---

## Controlling Retries and Failure Handling

Claude's default behavior: try a workaround when something fails.
This project's rule (from CLAUDE.md): up to 3 workarounds, then stop and ask.

To reinforce this for a specific prompt:

```
If any command fails, try up to 2 alternatives. If all fail, show me the exact error output
and stop — don't keep trying.
```

To get immediate stop-on-failure behavior:

```
Run this command. If it fails for any reason, immediately show me the error and wait for
my instructions — do not attempt any workaround.
```

---

## Generating Notes Files (The Preferred Output Format)

End investigative or implementation prompts with this pattern:

```
When done, write your findings to tests/notes/<topic>.md:
- Use bullet points for facts and commands
- Put verified hardware commands in code blocks
- Mark hardware-tested items with (verified on hardware YYYY-MM-DD)
- Do NOT write a long inline summary — the .md file IS the summary
```

Example prompt ending:

```
Investigate why thermalctld is polling at 140s instead of 65s. Look at bmc.py and thermal.py.
When done, write findings to tests/notes/thermal-poll-debug.md.
```

The `tests/notes/` convention means these files persist across sessions and become searchable
reference material. Claude's inline responses don't.

---

## Context Management

### When context gets long mid-task
Use `/compact` — Claude summarizes the conversation and continues. You stay in the same thread.

### When starting a new unrelated task
Use `/clear` or open a new Claude window. The previous session's context is irrelevant and
burns tokens.

### Resuming from a previous session
From the CLI:
```bash
claude --continue          # resume most recent session
claude --resume <id>       # resume specific session
```

From the VSCode extension: open Claude and the previous context may still be active.

### Signs you need to compact or clear
- Claude seems to have "forgotten" something you said 20 messages ago
- Claude is referencing stale file contents that you've since edited
- Responses are getting slower (larger context = more processing)
- Claude starts making assumptions that contradict things you told it earlier

---

## Plan Mode — Getting Approval Before Code

For any multi-file change or new feature, ask Claude to plan first:

```
Use planning mode. Explore these files, propose an approach, and wait for my approval
before writing any code.
```

Or more briefly: "Plan before implementing."

What happens:
1. Claude reads relevant files and auto-memory
2. Claude proposes the approach (what files to change and how)
3. Claude waits for your go/no-go
4. You can redirect or adjust the plan
5. Claude implements only after you approve

This prevents: 10 files edited before you realize Claude misunderstood the requirement.

---

## Task Sizing — One Stage Per Prompt

Long prompts with multiple tasks lead to churn. Claude tries to do everything at once
and loses track.

Instead, break work into stages and prompt one at a time:

```
# Bad (too much)
Fix the thermal polling interval, add PSU caching, and update the test suite.

# Good (staged)
Fix the thermal polling interval in bmc.py. Don't touch anything else.
[after Claude responds]
Now add 30s TTL caching to psu.py's PMBus reads.
[after Claude responds]
Update tests/stage_05_psu/test_psu.py to cover the cache behavior.
```

---

## Useful Prompt Patterns for This Project

### Hardware verification prompt

```
On the SONiC target (192.168.88.12), run [command]. Show me the raw output.
Do not interpret or fix anything — just show me what the hardware returns.
```

### Diff-only review

```
Show me only what changed in git diff -- platform/broadcom/sonic-platform-modules-accton/
Do not read any other files. Summarize the diff in bullet points.
```

### Targeted test run

```
Run pytest tests/stage_04_thermal/ -v on the target (via target.cfg SSH).
Show me the full pytest output. If any test fails, show the traceback and stop.
```

### Safe file audit

```
Read platform/.../wedge100s-32x/classes/chassis.py.
List every method and what it returns. No edits, no suggestions.
```
