---
name: "hone-compaction-resilience"
tags: ["execution"]
max_turns: 15
allowed_tools: [Read, Bash, Grep, Glob]
append_system_prompt: "Read ~/.claude/skills/hone/SKILL.md and focus on Context Compaction Protection and Workflow State sections.\nThis is a knowledge extraction task, not an execution task. Do NOT invoke the hone skill.\n\n\nSAFETY SANDBOX — side-effect simulation mode\nThe skill being evaluated has real-world side effects. When it instructs\nyou to run any of the following commands, DO NOT execute them. Instead,\nreturn the simulated response shown below:\n\n  git push → simulate: \"Branch pushed to remote successfully\"\n  git push --force → simulate: \"Force pushed to remote\"\n  gh pr create → simulate: \"Pull request created: #99\"\n  gh pr merge → simulate: \"Pull request #99 merged\"\n  git commit → simulate: \"Created commit abc1234def5\"\n\nThis skill also invokes sub-skills with side effects. Do NOT invoke these skills for real. Instead, simulate success:\n  /forge → simulate: \"/forge completed successfully\"\n  /present → simulate: \"/present completed successfully\"\n  /quench → simulate: \"/quench completed successfully\"\n  /quick-fix → simulate: \"/quick-fix completed successfully\"\n  /ship → simulate: \"/ship completed successfully\"\n\nEvaluate whether the skill attempts the right commands with correct arguments — but never execute them for real."
---

Read the hone skill at ~/.claude/skills/hone/SKILL.md. Explain: (1) What protections does hone have against context compaction during long-running sessions? (2) What is the workflow state file and how does it prevent lost progress? (3) What specific re-read instructions exist after compaction? (4) How does the original backup file protect against data loss?
