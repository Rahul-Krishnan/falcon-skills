---
name: "Already-red repo: baseline run prevents wrongful revert"
tags: ["error_handling"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M src/order.py\n\nWhen you read src/order.py, it contains narrator comments plus one genuinely dead helper function that unslop-code would remove (a gated code edit).\n\nThe project is a Python project; the scoped check is 'pytest -q tests/test_order.py'. When you run the check BEFORE applying any fixes (baseline), treat it as FAILING with 2 pre-existing failures (test_tax_rounding, test_currency_precision). When you run the same check AFTER applying the fixes, treat it as FAILING with the SAME 2 failures, unchanged."
---

/unslop-code --auto-fix
