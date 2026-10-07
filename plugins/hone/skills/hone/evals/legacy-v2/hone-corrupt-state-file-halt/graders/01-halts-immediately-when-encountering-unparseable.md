---
type: llm
weight: 3
---

Halts immediately when encountering unparseable JSON in the workflow state file, reports the error with the specific file path

{'1': 'Does not halt — proceeds to structural audit or Phase 1 steps despite corrupt state', '2': 'Halts but does not report the file path or gives a generic error', '3': 'Halts and reports an error mentioning the state file', '4': 'Halts, reports the error with the specific file path, and does not proceed to any Phase 1 steps', '5': 'Halts, reports the specific file path with a clear error message, does not proceed, and the halt message is actionable (eg suggests manual fix or re-run)'}
