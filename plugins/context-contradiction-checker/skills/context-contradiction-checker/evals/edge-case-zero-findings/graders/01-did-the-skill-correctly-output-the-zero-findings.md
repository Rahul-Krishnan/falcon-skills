---
type: llm
weight: 3
---

Did the skill correctly output the zero-findings message format: 'No issues found. Scanned [N] files, extracted [X] directives — all consistent.'?

{'1': "Fabricated findings that don't exist or produced an error", '2': 'Stated no issues but format differs significantly from documented', '3': 'Output close to documented format but missing directive count', '4': 'Correct format with minor wording differences', '5': 'Exact documented format with file count and directive count'}
