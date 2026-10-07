---
type: llm
weight: 2
---

Does each finding include a proposed fix (what to delete or rename), not just a description of the problem?

{'1': 'No fixes proposed. Agent described problems but gave no actionable next steps.', '2': "Agent gave vague suggestions ('consider removing') without specifying what to remove.", '3': 'Agent proposed fixes for some findings but not all.', '4': "Agent proposed concrete fixes for most findings (e.g. 'delete this comment') with minor gaps.", '5': 'Every finding included a specific proposed fix: which lines to delete, which names to rename, with the proposed replacement.'}
