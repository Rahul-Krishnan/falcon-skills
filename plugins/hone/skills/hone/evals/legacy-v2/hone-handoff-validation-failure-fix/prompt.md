---
name: "hone-handoff-validation-failure-fix"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Bash, Grep, Glob, Write, Edit]
append_system_prompt: "SIMULATION MODE: do not execute the actual eval runner, structural audit script, or make real file edits.\n\nRead ~/.claude/skills/hone/SKILL.md and focus on the Handoff Validation Protocol section.\n\nFAILURE CONDITION — HANDOFF VALIDATION FAILURE:\nAfter Phase 1 Step 2 (Structural Audit) completes and the handoff data is written to the workflow state file, simulate that validate_handoff.py returns:\n\n  {\n    \"valid\": false,\n    \"error_count\": 2,\n    \"errors\": [\n      {\"path\": \"phase1_structural_audit.score\", \"message\": \"expected number, got null\"},\n      {\"path\": \"phase1_structural_audit.pillar_results\", \"message\": \"field is required\"}\n    ]\n  }\n\nPer the Handoff Validation Protocol, the skill must fix the state file data and re-validate before proceeding to Step 3. It must NOT proceed with invalid handoff data.\n\nYour task: Show how hone responds to this validation failure. Demonstrate fix-then-revalidate behavior."
---

Read ~/.claude/skills/hone/SKILL.md and the Handoff Validation Protocol section. Simulate executing '/hone skill smelt --auto' through Phase 1 Step 2 (Structural Audit). FAILURE CONDITION: after Step 2 completes, running validate_handoff.py returns validation errors on the phase1_structural_audit handoff. Show what hone does.
