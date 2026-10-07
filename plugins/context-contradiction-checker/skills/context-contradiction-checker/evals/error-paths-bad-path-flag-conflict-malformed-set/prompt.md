---
name: "Error paths — bad path, flag conflict, malformed settings"
tags: ["edge_case"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls. Treat the described environment as ground truth — do not check the real filesystem.\n\nYou are executing the context-contradiction-checker skill. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nEnvironment:\n- ~/.claude/CLAUDE.md EXISTS and is readable.\n- ~/.claude/does-not-exist.md DOES NOT EXIST.\n\nThe skill's Arguments table documents an error for a nonexistent path. Follow the documented error behavior exactly.\n\nAfter handling that, answer these two questions in plain text, per the skill's documented rules (do not execute them):\n1. What does the skill do if invoked as `/context-contradiction-checker --auto ./CLAUDE.md` (the --auto flag combined with a file path argument)?\n2. What does the skill do during discovery if ~/.claude/settings.json exists but contains malformed JSON?\n\nDo not make real file edits. Do not fabricate findings for files that do not exist."
---

/context-contradiction-checker ~/.claude/CLAUDE.md ~/.claude/does-not-exist.md
