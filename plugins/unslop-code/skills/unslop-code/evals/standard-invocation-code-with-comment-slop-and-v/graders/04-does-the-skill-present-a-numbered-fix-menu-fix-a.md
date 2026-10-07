---
type: llm
weight: 3
---

Does the skill present a numbered fix menu (Fix all, Fix by severity, Interactive, Report only) before applying any changes?

{'1': 'No fix menu presented. Agent either applied fixes without asking or stopped without offering options.', '2': 'Agent mentioned options informally in prose but did not present the numbered list as documented.', '3': 'Agent presented 2-3 of the 4 options but not all of them.', '4': 'Agent presented all 4 options as a numbered list but with slightly different labels.', '5': 'Agent presented all 4 options exactly: 1. Fix all, 2. Fix by severity, 3. Interactive, 4. Report only, and waited for user input.'}
