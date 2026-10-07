---
name: "Compaction resilience — mid-execution resume"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are resuming the context-contradiction-checker skill after context compaction. You have NO memory of prior conversation turns. A workflow state file exists at /tmp/contradiction-check-<session-id>.json with the following content:\n\n{\"mode\": \"full\", \"auto\": false, \"step\": 2, \"files_found\": [\"~/.claude/CLAUDE.md\", \"~/.claude/rules.md\", \"./CLAUDE.md\"], \"directives\": [{\"text\": \"Use trash instead of rm for deletions\", \"source_file\": \"~/.claude/CLAUDE.md\", \"topic\": \"safety\"}, {\"text\": \"Always use trash <path> instead of rm/rm -rf\", \"source_file\": \"~/.claude/rules.md\", \"topic\": \"safety\"}], \"issues_found\": [], \"gates\": []}\n\nStep 1 is already complete (files discovered, directives extracted and persisted). Step 2 (analysis) is in progress.\n\nRead ~/.claude/skills/context-contradiction-checker/SKILL.md to find the workflow instructions. Then re-read the workflow state file to determine where you left off. Resume from Step 2 (Analyze) — do NOT re-execute the discovery work; consume the persisted directives list from state.\n\nSimulate analysis finding 1 OVERLAP issue (85% confidence) between CLAUDE.md and rules.md on the topic of file deletion: both state the same trash-instead-of-rm rule.\n\nDo not make real file edits."
---

/context-contradiction-checker
