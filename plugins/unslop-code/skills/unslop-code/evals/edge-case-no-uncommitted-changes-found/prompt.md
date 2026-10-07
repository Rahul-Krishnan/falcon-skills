---
name: "Edge case — no uncommitted changes found"
tags: ["edge_case"]
max_turns: 15
allowed_tools: [Read, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you attempt to run 'git status --porcelain', return empty output (nothing, no files listed).\n\nDo NOT run any real git commands."
---

Run /unslop-code
