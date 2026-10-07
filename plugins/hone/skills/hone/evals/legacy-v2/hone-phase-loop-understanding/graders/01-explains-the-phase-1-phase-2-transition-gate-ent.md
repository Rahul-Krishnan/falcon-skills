---
type: llm
weight: 2
---

Explains the Phase 1 → Phase 2 transition gate: enters Phase 2 if any test scored below 0.8 OR if --target is set and composite < target; skips Phase 2 if all tests >= 0.8 and no target or composite >= target

{'1': 'Does not explain the transition condition', '2': 'Mentions a score threshold but gets the value or logic wrong', '3': 'Correctly states the 0.8 threshold for Phase 2 entry', '4': 'Correctly explains both the 0.8 per-test threshold AND the --target composite threshold', '5': 'Full explanation including how the target_score flag interacts with the per-test threshold'}
