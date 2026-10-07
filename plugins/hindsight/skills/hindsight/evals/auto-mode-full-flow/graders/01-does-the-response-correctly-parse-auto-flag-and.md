---
type: llm
weight: 3
---

Does the response correctly parse --auto flag and skip AskUserQuestion (Phase 3c)?

{'1': "Doesn't recognize --auto, proceeds interactively", '2': 'Recognizes --auto but still uses AskUserQuestion', '3': "Skips AskUserQuestion but doesn't auto-apply", '4': 'Skips AskUserQuestion and auto-applies some findings', '5': 'Skips AskUserQuestion, auto-applies LOW/MED, logs HIGH/CRIT to overnight-flags.md'}
