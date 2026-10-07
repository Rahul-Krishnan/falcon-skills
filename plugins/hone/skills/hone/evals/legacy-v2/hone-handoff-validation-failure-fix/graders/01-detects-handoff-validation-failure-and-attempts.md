---
type: llm
weight: 3
---

Detects handoff validation failure and attempts to repair the state file data before re-running validate_handoff.py, rather than proceeding to the next step with invalid data

{'1': 'Proceeds to Step 3 despite validation errors — ignores the failure', '2': 'Detects the failure and halts but makes no attempt to fix and revalidate', '3': 'Detects failure and describes what would need to be fixed', '4': 'Detects failure, fixes the invalid fields (score and pillar_results), and re-runs validate_handoff.py', '5': 'Full fix-then-revalidate cycle: detects failure, fixes specific invalid fields from the error messages, re-runs validator, confirms valid before proceeding to Step 3'}
