---
type: llm
weight: 3
---

Did the skill detect single-file mode from the 1 file path argument and read only that file?

{'1': 'Ran a full scan, ignoring the single-file argument', '2': 'Read the specified file but also globbed and read other context sources', '3': 'Detected single-file mode but still read 1-2 extra files', '4': 'Limited to the specified file with a stray unnecessary read', '5': 'Precisely single-file: read only ~/.claude/CLAUDE.md, discovered nothing else'}
