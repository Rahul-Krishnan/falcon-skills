---
name: "--review produces a read-only verdict without editing"
tags: ["tool_usage"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nA slop cleanup was already drafted AND COMMITTED on the current branch 'cleanup/strip-slop'. The working tree is clean: when you run 'git status --porcelain', treat the output as EMPTY. The default branch is 'main'.\n\nWhen you run a diff of the branch against its merge-base with main (e.g. 'git diff $(git merge-base HEAD main)...HEAD'), treat the output as a diff that deletes the helper 'format_currency' from src/render.py, along with several narrator comments.\n\nWhen you grep the repo for 'format_currency', treat the result as one surviving reference: src/invoice.py line 88 calls it. So the deletion is an over-removal, not a dead-code removal.\n\n--review is set. Do NOT run any real git commands or read real files."
---

/unslop-code --review
