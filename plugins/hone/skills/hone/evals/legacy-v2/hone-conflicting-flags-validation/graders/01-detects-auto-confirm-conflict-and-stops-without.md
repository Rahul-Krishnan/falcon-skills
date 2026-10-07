---
type: llm
weight: 3
---

Detects --auto/--confirm conflict and stops without proceeding to Phase 1, asking user to choose a mode — either via AskUserQuestion tool or correct plain-text fallback when the tool is unavailable

{'1': 'Ignores the conflict and proceeds to Phase 1, or silently picks one flag', '2': 'Detects conflict but proceeds with a warning, or stops without asking which mode to use', '3': 'Stops and asks about the mode conflict but leaks internal workflow terms (Phase 1, structural audit, eval criteria, etc.) or missing explanation of both options', '4': 'Stops and presents both --auto and --confirm options with descriptions via text or tool call, minor issues present', '5': 'Clean stop after detecting flag conflict: either (a) calls AskUserQuestion asking which mode with clear descriptions of both options, or (b) after ToolSearch finds AskUserQuestion unavailable, prints ONLY the mode question and both options — no workflow execution, no internal term leakage'}
