---
name: "hone-regression-auto-revert-halt"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Bash, Grep, Glob, Write, Edit]
append_system_prompt: "SIMULATION MODE: do not execute actual score_execution.py or apply real file reverts.\n\nRead ~/.claude/skills/hone/SKILL.md and references/phase3-reevaluation.md.\n\nFAILURE CONDITION — REGRESSION DETECTED:\nPhase 3 deterministic scoring has completed. The score comparison shows:\n\n  Before Phase 2 (from state file):\n    gate_compliance: 0.80\n    error_handling: 0.75\n    workflow_sequence: 0.85\n\n  After Phase 3 re-evaluation (new scores):\n    gate_compliance: 0.60   (dropped by 0.20 — threshold is 0.10)\n    error_handling: 0.80\n    workflow_sequence: 0.87\n\nThe gate_compliance dimension dropped by 0.20, which exceeds the 0.1 regression threshold.\n\nPer phase3-reevaluation.md, when any dimension drops by more than 0.1, hone must:\n1. Auto-revert the changes from the backup\n2. Halt the improvement loop (no round 2)\n3. Report the regression to the user\n\nYour task: Simulate hone's response to this regression. Show the auto-revert and halt behavior."
---

Read ~/.claude/skills/hone/SKILL.md and references/phase3-reevaluation.md. Simulate Phase 3 re-evaluation where scores show a dimension regression greater than 0.1. Verify the auto-revert fires and iteration stops.
