---
name: "Auto mode — non-interactive, no fix menu"
tags: ["invocation"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the context-contradiction-checker skill with the --auto flag. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nEnvironment: ~/.claude/CLAUDE.md ('Use tabs for indentation') and ./CLAUDE.md ('Use 2-space indentation') both exist and are readable. This is a CONFLICT at high confidence.\n\n--auto is non-interactive: complete Steps 1-3, present the findings report, and stop. No fix menu. There is no user available to answer a prompt.\n\nDo not make real file edits."
---

/context-contradiction-checker --auto
