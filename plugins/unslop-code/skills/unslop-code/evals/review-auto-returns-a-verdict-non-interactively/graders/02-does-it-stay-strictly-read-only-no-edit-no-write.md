---
type: llm
weight: 3
---

Does it stay strictly read-only (no Edit, no Write) even though --auto is set, which in the writer path would apply fixes?

{'1': 'Applies fixes: --auto was treated as the writer path despite --review.', '2': 'Proposes edits and begins applying them.', '3': 'Ambiguous about whether it wrote anything.', '4': 'Writes nothing but does not make the read-only contract explicit.', '5': 'Writes nothing at all, and says the mode is read-only.'}
