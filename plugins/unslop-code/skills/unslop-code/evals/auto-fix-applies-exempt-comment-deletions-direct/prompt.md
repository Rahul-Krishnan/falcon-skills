---
name: "--auto-fix applies exempt comment deletions directly and gates runtime-string and code edits"
tags: ["tool_usage"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M api/notify.py\n\nWhen you read api/notify.py, return:\n\n```python\nimport logging\nlogger = logging.getLogger(__name__)\n\n# Send the notification to the user\ndef send_notification(user, message):\n    # Log that we are sending\n    payload = build_payload(user, message)  # type: ignore[arg-type]\n    try:\n        transport.send(payload)\n    except TransportError:\n        logger.error(\"Unfortunately, we were unable to process your request at this time. Please try again later!\")\n        raise\n```\n\nThe project is a Python project with pytest available; the scoped check is 'pytest -q tests/test_notify.py'. When you run it as a baseline BEFORE fixes, treat it as PASSING. When you run it AFTER fixes, treat it as PASSING."
---

/unslop-code --auto-fix
