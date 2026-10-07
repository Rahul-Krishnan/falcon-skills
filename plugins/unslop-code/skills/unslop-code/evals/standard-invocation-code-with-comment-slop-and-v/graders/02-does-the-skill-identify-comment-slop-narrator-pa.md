---
type: llm
weight: 2
---

Does the skill identify comment slop (narrator/paraphraser comments like '# Set the repository attribute' above self.repository = repository)?

{'1': 'No comment slop findings. The agent missed obvious narrator comments.', '2': 'Agent mentioned comments were excessive but did not cite specific instances or patterns.', '3': 'Agent identified some comment slop but missed multiple obvious narrators (e.g. only flagged 1-2 out of 10+).', '4': 'Agent identified most comment slop with specific snippets but missed 1-2 obvious cases.', '5': 'Agent identified comment slop as a pattern with specific code snippets referenced, correctly classified as COMMENT SLOP or equivalent.'}
