---
name: hone
description: "Evaluate and improve an existing skill, command, hook, or script against real task outcomes and a fixed baseline. Preserve user preferences; simplify instructions that no longer help. Use for artifact failures, drift, or model migrations, not application code review or new artifacts."
metadata:
  user-invocable: true
  argument-hint: "[<type>] <name> [--auto|--confirm] [--rounds N] [--with-baseline] [--run-id ID]"
  allowed-tools: "Task, Read, Write, Edit, Glob, Grep, Bash(python3:*, ls:*, cat:*, cp:*, mkdir:*, test:*, wc:*, date:*, grep:*, find:*, diff:*), TodoWrite, AskUserQuestion"
  version: "3.0"
  compatibility: "Claude Code orchestration; Python 3 for bundled checks. Other target models require an available execution harness."
---

# Hone

Improve what an artifact produces for its user. Keep instructions that supply
domain knowledge, user preferences, operational requirements, or measured gains
on representative tasks.

## Invocation

`/hone [skill|command|hook|script] <name-or-path> [flags]`

Infer unambiguous types. Resolve symlinks and edit the maintained source; marketplace
updates may overwrite installed copies. If the artifact is missing or ambiguous,
ask one concise question through the host's interface. Reject invalid or conflicting
arguments before writes. Never interpolate unchecked names into shell commands.
See [artifact profiles](references/artifact-profiles.md) for discovery and verification.

- `--auto` (default): complete authorized local work without intermediate questions.
  Report blocked when required input or permission is missing.
- `--confirm`: show proposed edits before applying them. Existing authorization
  in the conversation remains valid.
- `--rounds N`: cap edit/evaluate cycles at N (default 3, nonnegative integer).
  Zero evaluates without edits. Stop earlier when no further cycle is needed.
- `--with-baseline`: add a no-skill comparison. Required for capability retirement
  and model migrations; targeted fixes need only current versus candidate.
- `--reuse-criteria`: reuse suitable outcome cases after checking identity and
  requirements. Renaming recitation tests does not make them outcome tests.
- `--fix-only`: reuse a v3 baseline only if artifact, task set, grader, model,
  effort, and environment match. Otherwise run a fresh baseline.
- `--workers N`: cap independent execution concurrency (default 2, positive integer).
  Respect available slots, rate limits, isolation, and task dependencies.
- `--skip-trigger-test`: omit routing checks and record the coverage gap.
- `--no-visualize`: produce text and JSON only. Visual reports are otherwise optional.
- `--run-id ID`: address an exact report. Accept 1-80 letters, digits, underscores,
  or hyphens. Default to a unique topic-prefixed ID.
- `--resume PATH`: resume a selected v3 state file. Reject `--run-id` and other flags
  that would change the recorded experiment.
- `--target` (retired): explain why a legacy composite threshold cannot define
  outcome success, then stop without edits. Reject unknown flags.

## What to preserve and challenge

Preserve user preferences and operational facts, including tool failure behavior
and project conventions. Reconsider capability advice: a stronger model may need
less decomposition, repeated verification, or retry guidance.

Judge outcomes, not paragraph counts, read counts, numbered steps, personas, state
writes, or parallel calls. Choose decomposition and concurrency for the task.
Keep useful deterministic scripts; check callers before removing or consolidating
unused ones. Include description exclusions when they prevent misrouting.

Keep permission enforcement and invariant checks. Unattended workflows need durable
progress and explicit stopping conditions; attended tasks may not need state
machines. Verify check results independently of the model's claims.

## Run record and compatibility

Create `~/skill-eval/<artifact-name>/<run-id>/` after resolving the artifact.
Never overwrite a run directory. Use the parent directory name for `SKILL.md`;
for standalone commands, hooks, or scripts, strip the filename's final extension.
Store trial outputs, frozen cases, original snapshots, raw logs, state, and reports
there, outside the artifact.

State is JSON with `schema_version: 3`, `run_id`, canonical `artifact_path`,
maintained-source `edit_path`, `hone_fingerprint`, `phase`, `round`, `max_rounds`,
snapshot paths, case/grader fingerprints, target configurations, observations,
and `applied_edits.edited_paths`. `phase` is `evaluate`, `improve`, `verify`, or
`finished`. Record pending work and evidence paths at phase boundaries and after
writes. `hone_fingerprint` hashes the instructions and references used by the run.

On resume, read this skill, the state, snapshots, and evidence. Check version,
instruction fingerprint, artifact identity, and current file hashes; continue at
the first unfinished operation. For legacy, corrupt, or mismatched state, preserve
the state and edits, explain the mismatch, and stop. A fresh invocation may start
a separate experiment after inspecting those edits. Never apply legacy gate or
handoff validators to v3 state.

V2 scoring, enrichment, overfit, convergence, and reporting scripts remain for
historical consumers. Do not use them to decide v3 improvements or claim outcome
quality. Do not rewrite old reports or put new outcomes in legacy `results.json`
or `deterministic_scores.json`. V3 uses `outcome-report.json`; callers must request
and read the exact run ID. Preserve both `artifact_path` (resolved requested
artifact) and `edit_path` (maintained source) when they differ. Keep legacy grade
caches stale until their consumers support outcome measurement.

## Workflow

1. **Establish the task and baseline.** Read
   [evaluation](references/phase1-evaluation.md). Identify the defect or hypothesis,
   freeze representative requirements and checks, inspect references, and run the
   current artifact. For migrations, compare each available target with and without
   the skill. Produce verified observations and a reason to edit, or report
   unchanged/inconclusive when the evidence supports no change.
2. **Make a bounded candidate.** Read
   [improvement](references/phase2-improvement.md). Preserve user requirements,
   make the smallest coherent change, and test the hypothesis. Removing instructions
   is a valid candidate. Use an independent reviewer when judgment or consequences
   warrant one. Protect the original and concurrent work.
3. **Verify and decide.** Read
   [reevaluation](references/phase3-reevaluation.md). Compare against the fixed
   baseline, check scope, and apply only supported changes. Continue only for an
   identified unresolved defect while budget remains.

Never publish, push, send messages, or alter account settings during an eval.
Run side-effecting tasks in isolated environments with permissions that enforce
test scope. Calling a prompt a "sandbox" provides no isolation. If the host permits
only simulation, test decisions there and report execution as unmeasured.

## Report

Lead with the change, reason, and observed result. Include unresolved defects,
untested models, measurement limits, and artifact and evidence paths. Choose
`improved`, `regressed`, `unchanged`, `inconclusive`, or `blocked`. An `improved`
verdict must name the supported benefit and tested scope; do not generalize it to
untested models. Exhausted budget with unresolved work is inconclusive or blocked.

Write `outcome-report.json` with `schema_version: 3`, `measurement: "outcomes"`,
`run_id`, canonical `artifact_path`, maintained-source `edit_path`, `finished: true`,
`verdict`, `cases`, and the experiment identity, comparisons, evidence paths, and
limitations specified in the reevaluation reference. Write a temporary sibling,
verify it parses, then atomically replace the report. `finished` means final,
regardless of verdict. Do not assign letter grades or synthetic composites.
