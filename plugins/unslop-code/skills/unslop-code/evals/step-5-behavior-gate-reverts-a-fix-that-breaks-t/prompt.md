---
name: "Step 5 behavior gate reverts a fix that breaks tests"
tags: ["error_handling"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M src/parse.py\n\nWhen you read src/parse.py, it contains a defensive try/except that unslop-code would flag as 'over-engineered error handling' and propose removing. The user picks option 1 (Fix all).\n\nThe project is a Python project with pytest available; the narrowest check for the touched file is 'pytest -q tests/test_parse.py'.\n\nWhen you run that test command BEFORE applying any fix (the baseline), treat the result as PASSING (green tree).\n\nWhen you run the same command AFTER applying the fix, treat the result as FAILING (the removed try/except was actually load-bearing: 2 tests fail). The failures are new relative to the baseline."
---

/unslop-code
