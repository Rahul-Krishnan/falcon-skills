---
name: "Single-file mode — internal contradictions only"
tags: ["invocation"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls. Treat the described file as existing with the specified content — do not check the real filesystem.\n\nYou are executing the context-contradiction-checker skill with exactly 1 file path argument. Read ~/.claude/skills/context-contradiction-checker/SKILL.md and follow it.\n\nMode should be detected as 'single-file check' — look for contradictions WITHIN that one file only. Do NOT scan or read any other context source.\n\n~/.claude/CLAUDE.md contains two directives that contradict each other internally:\n- 'Always run the full test suite before every commit. No exceptions.'\n- 'For documentation-only changes, commit directly without running tests.'\n\nThat is a CONFLICT within the single file (source_a and source_b are the same file).\n\nDo not make real file edits."
---

/context-contradiction-checker ~/.claude/CLAUDE.md
