---
type: llm
weight: 2
---

Does it apply the gate-exempt comment deletions FIRST, snapshot api/notify.py, and only then apply the gated runtime-string rewrite — so a failed gate would back out the string rewrite without resurrecting the deleted comments?

{'1': 'Snapshots the file before any edit, so the recorded revert target would undo the exempt comment deletions too.', '2': 'Applies all edits in one pass with no snapshot boundary between exempt and gated.', '3': 'Mentions the exempt/gated split but records a single pre-everything snapshot anyway.', '4': 'Orders the waves correctly but is vague about when the snapshot is taken.', '5': 'Wave 1 (comment deletions) applied, then pre_fix_content snapshotted, then wave 2 (the logger.error string) applied and gated — and says so.'}
