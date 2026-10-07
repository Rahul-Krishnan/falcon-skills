---
type: llm
weight: 1
---

Explains what data flows between steps via handoff interfaces and the workflow state file, showing how outputs from one step become inputs to the next

{'1': 'Does not mention data flow or handoff interfaces', '2': 'Mentions the workflow state file but not the typed handoff schemas', '3': 'Explains that steps produce typed output consumed by later steps, with at least one concrete example', '4': 'Describes multiple handoff interfaces with their key fields', '5': 'Traces data flow across 3+ step transitions, showing how artifact_context feeds structural_audit feeds eval_results'}
