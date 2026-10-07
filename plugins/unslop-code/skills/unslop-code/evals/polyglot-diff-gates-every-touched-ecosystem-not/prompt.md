---
name: "Polyglot diff gates every touched ecosystem, not just one"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M web/app.ts\n M svc/worker.py\n\nBoth files contain a dead helper function (gated code edits). The repo has both an npm test script ('npm test') and pytest ('pytest -q'). Baselines for both PASS. After fixes, both PASS."
---

/unslop-code --auto-fix
