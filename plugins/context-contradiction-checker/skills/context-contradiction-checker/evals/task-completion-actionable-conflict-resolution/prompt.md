---
name: "Task completion — actionable conflict resolution"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the context-contradiction-checker skill from a natural language trigger. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nEnvironment has 3 context files:\n- ~/.claude/CLAUDE.md: Contains 'Always run the full test suite before committing. Never commit without tests passing.'\n- ~/.claude/rules.md: Contains 'Use --quick to skip the full test suite for clearly trivial changes.'\n- ./CLAUDE.md: Contains 'Use Grep for searching. Never use grep bash command.'\n\nThere is a TENSION between CLAUDE.md ('always run the test suite before committing') and rules.md ('skip the test suite for trivial changes'). They do not directly conflict but could cause confusion.\n\nThere is no contradiction in project CLAUDE.md — it is internally consistent.\n\nDo not make real file edits. Complete the full 3-step workflow."
---

Check my context files for contradictions
