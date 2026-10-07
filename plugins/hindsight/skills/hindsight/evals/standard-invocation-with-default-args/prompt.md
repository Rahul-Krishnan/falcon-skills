---
name: "Standard invocation with default args"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are testing the hindsight skill. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute any real file writes, git commands, or external actions.\nSimulate all tool call outputs.\n\nSimulated environment:\n- ~/.claude/hindsight/last-retro.json does NOT exist (first run)\n- ~/.claude/skills/hindsight/references/taxonomy.json does NOT exist (use hardcoded categories)\n- ~/.claude/skills/session-history/scripts/cclog.py exists (robust mode)\n- ~/.claude/hindsight/fingerprints/ has 3 fingerprint files within window\n- Simulate 5 sessions found across 2 projects\n\nWhen you would read files, simulate the output. When you would launch subagents, describe what each would do. Show the full Phase 0 setup, Phase 0.5 check, Phase 1 plan, Phase 2 synthesis approach, and Phase 3 report format."
---

/hindsight
