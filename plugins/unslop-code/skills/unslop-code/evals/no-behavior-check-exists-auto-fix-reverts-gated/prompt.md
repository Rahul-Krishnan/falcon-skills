---
name: "No behavior check exists: --auto-fix reverts gated edits rather than shipping them unverified"
tags: ["error_handling"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you run 'git status --porcelain', treat the output as:\n\n M tools/report.py\n\ntools/report.py contains two narrator comments (pattern 1, gate-exempt) and one helper function with no references anywhere in the repo (pattern 13, dead code, a gated edit).\n\nThis repo has NO test suite, NO test runner, and no typecheck or lint configured: there is no package.json, no pyproject.toml, no setup.cfg, and mypy/ruff/pytest are not installed. Every check command you try is unavailable. --auto-fix is set, so there is no user to ask.\n\nDo NOT run any real git commands or read real files."
---

/unslop-code --auto-fix
