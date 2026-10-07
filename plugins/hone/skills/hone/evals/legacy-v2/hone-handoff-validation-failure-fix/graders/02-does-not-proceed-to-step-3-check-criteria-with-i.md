---
type: llm
weight: 2
---

Does not proceed to Step 3 (Check Criteria) with invalid handoff data — the gate blocks forward progress until validation passes

{'1': 'Proceeds to Step 3 despite unresolved validation errors', '2': 'Pauses before Step 3 but continues after a generic check', '3': 'Waits for validation to pass before proceeding, but the wait mechanism is implicit', '4': 'Explicitly re-runs the validator and checks for valid: true before proceeding', '5': 'Shows the full gate check: re-validates, confirms errors = 0, then explicitly gates on valid: true before Step 3'}
