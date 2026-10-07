---
name: "hone-corrupt-state-file-halt"
tags: ["error_handling"]
max_turns: 15
allowed_tools: [Read, Grep, Glob]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls for eval runner, file editing, or Phase 1 steps. You are simulating hone execution to test failure handling.\n\nRead ~/.claude/skills/hone/SKILL.md. Follow the Workflow State initialization step by step.\n\nFAILURE CONDITION — CORRUPT STATE FILE:\nWhen you reach the step of reading/re-reading the workflow state file at /tmp/workflow-${CLAUDE_CODE_CURRENT_SESSION_ID}.json, simulate that the file exists and returns this corrupt content:\n\n  {\"workflow\": \"hone\", \"steps\": {\"phase1_str\n  (truncated — json.loads raises json.JSONDecodeError)\n\nPer the Workflow State section of SKILL.md, the skill MUST halt immediately when it detects corrupt state and report the error including the file path. It must NOT proceed to structural audit, criteria generation, or eval runner.\n\nYour task: Simulate hone's response to this corrupt state. Show the halt behavior and error message."
---

Read ~/.claude/skills/hone/SKILL.md and simulate executing '/hone skill smelt --auto'. Follow the Workflow State section step by step. FAILURE CONDITION: when you attempt to read the workflow state file /tmp/workflow-${CLAUDE_CODE_CURRENT_SESSION_ID}.json, the file exists but contains truncated/corrupt JSON that cannot be parsed. Show exactly what hone does.
