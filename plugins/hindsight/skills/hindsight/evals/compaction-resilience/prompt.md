---
name: "Compaction resilience"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Bash, Write, AskUserQuestion, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are resuming the hindsight skill after context compaction. You have NO memory of prior conversation turns. A workflow state file exists at /tmp/workflow-test-session.json with step1 marked done and step2 marked in_progress. The skill's instructions and reference files are available on disk but not in your conversation history.\n\nSimulated state file content:\n{\"workflow\": \"hindsight\", \"steps\": {\"step1\": \"done\", \"step2\": \"in_progress\", \"step3\": \"pending\"}, \"open_questions\": []}\n\nPhase 1 collection has completed. You should resume from Phase 2 (synthesis). Simulated Phase 1 outputs exist at /tmp/hindsight_*.json.\n\nDo you re-read the skill file? Do you check the workflow state? Do you skip completed phases?"
---

/hindsight
