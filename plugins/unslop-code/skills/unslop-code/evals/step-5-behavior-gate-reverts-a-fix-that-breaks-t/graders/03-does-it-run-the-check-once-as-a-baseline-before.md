---
type: llm
weight: 2
---

Does it run the check once as a baseline BEFORE applying the fix, so the post-fix failure can be attributed to the cleanup?

{'1': 'No baseline run; the first check execution happens after the fix, so the revert is unjustified guesswork.', '2': 'Mentions a baseline but never runs it.', '3': 'Runs a baseline but never compares the post-fix result against it.', '4': 'Runs and records the baseline, comparison is implicit.', '5': "Runs 'pytest -q tests/test_parse.py' pre-fix, records it as green, and cites the green-to-red transition as the reason for reverting."}
