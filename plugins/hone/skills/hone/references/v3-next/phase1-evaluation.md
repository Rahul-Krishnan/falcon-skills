# Evaluate the current artifact

## Define the question

Read the artifact, its relevant dependencies, existing tests, and prior failures.
Resolve installed copies to maintained source before any edit. Explain the assumed
scope. A broken referenced script, a reproducible wrong result, and an unnecessary
instruction are different hypotheses and need different evidence.

Classify important instructions:

| Kind | What the test must preserve |
|---|---|
| Capability guidance | Correct completion of a task the model may now handle unaided |
| User preference | The requested voice, format, workflow, or product choice |
| Operational knowledge | Correct tool use, paths, recovery, permissions, and data handling |

Carry the same user requirements and permissions into every comparison arm. A
no-skill baseline removes only the artifact under test, not the user's acceptance
criteria. Domain facts that are part of the task must be available equally; facts
whose retrieval is the skill's contribution remain part of what is being tested.

## Freeze cases before changing the artifact

Start with observed failures and representative successful tasks. Write the prompt,
fixtures, expected outcome, allowed side effects, checks, and evidence requirements
for each case. Cover when behavior should happen and when it should not. For
example: ask when input is indispensable, proceed when it is already supplied,
verify a consequential change, and stop after sufficient verification.

Use task-specific deterministic checks for concrete properties: expected stdout
and exit code, file contents, required JSON fields, unchanged unrelated files, or
an actual application state. Use an independent semantic judge for relevance,
correctness that needs expertise, and preference fidelity. Literal matches are
appropriate for contractual literals, not for whether prose sounds correct.
Do not derive required output vocabulary from the instruction file.

Identify critical requirements in advance. A confirmed violation blocks the
candidate regardless of how many other cases pass. Validate graders on a known
correct result and a deliberately wrong result where feasible; a grader that accepts
both cannot establish the improvement. This includes script tests: a zero exit
code alone is not evidence of the correct result.

Keep development cases separate from final acceptance cases. The editor may see
development failures; it must not see held-out prompts or expected answers before
the final candidate is frozen. A main agent cannot unsee cases it generated: use
an independent case author/evaluator, or label the set as development coverage.
Once a held-out result informs an edit, that case becomes development coverage.

Cheap passing regression tests remain valuable. Add harder cases for new capability
rather than replacing all passing tests. A handful of cases can reproduce a defect
but does not establish general equivalence or statistical power.

A suite can use the repository's v3 outcome format: `schema_version: 3`,
`measurement: "outcomes"`, `project`, `skill_name`, and `test_cases`. Each case has
`id`, `name`, `mode` (`simulation` or `execution`), `prompt`, and `checks`. Each check
has an `id`, `description`, `method` (`artifact`, `judgment`, or `trace`), and boolean
`required`. IDs are unique within their scope. A simulation's `runner_context`
starts with `SIMULATION MODE: do not issue real tool calls.` Supply the relevant
instructions and fixtures as text before launch. Simulations measure decisions;
they cannot pass checks that require actual tool execution or changed files.

## Choose comparable runs

For a targeted fix, execute current and candidate artifacts on the same cases.
For a model migration or proposed capability retirement, include no-skill, current,
and lean-candidate arms within each target model. Routine follow-up work may test
only its active target; do not claim results for untested models.

Record the resolved model identifier, effort setting, harness/version, tools and
permissions, relevant user instructions, fixtures, artifact hash, task-set hash,
and grader hash. Record unavailable identifiers as unknown, not guessed. Unknown
or different configurations limit comparability. Do not silently substitute a model
when the requested one cannot run. Model names belong in experiment configuration,
not permanent assumptions that one family is always stronger than another.

Set the round, trial, concurrency, and time budgets before launching. Use paired
trials from clean equivalent environments. Keep all copies isolated from each
other's outputs and previous history. A missing tool or broken fixture is a harness
failure, not a model failure. Do not keep rerunning only the losing arm until it wins.

## Execute and collect evidence

Use the available native harness for model tasks and the actual test runner or
subprocess invocation for hooks/scripts. Fixtures and graders are prepared by the
orchestrator outside candidate-controlled output directories. Permit writes only
to trial resources; exclude live credentials, client data, messaging, and publication.
Do not run destructive tasks if the execution environment cannot enforce isolation.

Collect the actual output artifacts independently of the executor's summary. Run
assertions against them and retain command exit statuses and stdout/stderr. When
process behavior matters, use the harness's native event log, matching tool calls
to their returned results. Preserve the raw log. Do not ask the executor to create
an `execution_timeline` and then treat that declaration as observed execution.
If native events are unavailable, mark trace checks unmeasured; file-based outcomes
may still be measurable through independent reads and assertions.

Record elapsed time and reported token usage when available. Unknown usage stays
unknown; output characters are not total token consumption. Compare resource use
only after required outcome checks pass. Reduced instruction length by itself is a
maintenance improvement, not evidence of faster or better model execution.

## Use existing tools selectively

When skill-creator is installed, its artifact grader and blind comparator can help.
Resolve its location through the host's skill catalog and read the relevant agent
instructions. Give the judge the frozen rubric and actual outputs, permit ties and
unknowns, and withhold version labels and editor commentary. Do not regenerate the
rubric after seeing the candidate. Different personas on one model are not different
models. Use another capable model for disputed or consequential semantic judgments
when available and authorized; otherwise state the independence limit.

Skill-creator's `run_eval.py` checks description triggering and stops early. It is
not a full task runner. Its aggregate report is optional presentation: verify the
compared configurations and actual usage fields, and never let a two-arm headline
stand in for the full three-arm matrix. These helpers are optional; native tools
and direct artifact assertions remain sufficient when they are unavailable.

Inspect broken references, frontmatter, and executable dependencies with existing
validators. The legacy structural audit may provide hints, but its style pillars
and numeric grade do not decide whether this artifact works.

## Handoff to improvement

Persist the frozen cases, experiment identity, original snapshot, per-check
observations (`pass`, `fail`, `unmeasured`), and evidence paths. Name the demonstrated
failure or concrete ablation hypothesis. Without either, report unchanged or
inconclusive instead of manufacturing a structural defect to justify edits.
