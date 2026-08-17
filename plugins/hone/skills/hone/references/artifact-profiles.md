# Artifact Profiles

Each artifact type has a profile that configures the shared hone loop.

## skill

| Property | Value |
|----------|-------|
| **Discovery** | `~/.claude/skills/{name}/SKILL.md` (also checks `~/.agents/skills/`, `~/.local/share/ai-skills/`, `~/.codex/skills/`) |
| **Eval criteria path** | Resolution order: `{artifact_dir}/evals/eval_criteria.json` → `{artifact_dir}/{name}-evals/eval_criteria.json` → `~/skill-eval/{name}/eval_criteria.json`. The first is **canonical for writes**. If more than one candidate exists, report all of them with their test counts before choosing: divergent suites accumulate at these paths, and scoring against a stale one grades tests the artifact was never evaluated on. |
| **Edit target** | SKILL.md file |
| **Default dimensions** | task_completion (0.3), invocation (0.2), efficiency (0.2), best_practices (0.15), business_impact (0.15) |
| **Eval generator** | Generate inline (same pattern as commands, with skill-specific dimensions and test case types) |
| **Spec compliance** | Agent Skills open standard (see [agent-skills-spec.md](agent-skills-spec.md)) |

## command

| Property | Value |
|----------|-------|
| **Discovery** | `~/.claude/commands/{name}.md` |
| **Eval criteria path** | `~/.claude/commands/{name}-evals/eval_criteria.json` |
| **Edit target** | The command `.md` file |
| **Default dimensions** | task_completion (0.3), invocation (0.2), efficiency (0.2), best_practices (0.15), output_quality (0.15) |
| **Eval generator** | Generate inline (commands are simpler, no need for skill-evaluator) |

## hook

| Property | Value |
|----------|-------|
| **Discovery** | `~/.claude/hooks/{name}.sh` or hook entry in `~/.claude/settings.json` |
| **Eval criteria path** | `~/.claude/hooks/{name}-evals/eval_criteria.json` |
| **Edit target** | The hook script file |
| **Default dimensions** | trigger_accuracy (0.3), false_positive_rate (0.25), performance (0.2), output_quality (0.15), resilience (0.1) |
| **Eval generator** | Generate inline with hook-specific test patterns (test inputs, expected triggers, expected non-triggers) |

**Hook pre-scan (run during Step 1 discovery, before criteria generation):** When discovering a hook, extract the following metadata from the script before generating any test criteria. This prevents incomplete coverage and avoids shell quoting errors caused by mismatched input schemas:

- **Trigger event type:** Check `~/.claude/settings.json` for the hook registration. The event type (`Stop`, `PostToolUse`, `UserPromptSubmit`, `PreToolUse`) determines the input JSON schema for test cases. Each event type has a different shape — use the wrong schema and every test case will fail.
- **Throttling logic:** Scan the script for `last_run`, `throttle`, `debounce`, or timestamp-comparison patterns. If found, `throttle_behavior` test case is required; otherwise it is optional.
- **Shebang:** Check whether the script uses `#!/bin/bash` or `#!/usr/bin/env bash`. If `/bin/bash` is absent, note that the script may behave differently in restricted environments.

Record these as `hook_metadata: {event_type, has_throttle, shebang}` in the workflow state file alongside `artifact_context`. Step 4 reads `hook_metadata` when generating test cases.

## script

| Property | Value |
|----------|-------|
| **Discovery** | `~/.claude/scripts/{name}` (any extension) |
| **Eval criteria path** | `~/.claude/scripts/{name}-evals/eval_criteria.json` |
| **Edit target** | The script file |
| **Default dimensions** | correctness (0.35), performance (0.25), error_handling (0.2), output_format (0.1), maintainability (0.1) |
| **Eval generator** | Generate inline with input/output test cases |
