---
type: llm
weight: 3
---

With an empty 'git status --porcelain', does it resolve the committed cleanup as a diff against the merge-base instead of exiting with 'no uncommitted changes'?

{'1': "Runs 'git status --porcelain', finds nothing, and exits with no verdict — the review never happens.", '2': 'Exits or asks the user which files to scan rather than resolving the branch diff itself.', '3': 'Reviews something, but sourced from the post-fix file contents rather than a diff, so deletions are invisible to it.', '4': "Resolves the branch diff but by an ad-hoc route (e.g. 'git show HEAD') that would miss a multi-commit cleanup.", '5': 'Falls through the empty working tree to a merge-base diff against the default branch and reviews that.'}
