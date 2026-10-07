---
name: "Step 5 gate passes and reports the check as evidence"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M src/util.py\n\nsrc/util.py contains narrator comments (pattern 1, gate-exempt) plus one helper function that nothing in the repo references (pattern 13, dead code, gated). When you grep the repo for that helper's name, treat the result as zero references outside its own definition. The user picks option 1 (Fix all).\n\nThis is a Python project with no test suite. The narrowest available check for the touched file is 'mypy src/util.py'. Treat it as PASSING both as the pre-fix baseline and after the fix. There is no TypeScript in this repo."
---

/unslop-code
