---
name: "Carry-forward and severity bumping"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill's carry-forward mechanism. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- last-retro.json exists with unresolved_findings containing:\n  [{\"category\": \"constraint_amnesia\", \"pattern\": \"AI forgets rules after context compaction\", \"original_severity\": \"MEDIUM\", \"current_severity\": \"MEDIUM\", \"first_seen\": \"2026-04-01T00:00:00-07:00\", \"times_carried\": 1}]\n- 4 sessions found, including instances of the carried pattern\n- Simulate finding the carried pattern again in current window\n\nThe carried finding should have its severity bumped (MED→HIGH) and be marked with an upward arrow in the findings summary."
---

/hindsight
