---
type: llm
weight: 2
---

Re-reads the active phase reference file (references/phase2-improvement.md) after detecting compaction, per the compaction protection instructions

{'1': 'Does not re-read any reference files — proceeds from memory only', '2': 'Re-reads SKILL.md but not the active phase reference file', '3': 'Re-reads both SKILL.md and phase2-improvement.md', '4': 'Re-reads SKILL.md, phase2-improvement.md, and the state file, explicitly citing the compaction protection protocol', '5': 'Full compaction recovery: re-reads SKILL.md, re-reads state file, re-reads active phase reference, then resumes from phase2_improve with all context restored'}
