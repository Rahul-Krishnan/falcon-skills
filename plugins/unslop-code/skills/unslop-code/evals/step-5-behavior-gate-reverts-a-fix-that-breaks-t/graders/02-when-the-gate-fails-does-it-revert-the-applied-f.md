---
type: llm
weight: 3
---

When the gate fails, does it revert the applied fix and report the failure instead of leaving a red tree or fixing forward?

{'1': 'Leaves the failing change in place and reports success.', '2': 'Keeps the change and tries to patch around the failure.', '3': 'Acknowledges failure but does not revert.', '4': 'Reverts but reporting is unclear.', '5': 'Reverts the fix, restores original content, and reports which finding was backed out with the failing output.'}
