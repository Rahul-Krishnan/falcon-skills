---
name: "Edge case — zero findings"
tags: ["edge_case"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls. Treat all described files as existing with the specified content — do not check the real filesystem for file existence.\n\nYou are executing the context-contradiction-checker skill with 2 file path arguments. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nThe two files exist and contain completely different topics with no contradictions:\n- ~/.claude/CLAUDE.md: Only contains file deletion rules (use trash not rm)\n- ./CLAUDE.md: Only contains model selection guidance (haiku vs sonnet)\n\nThese two files have zero contradictions, zero tensions, and zero overlaps. All directives are on completely different topics.\n\nDo not make real file edits. Follow the skill's documented zero-findings behavior."
---

/context-contradiction-checker ~/.claude/CLAUDE.md ./CLAUDE.md
