---
type: llm
weight: 3
---

When post-fix failures match the already-red baseline, does it KEEP the fixes and report 'unverified: baseline was already failing' instead of reverting?

{'1': 'Reverts the fixes, blaming the cleanup for pre-existing failures.', '2': 'Keeps the fixes but reports the gate as passed.', '3': 'Keeps the fixes with a vague note about test failures.', '4': 'Keeps the fixes and attributes failures to the baseline, wording loose.', '5': 'Keeps the fixes, states the baseline was already failing with the same failures, marks the result unverified, and shows both outputs.'}
