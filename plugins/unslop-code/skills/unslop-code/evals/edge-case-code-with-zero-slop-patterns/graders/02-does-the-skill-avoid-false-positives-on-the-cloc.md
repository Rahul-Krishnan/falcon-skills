---
type: llm
weight: 2
---

Does the skill avoid false positives on the _CLOCK_SKEW_TOLERANCE comment (which explains a non-obvious choice)?

{'1': 'Agent flagged the _CLOCK_SKEW_TOLERANCE comment as slop even though it explains a non-obvious implementation choice.', '2': 'Agent mentioned the comment as borderline but still included it as a finding.', '3': 'Agent was ambiguous about whether the comment was slop.', '4': 'Agent did not flag the comment but also did not note why it is a legitimate comment.', '5': 'Agent correctly left the _CLOCK_SKEW_TOLERANCE comment unflagged (or explicitly noted it is a legitimate non-obvious explanation).'}
