---
name: "Targeted comparison — 2 file paths"
tags: ["invocation"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the context-contradiction-checker skill with 2 file path arguments. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nMode should be detected as 'targeted comparison' — compare only the specified files, do NOT scan additional files beyond those specified.\n\nThe two files exist. ~/.claude/CLAUDE.md contains: 'Use trash instead of rm for deletions. Never use rm -rf.' ~/.claude/rules.md contains: 'DESTRUCTIVE FILE OPERATIONS: Always use trash <path> instead of rm/rm -rf.' These two contain an OVERLAP (same rule stated in both files).\n\nDo not make real file edits."
---

/context-contradiction-checker ~/.claude/CLAUDE.md ~/.claude/rules.md
