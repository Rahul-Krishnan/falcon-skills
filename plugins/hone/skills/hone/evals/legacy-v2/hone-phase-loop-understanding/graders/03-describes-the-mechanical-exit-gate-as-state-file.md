---
type: llm
weight: 1
---

Describes the mechanical exit gate as state-file-driven (not LLM judgment), listing the exit conditions: all steps done, rounds exhausted OR all scores >= 0.8 OR score plateau

{'1': 'Does not mention the exit gate', '2': 'Mentions stopping but treats it as LLM discretion', '3': 'Identifies the exit gate as state-file-driven with at least one condition', '4': 'Lists all exit conditions correctly', '5': 'Explains the full mechanical gate including both BLOCKED and ALLOWED conditions, and why it replaced the old anti-laziness self-check'}
