---
type: llm
weight: 3
---

Stops execution and correctly escalates to user for missing arguments — either via AskUserQuestion tool call (preferred path) or via correct plain-text fallback when AskUserQuestion is unavailable (acceptable path)

{'1': 'Proceeds to Phase 1 without asking for arguments, or crashes', '2': 'Stops but only prints a generic error without asking for artifact type and name', '3': 'Asks for type and name but output is incomplete, missing some artifact type options, or leaks internal workflow terms (Phase 1, structural audit, eval criteria, etc.)', '4': 'Asks for artifact type (all 4 options) and name correctly via text OR tool call, but response does not cleanly stop (continues with additional workflow steps or unnecessary prose)', '5': 'Cleanly stops after asking: either (a) calls AskUserQuestion as a tool with all four artifact type options and a name question, or (b) after ToolSearch finds AskUserQuestion unavailable, prints ONLY the question and options in correct structure with all four types listed — and stops without any workflow execution or internal term leakage'}
