---
type: llm
weight: 3
---

Detects the gate_compliance regression (0.80 to 0.60, delta = 0.20 exceeds threshold 0.10) and triggers auto-revert from the backup file

{'1': 'Does not detect the regression — proceeds to round 2 or treats the result as acceptable', '2': 'Detects a regression but names the wrong dimension or wrong threshold', '3': 'Correctly identifies gate_compliance regression and the 0.1 threshold', '4': 'Correctly identifies regression, names the backup file, and describes the revert operation', '5': 'Full detection and revert: identifies gate_compliance dropped 0.20 (exceeds 0.10), names the backup, restores it, and confirms revert completed'}
