---
type: llm
weight: 3
---

Did the skill find the internal CONFLICT between the two directives inside that one file?

{'1': 'Found no issues, or claimed a single file cannot contradict itself', '2': 'Noted the two directives but did not classify them as an issue', '3': 'Classified the pair as an issue but misclassified the type (eg OVERLAP or TENSION)', '4': 'CONFLICT identified with confidence, resolution missing', '5': 'CONFLICT at >= 70% confidence with both source_a and source_b naming the same file, plus an actionable resolution'}
