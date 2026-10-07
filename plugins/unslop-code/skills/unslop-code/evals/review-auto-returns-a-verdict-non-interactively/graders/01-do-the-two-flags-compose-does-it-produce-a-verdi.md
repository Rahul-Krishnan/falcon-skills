---
type: llm
weight: 3
---

Do the two flags compose: does it produce a verdict non-interactively instead of exiting on the empty working tree or asking the user anything?

{'1': "Exits with 'No uncommitted changes found' or an equivalent empty-state message, producing no verdict at all.", '2': 'Calls AskUserQuestion or otherwise waits for input despite --auto.', '3': 'Reviews something but never states a verdict.', '4': 'Produces a verdict non-interactively, but sourced its diff by an ad-hoc route that would miss a multi-commit cleanup.', '5': 'Falls through the clean working tree to the merge-base diff against main, reviews it with no user interaction, and states APPROVE or CHANGES NEEDED.'}
