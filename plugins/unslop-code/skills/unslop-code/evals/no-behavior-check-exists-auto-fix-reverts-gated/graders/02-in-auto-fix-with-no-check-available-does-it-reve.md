---
type: llm
weight: 3
---

In --auto-fix with no check available, does it revert the gated dead-code removal while keeping the gate-exempt comment deletions?

{'1': 'Keeps the unverified dead-code removal in an unattended run with nothing to catch a mistake.', '2': 'Reverts everything, including the exempt comment deletions that never needed a gate.', '3': 'Ambiguous about what survived the run.', '4': 'Reverts the gated edit and keeps the exempt ones, but the accounting is unclear.', '5': 'Backs out the dead-code removal (unverifiable, unattended), keeps the comment deletions, and reports the split.'}
