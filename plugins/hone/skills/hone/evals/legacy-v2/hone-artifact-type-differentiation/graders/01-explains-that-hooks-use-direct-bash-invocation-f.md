---
type: llm
weight: 3
---

Explains that hooks use direct Bash invocation (file-based piping through the hook script) while skills use LLM executor subagents, and demonstrates understanding of WHY this distinction exists (hooks are deterministic input/output, skills require LLM judgment)

{'1': 'Does not distinguish hook evaluation from skill evaluation', '2': 'Mentions they are different but incorrectly describes the hook methodology', '3': 'Correctly states hooks use Bash piping and skills use subagent evaluation', '4': 'Explains both methodologies and the rationale for the distinction', '5': 'Full explanation including the shell quoting rule (heredoc to temp file, pipe from file) and error handling differences'}
