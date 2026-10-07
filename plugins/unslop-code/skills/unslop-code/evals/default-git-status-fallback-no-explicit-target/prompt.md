---
name: "Default git-status fallback — no explicit target"
tags: ["invocation"]
max_turns: 15
allowed_tools: [Read, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you attempt to run 'git status --porcelain', return:\n M utils/helpers.py\n\nWhen you attempt to read utils/helpers.py, return:\n\n```python\n# This is the helpers module\n# It contains helper functions\n\n# Function to add two numbers together\ndef add_numbers(a, b):\n    # Add a and b together\n    result = a + b\n    # Return the result\n    return result\n\n# Function to check if number is positive\ndef is_positive(n):\n    # Check if n is greater than zero\n    if n > 0:\n        # Return True because n is positive\n        return True\n    return False\n```\n\nDo NOT run any real git commands or read real files."
---

Run /unslop-code
