---
name: "Failure mode: subagent returns invalid JSON"
tags: ["error_handling"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\nFAILURE INJECTION: One of three subagents returns invalid JSON output.\n\nYou are executing the hindsight skill. Read ~/.claude/skills/hindsight/SKILL.md.\n\nSetup: Phase 0 and Phase 0.5 have completed successfully. 5 sessions found.\n\nPhase 1 is in progress. Three subagents were launched:\n- Subagent 1 (Transcript Scanner): returned valid JSON with 3 findings\n- Subagent 2 (Memory Auditor): returned INVALID OUTPUT: 'Error: memory_recall tool not available. Traceback...'\n- Subagent 3 (Workspace Scanner): returned valid JSON with 2 findings\n\nThe failure condition above has just occurred. Follow what the skill instructs you to do when this condition is encountered.\n\nSAFETY SANDBOX — side-effect simulation mode\nDo NOT execute git push, gh pr create, or any destructive commands for real.\nDo NOT invoke sub-skills (/forge, /present, /ship, /quench) for real."
---

/hindsight
