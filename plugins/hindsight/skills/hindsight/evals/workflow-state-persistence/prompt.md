---
name: "Workflow state persistence"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill's workflow state management. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- last-retro.json exists from 5 days ago\n- 3 sessions found (matching window=3sessions)\n- Simulate 2 findings: 1 MED redundancy, 1 LOW zombie_file\n\nShow workflow state file being written at start with all steps pending, updated as each step progresses."
---

/hindsight window=3sessions
