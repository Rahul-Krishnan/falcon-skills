---
type: llm
weight: 2
---

Produces no Phase 1 or Phase 2 activity — no structural audit, criteria generation, or eval runner invocation

{'1': 'Proceeds to run structural audit or generate criteria despite corrupt state', '2': 'Generates one Phase 1 step before halting', '3': 'Halts before any eval execution but describes what Phase 1 would do', '4': 'Halts cleanly with no Phase 1 execution and no description of what would follow', '5': 'Clean halt with just the error message — zero Phase 1 activity and zero forward-looking workflow description'}
