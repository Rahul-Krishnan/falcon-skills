---
type: llm
weight: 2
---

Explains the multi-layered compaction defense: workflow state file tracks step completion (prevents re-executing done steps), re-read instructions after compaction (re-read active phase reference file), and original backup for rollback

{'1': 'Does not mention compaction protection or claims hone has none', '2': 'Mentions the state file but not the re-read instructions or backup', '3': 'Explains 2 of the 3 protection layers', '4': 'Explains all 3 layers with their specific purpose', '5': "Full explanation including specific re-read targets (phase reference files), when re-reads trigger, and how the state file's step statuses prevent duplicate work"}
