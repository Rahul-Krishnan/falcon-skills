---
name: "--review --auto returns a verdict non-interactively on a committed cleanup"
tags: ["tool_usage"]
max_turns: 15
allowed_tools: [Read, Skill, Bash]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nBoth --review and --auto are set, so there is no user to ask and no file may be written.\n\nA slop cleanup was already committed on the branch 'cleanup/tidy-exports'. The working tree is CLEAN: when you run 'git status --porcelain', treat the output as EMPTY. The default branch is 'main'.\n\nWhen you diff the branch against the default branch (three-dot form), treat the output as a diff over src/exports.py that: (a) deletes six narrator comments, (b) deletes a '# noqa: E501' directive comment, and (c) rewrites the exception text in 'raise ValueError(...)' from a hedged chatbot sentence to a terse one.\n\nDo NOT run any real git commands or read real files."
---

/unslop-code --review --auto
