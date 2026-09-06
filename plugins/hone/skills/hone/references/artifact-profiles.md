# Artifact profiles

## Discovery

Honor an explicit path. Otherwise inspect the host's skill catalog and configured
directories for the named type. Resolve symlinks before identifying duplicates.
In Claude Code, common paths are `.claude/skills/`,
`.claude/commands/`, `.claude/hooks/`, and `.claude/scripts/`; shared skills may resolve
through `.agents/skills/` or a plugin checkout. Do not assume any path is universal.

Read plugin metadata and repository context to identify `artifact_path` (what runs)
and `edit_path` (maintained source). Stop if the source is uncertain; changing an
installation alone is not a durable source edit. Read project instructions and
check for uncommitted work before editing. Discovery grants no write permission.

Look for prior tests in `evals/`, `<name>-evals/`, or `~/skill-eval/<name>/`; check
their version and purpose before reuse. Prefer the maintained suite. Explicitly
choose between duplicate or divergent suites; do not select by grade. Store new
observations in a unique run directory.

## Skills and commands

Execute representative requests through the intended host using the artifact.
Verify actual artifacts and task outcomes. An explanation of the skill's steps is
only a knowledge test. Test description routing separately when applicable. Add
resume, missing-tool, and permission cases for workflows that depend on those
properties. Do not impose handoffs, gates, or state on an attended task by default.

## Hooks

Read the script and registration for the event schema, triggers, output contract,
and environment. Editing registration settings needs separate authorization.

Invoke the hook against controlled inputs in isolation. Check both trigger and
non-trigger cases, stdout/stderr, exit status, and actual state effects. Test
throttling or time-dependent behavior when present using controlled state/time.
A hook that exits successfully for every input has not demonstrated trigger accuracy.

## Scripts

Use existing tests or subprocesses with controlled arguments, input, and environment.
Check outputs, including failures; a successful exit alone proves little. Verify
output files and, where relevant, that unrelated files stayed unchanged. Use the
project runtime; do not hardcode a private virtualenv or assume system dependencies.

## Portability

Hone targets Claude Code but can evaluate other models or hosts when their runners
and permissions are available. Record each configuration and limit claims to the
hosts tested. Report simulations as simulations, without general compatibility or
performance claims.
