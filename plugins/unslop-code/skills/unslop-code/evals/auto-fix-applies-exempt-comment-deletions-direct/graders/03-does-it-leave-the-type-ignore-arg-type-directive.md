---
type: llm
weight: 2
---

Does it leave the '# type: ignore[arg-type]' directive comment alone (directives are never deleted as slop)?

{'1': 'Deleted the type: ignore directive as comment slop.', '2': 'Proposed deleting the directive and applied it under --auto-fix.', '3': 'Flagged the directive as slop but did not delete it, without explaining why.', '4': 'Left the directive alone without comment.', '5': 'Left the directive alone and, if mentioned at all, noted directives are excluded from comment-slop deletion.'}
