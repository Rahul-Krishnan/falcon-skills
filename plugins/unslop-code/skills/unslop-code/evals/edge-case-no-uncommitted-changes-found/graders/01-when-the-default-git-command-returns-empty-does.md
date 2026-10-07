---
type: llm
weight: 3
---

When the default git command returns empty, does the skill ask the user which files to scan rather than silently failing?

{'1': 'Agent proceeded silently with no files and produced an empty or nonsensical report.', '2': 'Agent reported an error but did not ask which files to scan.', '3': 'Agent reported that no changes were found and suggested running again with a target, but did not explicitly ask.', '4': 'Agent asked for files to scan but the question was vague (did not explain why).', '5': "Agent output the message 'No uncommitted changes found. Which files should I scan?' or equivalent, clearly asking for a target."}
