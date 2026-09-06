# Verify and decide

Run frozen cases under the baseline's model, effort, harness, fixtures, permissions,
and context. Verify identities before comparison; changed tasks or environments
need a new baseline. Do not calculate outcome deltas across different measurement
methods, unknown model substitutions, missing outputs, or simulation/execution modes.

## Judge the result

Apply deterministic assertions to outputs and independent semantic judgment where
needed. Give judges frozen requirements and evidence, withholding version labels
and editor explanations. Allow ties and unknowns; inspect disagreements rather
than averaging them away.

Inspect critical failures immediately. If a candidate demonstrably violates a
required invariant, reject it even when the suite is small. Confirm that the failure
comes from the candidate rather than a broken fixture. Restore only this run's
attributable maintained-source edits, after checking for concurrent changes, and
verify the restoration. A suspected but unmeasured regression is not permission
to overwrite files. Prefer leaving an uncertain candidate separate from the original.

For stochastic quality changes, use the predetermined paired trial budget and
report individual wins, losses, ties, and unmeasured cases. A few passes can establish
that a known defect was repaired; they cannot establish general non-inferiority.
Do not add cases merely to clear a significance floor, selectively retry losses,
or treat an insignificant difference as proof of equivalence.

After selecting the final candidate using development cases, run acceptance cases
that the editor has not seen. If their results inform more editing, they are now
development cases. State that limitation and reserve fresh acceptance cases before
making a broader generalization claim. Run description checks when routing changed,
using [trigger testing](description-trigger-testing.md).

## Apply and stop

For a supported candidate, apply the minimal diff to the maintained source under
the scope protocol in [improvement](phase2-improvement.md). Re-read the files, run
relevant validators/tests, and verify scope after the last write. Do not install,
publish, push, or edit a second copy unless the user's task authorizes that action.

Continue only for a specific unresolved defect with a supported next change and
remaining budget. Stop when clean, no edit is justified, budget is exhausted, or
permissions, capabilities, or scope conflicts block progress. Retain state and
evidence. Do not chase a grade or spend rounds without a reason.

Use these verdicts with explicit scope:

| Verdict | Meaning |
|---|---|
| improved | A named defect was fixed or a justified simplification was verified on the reported cases, with required checks satisfied |
| regressed | The candidate caused a confirmed outcome failure; state whether it was rejected or safely restored |
| unchanged | Checks completed and no supported change was needed or selected |
| inconclusive | Available evidence cannot settle the proposed change, including an exhausted budget with unresolved quality questions |
| blocked | Required input, permission, capability, or safe edit conditions are unavailable |

List tested models and cases. Simulations measure decisions, not successful edits,
runtime safety, or end-to-end performance. Label untested targets. Distinguish
maintenance improvements, such as fewer redundant instructions, from measured
task quality, latency, and token usage.

## Durable outcome report

Write `outcome-report.json` in the exact run directory. It includes:

```json
{
  "schema_version": 3,
  "measurement": "outcomes",
  "run_id": "the-resolved-run-id",
  "artifact_path": "/canonical/path/to/the/artifact",
  "edit_path": "/canonical/path/to/maintained/source",
  "finished": true,
  "verdict": "inconclusive",
  "identity": {
    "artifact_before_sha256": "digest",
    "artifact_after_sha256": "digest",
    "source_before_sha256": "digest",
    "source_after_sha256": "digest",
    "criteria_sha256": "digest",
    "grader_sha256": "digest",
    "targets": []
  },
  "cases": [],
  "comparisons": [],
  "edited_paths": [],
  "limitations": [],
  "evidence_paths": []
}
```

Populate placeholders from the run; the example's empty arrays are not evaluation
evidence. Each case records `id`, `target` (a target ID), `arm` (`current`, `candidate`, or `no_skill`), positive integer `trial`,
`mode` (`simulation` or `execution`), and
`checks`. Each check carries `id`, boolean `required`, `result` (`pass`, `fail`,
`unmeasured`), `method` (`artifact`, `judgment`, or `trace`), and `evidence_paths`
to independently inspected files. Explain missing evidence. Distinguish raw harness
logs, assertion results, and model judgments.

The artifact hashes describe the requested entry file at `artifact_path`. The
source hashes describe the maintained-source snapshot rooted at `edit_path`,
including the entry file and relevant companion files through a frozen manifest
of canonical paths and content hashes. Retain both manifests as evidence. When
only source is edited, the requested installation's hashes can remain identical;
that does not establish that the installation was updated. Run candidate trials
from the identified source snapshot and label any unsynchronized installation.

Each target records `id`, resolved `model`, `effort`, `harness`, `harness_version`,
`tools` (a list), `permissions` (a description), `environment_sha256`, and
`context_sha256`. For direct deterministic hook/script checks, model and effort
may be `not_applicable`; identify the actual runtime and version as the harness.
Unknown configuration values stay explicitly unknown, with a
limitation; early blocked or inconclusive reports may have null hashes and empty
cases/targets with the reason recorded. An improved report needs measured candidate
cases, passing candidate required checks, complete comparable identities, and
existing evidence files. Schema validation establishes structure, not the truth of those findings.
Expected failures in the current or no-skill arms remain in the report and do not
by themselves disqualify an improved candidate.
Each comparison identifies its two arms,
paired cases, observed benefit or failure, and uncertainty. Include actual timing
and usage when available, otherwise null. Keep the report outside candidate writes.

Before finalizing, match the run ID and artifact identity to state, confirm evidence
files exist, and check the verdict against outcomes and scope. Write atomically and
read back. Callers must read this exact path and check `schema_version`,
`measurement`, `run_id`, `artifact_path`, and `finished`.
They must not find an arbitrary newest `results.json`, derive a legacy grade, or
compare old simulated-plan scores to this report.

Legacy caches and workout versions that cannot consume this contract need an
explicit integration update. Leave old evidence intact and report that legacy
grades are stale. Do not rebuild a fresh-looking grade from old scores and new
instruction hashes. An incompatible old state is preserved, never migrated by
inventing missing observations.
