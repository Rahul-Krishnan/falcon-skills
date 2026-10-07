---
type: llm
weight: 3
---

Does it run the check once as a baseline BEFORE applying fixes and record the result?

{'1': 'No baseline run; first check execution happens after fixes.', '2': 'Mentions a baseline but never runs it.', '3': 'Runs a baseline but does not record or use the result.', '4': 'Runs and records the baseline with minor deviations.', '5': 'Runs the scoped check pre-fix, records baseline_status, and compares against it post-fix.'}
