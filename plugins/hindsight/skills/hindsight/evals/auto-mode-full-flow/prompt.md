---
name: "Auto mode full flow"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill's --auto mode. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- ~/.claude/hindsight/last-retro.json exists with a previous run from 7 days ago\n- cclog.py exists (robust mode)\n- 8 sessions found, 2 fingerprints\n- Simulate subagent outputs with 6 findings: 1 CRITICAL, 2 HIGH, 2 MEDIUM, 1 LOW\n- The CRITICAL finding is 'unauthorized_actions' with 4 incidents\n\nShow the full --auto flow: auto-apply LOW/MED, log HIGH/CRIT to overnight-flags.md, write report, skip AskUserQuestion entirely."
---

/hindsight --auto
