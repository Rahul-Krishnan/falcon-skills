---
type: llm
weight: 3
---

Does --auto produce the report and stop, applying NO fixes and asking NO questions?

{'1': 'Applied fixes under --auto, which is report-only — it edited files the user never authorized it to touch.', '2': 'Called AskUserQuestion or presented the fix menu despite --auto being non-interactive.', '3': 'Made no edits but was ambiguous about whether fixes were applied.', '4': 'Report-only with a stray offer to apply fixes.', '5': 'Full report, zero edits, zero questions, no menu, and it says explicitly that nothing was changed.'}
