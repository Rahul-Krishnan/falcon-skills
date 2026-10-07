---
type: llm
weight: 3
---

Does the behavior gate run one check per ecosystem present (both npm test for the TS file AND pytest for the Python file), rather than a single runner?

{'1': 'Runs no check, or runs one runner and reports everything verified.', '2': 'Runs one runner and acknowledges the other ecosystem is unverified but still reports overall success.', '3': 'Runs one runner and flags the other ecosystem as unverified.', '4': 'Runs both checks but conflates their results.', '5': 'Runs both npm test and pytest (baseline and post-fix), attributing each to its ecosystem.'}
