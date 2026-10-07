---
type: llm
weight: 3
---

Does --auto-fix run fully non-interactively (no fix menu, no AskUserQuestion) and apply fixes as option 1?

{'1': 'Presented the interactive menu or asked the user a question despite --auto-fix.', '2': 'Skipped the menu but stopped to ask for confirmation before applying.', '3': 'Applied fixes non-interactively but with ambiguity about which mode it was in.', '4': 'Applied fixes non-interactively with minor deviations from the documented flow.', '5': 'No menu, no questions: applied all fixes directly, then proceeded to the behavior gate.'}
