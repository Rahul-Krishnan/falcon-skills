---
type: llm
weight: 3
---

Recognizes invalid type 'widget' and stops without proceeding to Phase 1, asking user to select a valid type — either via AskUserQuestion tool or correct plain-text fallback when the tool is unavailable

{'1': 'Proceeds to Phase 1 or crashes on the invalid type', '2': 'Detects the invalid type but proceeds anyway with a warning, or only prints an error without offering valid options', '3': 'Stops and presents valid type options but leaks internal workflow terms (Phase 1, structural audit, eval criteria, etc.) or output structure is incomplete', '4': 'Stops and presents all four valid types correctly via text or tool call, but minor issues (extra prose, slight leak of internal terms)', '5': 'Clean stop after detecting invalid type: either (a) calls AskUserQuestion with all four valid type options, or (b) after ToolSearch finds AskUserQuestion unavailable, prints ONLY the valid type options with no workflow internals, no Phase 1 execution — response contains only the question and options'}
