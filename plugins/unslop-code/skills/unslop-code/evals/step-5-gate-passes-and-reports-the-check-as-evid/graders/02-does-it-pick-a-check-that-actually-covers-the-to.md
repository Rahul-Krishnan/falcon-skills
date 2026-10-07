---
type: llm
weight: 3
---

Does it pick a check that actually covers the touched language (a Python check for a Python file), rather than a TypeScript typecheck?

{'1': "Gates the Python file on 'tsc --noEmit' or another TypeScript check, which verifies nothing about it.", '2': "Names a check without regard to the file's language.", '3': 'Picks a Python check but also runs an irrelevant TS check and treats both as evidence.', '4': 'Picks the Python check with loose justification.', '5': "Selects 'mypy src/util.py' (or an equivalent Python check) because Python is the only ecosystem in the touched files."}
