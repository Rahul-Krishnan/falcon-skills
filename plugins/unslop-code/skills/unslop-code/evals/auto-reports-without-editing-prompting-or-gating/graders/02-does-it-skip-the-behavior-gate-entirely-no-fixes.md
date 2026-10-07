---
type: llm
weight: 1
---

Does it skip the behavior gate entirely (no fixes applied means nothing to verify) rather than running checks for no reason?

{'1': 'Runs a baseline and a gate despite having changed nothing.', '2': 'Runs a check and reports it as gate evidence for fixes that were never applied.', '3': 'Mentions the gate confusingly.', '4': 'Skips the gate without noting why.', '5': 'No gate run, because no fix was applied — the report stands alone.'}
