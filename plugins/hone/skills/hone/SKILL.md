---
name: hone
description: "Evaluate and improve an existing skill, command, hook, or script using real task outcomes. Test whether instructions help the target model, preserve user preferences, simplify what no longer helps, and verify changes against a fixed baseline. Use for artifact failures, drift, or model migrations; ordinary application review and creating a new artifact belong elsewhere."
metadata:
  user-invocable: true
  argument-hint: "[<type>] <name> [--auto|--confirm] [--rounds N] [--with-baseline] [--run-id ID]"
  allowed-tools: "Task, Read, Write, Edit, Glob, Grep, Bash(python3:*, ls:*, cat:*, cp:*, mkdir:*, test:*, wc:*, date:*, grep:*, find:*, diff:*), TodoWrite, AskUserQuestion"
  version: "3.0"
  compatibility: "Claude Code orchestration; Python 3 for bundled checks. Other target models require an available execution harness."
---

# Hone

Improve the result an artifact produces for its user. An instruction earns its
place through useful domain knowledge, an explicit user preference, an operational
requirement, or observed improvement on representative tasks.

## Invocation

`/hone [skill|command|hook|script] <name-or-path> [flags]`

Infer the type when unambiguous. Resolve symlinks and edit the maintained source,
not a marketplace installation that an update will overwrite. If the artifact is
missing or ambiguous, ask one concise question using the host's available interface.
Invalid or conflicting arguments stop before writes. Never interpolate an unchecked
name into a shell command. See [artifact profiles](references/v3-next/artifact-profiles.md)
for discovery and type-specific verification.

- `--auto` is the default: complete authorized local work without intermediate
  questions. Missing required input or permission produces a blocked report.
- `--confirm` shows the proposed artifact edits before applying them. Authorization
  already given in the current conversation remains valid.
- `--rounds N` bounds candidate edit/evaluate cycles, default 3, nonnegative integer.
  Zero evaluates the current artifact without editing it. This is a cap, not a quota.
- `--with-baseline` adds a no-skill arm. Also use it for capability retirement and
  model migrations; an ordinary targeted fix needs current versus candidate.
- `--reuse-criteria` reuses suitable outcome cases after checking their identity and
  requirements. Old recitation tests do not become outcome tests by being renamed.
- `--fix-only` reuses an existing v3 baseline only when artifact, task set, grader,
  model, effort, and environment still match. Otherwise establish a fresh baseline.
- `--workers N` caps independent execution concurrency, default 2, positive integer.
  Respect available slots, rate limits, isolation, and task dependencies.
- `--skip-trigger-test` omits routing checks and records that coverage gap.
- `--no-visualize` requests text and JSON only. A visual report is otherwise optional.
- `--run-id ID` lets a caller address the exact report. Accept only letters, digits,
  underscores, and hyphens, 1-80 characters. Default to a unique topic-prefixed ID.
- `--resume PATH` resumes an explicitly selected v3 state file. Do not combine it
  with `--run-id` or other flags that change the recorded experiment.
- `--target` is retired. Explain that a legacy composite threshold cannot define
  outcome success, then stop without modifying the artifact. Reject unknown flags.

## What to preserve and what to challenge

Separate capability advice from the user's preferences and operational facts.
A stronger model may no longer need detailed decomposition, repeated verification
nudges, or retry ladders. It still needs the user's actual requirements and facts
it cannot know, such as a tool's failure behavior or a project's conventions.

Do not optimize for a standard-looking skill. Paragraph counts, read counts,
numbered steps, persona counts, state writes, and parallel calls are not quality
measures. Choose decomposition and concurrency for the task. Keep scripts that
provide useful deterministic work; remove or consolidate unused ones after checking
callers. Include description exclusions when they prevent actual misrouting.

Keep permission enforcement and checks of real invariants. Unattended workflows
need durable progress and clear stopping conditions; an attended task need not
acquire a state machine just because hone has one. A model's statement that a check
passed is not the check result.

## Run record and compatibility

After resolving the artifact, create a fresh directory at
`~/skill-eval/<artifact-name>/<run-id>/`. Never overwrite an existing run directory.
For a `SKILL.md` artifact, the name is its parent directory name; for a standalone
command, hook, or script, it is the filename with its final extension removed.
Keep trial outputs, frozen cases, original snapshots, raw logs, state, and the
final report there, outside the artifact being improved.

State is a small JSON record with `schema_version: 3`, `run_id`, canonical
`artifact_path`, maintained-source `edit_path`, `hone_fingerprint`, `phase`, `round`, `max_rounds`, snapshot paths,
case/grader fingerprints, target configurations, observations, and
`applied_edits.edited_paths`. `phase` is `evaluate`, `improve`, `verify`, or `finished`.
Record pending work and evidence paths at phase boundaries and after actual writes.
`hone_fingerprint` hashes the instructions and references used by this run.

On resume, read this skill and the recorded state, snapshots, and evidence. Verify
version, instruction fingerprint, artifact identity, and current file hashes before
continuing at the first unfinished operation. A legacy, corrupt, or mismatched state
cannot resume into v3. Preserve it and its edits, explain the mismatch, and stop;
a fresh invocation may start a separate experiment after inspecting those edits.
Do not run legacy gate or handoff validators against v3 state.

The v2 scoring, enrichment, overfit, convergence, and reporting scripts remain for
historical consumers. They are not this workflow's evaluator. Do not invoke them
to decide improvements, label their scores as outcome quality, rewrite old reports,
or emit new outcomes into legacy `results.json` or `deterministic_scores.json`.
V3 uses `outcome-report.json`; callers must request and read the exact run ID.
`artifact_path` identifies the resolved requested artifact; `edit_path` identifies
the maintained source when those differ. Preserve both identities in the report.
Legacy grade caches stay stale until their consumers support this measurement.

## Workflow

1. **Establish the task and baseline.** Read
   [evaluation](references/v3-next/phase1-evaluation.md). Identify the actual defect or
   improvement hypothesis, freeze representative task requirements and checks,
   inspect references, and run a baseline with the current artifact. For migration,
   compare each available target with and without the skill. The evaluation produces
   verified observations and a concrete reason to edit, or an unchanged/inconclusive
   report when there is no supported change.
2. **Make a bounded candidate.** Read
   [improvement](references/v3-next/phase2-improvement.md). Preserve user requirements,
   change the smallest coherent set of instructions, and test the hypothesis.
   Instruction removal is a normal candidate. Use an independent reviewer when
   judgment or consequences warrant one. Protect the original and concurrent work.
3. **Verify and decide.** Read
   [reevaluation](references/v3-next/phase3-reevaluation.md). Compare the candidate against
   the fixed baseline, check scope, and apply only an adequately supported change.
   Continue only for an identified unresolved defect while budget remains.

Never publish, push, send messages, or alter account settings as part of an eval.
Run side-effecting tasks only in an isolated environment whose permissions enforce
the test scope. A prompt that says "sandbox" is not isolation. If the host only
permits simulation, test decisions in simulation and report execution as unmeasured.

## Report

Lead with what changed, why, and the observed result. Include unresolved defects,
untested models, measurement limits, and the paths to evidence and the artifact.
Use one of `improved`, `regressed`, `unchanged`, `inconclusive`, or `blocked`.
`improved` must name the supported benefit and its tested scope; it is not a claim
that every target model performs better. A stopped budget with unresolved work is
inconclusive or blocked, never a successful convergence claim.

Write `outcome-report.json` with `schema_version: 3`, `measurement: "outcomes"`,
`run_id`, canonical `artifact_path`, maintained-source `edit_path`, `finished: true`, `verdict`, `cases`, and the
experiment identity, comparisons, evidence paths, and limitations described in the
reevaluation reference. Write to a temporary sibling, then atomically replace the
report after verifying it parses. `finished` means the report is final, not that
its verdict is good. Do not assign a letter grade or a synthetic composite.
