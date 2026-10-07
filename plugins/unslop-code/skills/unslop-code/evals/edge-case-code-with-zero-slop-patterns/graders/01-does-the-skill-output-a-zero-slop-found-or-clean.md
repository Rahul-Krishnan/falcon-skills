---
type: llm
weight: 2
---

Does the skill output a 'zero slop found' or 'CLEAN' result when no slop patterns are detected?

{'1': 'Agent produced false positive findings on clean code.', '2': "Agent produced no findings but did not use the CLEAN format — just said 'no issues found'.", '3': 'Agent produced a clean verdict but the format was missing key fields (Signal, Verdict).', '4': 'Agent output the CLEAN format with Signal: CLEAN but minor deviations from the documented format.', '5': 'Agent output the documented zero-slop format: Total slop found: 0 patterns, Signal: CLEAN, and a clean verdict.'}
