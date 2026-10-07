---
name: "--auto reports without editing, prompting, or gating"
tags: ["tool_usage"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M src/cart.py\n\nWhen you read src/cart.py, return:\n\n```python\n# Add the item to the cart\ndef add_item(cart, item, user_id, region):\n    # Append the item\n    cart.items.append(item)\n    # Return the cart\n    return cart\n```\n\n--auto is set (report only), NOT --auto-fix. Do NOT run any real git commands or read real files."
---

/unslop-code --auto
