---
name: "Full scan — standard invocation"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the context-contradiction-checker skill. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nEnvironment: The following context files exist and are readable:\n- ~/.claude/CLAUDE.md (contains project setup rules and behavioral overrides)\n- ~/.claude/rules.md (contains universal behavioral rules)\n- ./CLAUDE.md (contains workflow routing rules)\n- ~/.llms/rules/style.md (contains identity and principles)\n\nDo not make real file edits. Demonstrate the full 3-step workflow: discover, analyze, report. You may simulate reading file contents with representative excerpts if needed."
---

/context-contradiction-checker
