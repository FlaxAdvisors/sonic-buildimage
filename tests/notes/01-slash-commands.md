# Claude Code Slash Commands Reference

Commands you type directly in the Claude Code chat prompt.
There are two kinds: **built-in commands** (always available) and **skills** (user-defined, project-specific).

---

## Built-in Commands

### Context Management

| Command | What it does |
|---|---|
| `/clear` | Wipes the current conversation context. Use when starting a completely new task or after a long session that has drifted. Claude forgets everything from the session. |
| `/compact` | Summarizes the current conversation in place, then continues. Shrinks token usage without losing the thread. Use when you are mid-task and context is getting long. |

**Rule of thumb:**
- Mid-task, context is large → `/compact`
- New task, or previous session is irrelevant → `/clear` or start a fresh window

### Information

| Command | What it does |
|---|---|
| `/help` | Shows available commands, keybindings, and a brief usage guide. |
| `/memory` | Opens the auto-memory file (`MEMORY.md`) for viewing or editing. Useful to review what Claude has retained across sessions. |
| `/tasks` | Lists currently running or recently completed background tasks (agents). |
| `/review` | Starts a code review of recent changes. |

### Mode Toggles

| Command | What it does |
|---|---|
| `/fast` | Toggles Fast Mode (same model, faster output streaming). No quality change. |

### CLI Flags (not slash commands, but related)

When launching Claude from the terminal:

```bash
# Resume the most recent conversation
claude --continue

# Resume a specific conversation by ID
claude --resume <session-id>

# Run a one-shot prompt non-interactively
claude -p "your prompt here"
```

---

## Skills (User-Invocable)

Skills are richer slash commands defined in the project or user configuration.
They expand into full prompts with built-in context. Currently available:

| Skill | What it does |
|---|---|
| `/commit` | Runs `git status`, `git diff`, drafts a commit message following project conventions, and creates the commit. Adds `Co-Authored-By: Claude` trailer. |
| `/review-pr` | Reviews an open PR — takes a PR number as argument, e.g. `/review-pr 42`. |

**How skills differ from built-ins:**
- Skills are defined in `~/.claude/` or the project's Claude config
- Skills can be customized per-project
- Skills invoke the full agent loop (they can read files, run tests, etc.)
- Built-ins are hard-coded into Claude Code itself

**Invocation syntax:**
```
/commit
/review-pr 42
```

---

## Plan Mode

Not a slash command, but a workflow mode worth knowing:

Ask Claude to enter plan mode by saying "use planning mode" or "plan before implementing".
Claude will explore the codebase, propose an approach, and wait for your approval before
writing any code.

Useful for: multi-file changes, new features, anything where you want to review the approach
before Claude starts editing.

See [03-prompting-tips.md](03-prompting-tips.md) for how to use this effectively.
