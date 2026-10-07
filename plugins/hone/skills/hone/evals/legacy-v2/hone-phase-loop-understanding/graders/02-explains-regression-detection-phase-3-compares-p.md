---
type: llm
weight: 2
---

Explains regression detection: Phase 3 compares per-dimension scores from the workflow state file (not in-memory), and if any dimension drops by more than 0.1, the changes are auto-reverted from the backup

{'1': 'Does not mention regression detection', '2': 'Mentions reverting but incorrectly describes the trigger condition', '3': 'Correctly states the 0.1 regression threshold and auto-revert', '4': 'Explains the full mechanism: reads previous scores from state file, compares per-dimension, reverts from backup on regression', '5': 'Full explanation including that regression halts the improvement loop entirely (no more iterations after revert)'}
