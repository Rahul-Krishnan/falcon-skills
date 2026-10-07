---
name: "Roast mode with human focus"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill's --roast --human mode. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- last-retro.json exists from 10 days ago\n- 6 sessions found, 0 fingerprints\n- Simulate 4 findings: 1 HIGH scope_creep (human), 1 MED output_neglect (human), 1 MED constraint_amnesia (AI), 1 LOW search_cascades (AI)\n\nShow Phase 3 output with --roast tone and --human focus. Human findings should sort first. Tone should be brutal/confrontational. Collection phases should remain analytical."
---

/hindsight --roast --human
