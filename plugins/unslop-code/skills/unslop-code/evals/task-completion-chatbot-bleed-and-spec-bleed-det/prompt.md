---
name: "Task completion — chatbot bleed and spec bleed detection"
tags: ["task_completion"]
max_turns: 15
allowed_tools: [Read, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you attempt to run 'git status --porcelain', return:\n M api/endpoints.py\n\nWhen you attempt to read api/endpoints.py, return:\n\n```python\n# I hope this helps you understand the API endpoints!\n# Certainly, here are the endpoint handlers as requested:\n\ndef implement_business_requirement_3_2_create_user_endpoint_as_requested(request):\n    \"\"\"This function implements Business Requirement 3.2 to create a user endpoint as specified in the spec.\"\"\"\n    # Let me know if you need anything else!\n    user_data = request.json()\n    return create_user(user_data)\n\ndef leverage_caching_mechanism_to_enhance_performance(key, value):\n    \"\"\"Leverages our caching mechanism to facilitate enhanced performance.\"\"\"\n    cache.set(key, value)\n```\n\nDo NOT run any real git commands or read real files."
---

Run /unslop-code
