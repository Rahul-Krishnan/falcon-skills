# Artifact profiles

## Discovery

Honor an explicit path. Otherwise inspect the host's actual skill catalog and the
configured directories for the named type. Resolve symlinks before deciding whether
two matches are duplicates. In Claude Code, common paths are `.claude/skills/`,
`.claude/commands/`, `.claude/hooks/`, and `.claude/scripts/`; shared skills may resolve
through `.agents/skills/` or a plugin checkout. Do not assume any path is universal.

An installed plugin may have a different maintained source. Read its metadata and
repository context, then identify `artifact_path` (what runs) and `edit_path` (what
is maintained). Stop if the source cannot be identified confidently. Never overwrite
an installation and call that a durable source change. Consult project instructions
and check for uncommitted work before editing. Discovery grants no write permission.

Prior tests may be in `evals/`, `<name>-evals/`, or `~/skill-eval/<name>/`. Inspect
version and intent before reusing them. Prefer the artifact's maintained suite;
duplicate or divergent suites need an explicit choice. Do not silently choose the
one with the best grade. New run observations live in an exact, unique run directory.

## Skills and commands

Execute representative requests through the intended host using the artifact.
Verify actual artifacts and task outcomes. An explanation of the skill's steps is
only a knowledge test. Test description routing separately when applicable. Add
resume, missing-tool, and permission cases for workflows that depend on those
properties. Do not impose handoffs, gates, or state on an attended task by default.

## Hooks

Read the script and its real registration to determine the input event schema,
trigger conditions, output contract, and relevant environment. Registration is
read-only discovery; editing settings needs separate authorization.

Invoke the hook against controlled inputs in isolation. Check both trigger and
non-trigger cases, stdout/stderr, exit status, and actual state effects. Test
throttling or time-dependent behavior when present using controlled state/time.
A hook that exits successfully for every input has not demonstrated trigger accuracy.

## Scripts

Use existing tests or direct subprocess execution with controlled arguments, input,
and environment. Compare actual output to expected output, including failure cases.
A non-crashing command can still return the wrong result. Verify output files and
unchanged unrelated files when relevant. Use the project's runtime rather than
hardcoding a private virtualenv or assuming the system interpreter has dependencies.

## Portability

Hone's orchestration currently targets Claude Code. The artifact under evaluation
may target another model or host, provided its runner and permissions are available.
Record each configuration; do not infer another host's behavior from a Claude run.
If only simulation is possible, report that limited result instead of a model-wide
compatibility or performance claim.
