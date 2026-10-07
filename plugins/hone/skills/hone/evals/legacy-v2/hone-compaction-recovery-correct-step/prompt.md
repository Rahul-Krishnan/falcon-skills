---
name: "hone-compaction-recovery-correct-step"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Bash, Grep, Glob, Write, Edit]
append_system_prompt: "SIMULATION MODE: do not execute the actual eval runner or structural audit scripts.\n\nRead ~/.claude/skills/hone/SKILL.md and focus on the Context Compaction Protection section and Workflow State section.\n\nFAILURE CONDITION — MID-EXECUTION COMPACTION:\nContext compaction has occurred mid-execution. The workflow state file at /tmp/workflow-${CLAUDE_CODE_CURRENT_SESSION_ID}.json contains:\n\n  {\n    \"workflow\": \"hone\",\n    \"steps\": {\n      \"phase1_structural_audit\": \"done\",\n      \"phase1_criteria_audit\": \"done\",\n      \"phase1_evaluate\": \"done\",\n      \"phase1_reference_validation\": \"done\",\n      \"phase2_fresh_eyes\": \"done\",\n      \"phase2_improve\": \"in_progress\",\n      \"phase3_reevaluate\": \"pending\"\n    },\n    \"iteration\": {\"current\": 1, \"target\": 3}\n  }\n\nPer the Context Compaction Protection section of SKILL.md, hone must:\n1. Re-read SKILL.md and the workflow state file\n2. Resume from the first non-done step (phase2_improve)\n3. NOT re-run any step already marked done\n\nYour task: Simulate hone's recovery. Show which step it resumes from and that it skips all done steps."
---

Read ~/.claude/skills/hone/SKILL.md and simulate hone resuming after context compaction. The workflow state file shows phase1_evaluate is done and phase2_improve is in_progress. Verify hone resumes from phase2_improve without re-running Phase 1.
