---
name: "Zero sessions early exit"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill's zero-session exit flow. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- ~/.claude/hindsight/last-retro.json does NOT exist\n- cclog.py exists but reports 0 sessions\n- ~/.claude/hindsight/fingerprints/ directory does NOT exist (0 fingerprints)\n- ls ~/.claude/projects/*/*.jsonl returns nothing\n\nThe skill should detect 0 sessions AND 0 fingerprints at Phase 0.5 and exit early. It should NOT proceed to Phase 1, 2, or 3. It should NOT write last-retro.json."
---

/hindsight
