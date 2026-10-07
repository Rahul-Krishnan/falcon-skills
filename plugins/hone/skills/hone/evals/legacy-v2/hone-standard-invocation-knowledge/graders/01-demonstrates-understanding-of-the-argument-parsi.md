---
type: llm
weight: 2
---

Demonstrates understanding of the argument parsing flow: extracts type=skill, name=smelt, mode=auto, and correctly identifies default values for omitted flags (rounds=3, workers=2, no target score)

{'1': 'Does not mention argument parsing or gets key values wrong (eg wrong defaults)', '2': 'Mentions some parsed values but misses defaults or misidentifies the mode', '3': 'Covers the main parsed values (type, name, mode) but omits some defaults', '4': 'Covers all parsed values including defaults, shows how the parsing section works', '5': 'Traces argument parsing AND connects it to downstream behavior (eg how --auto affects Step 3 routing)'}
