# Description triggering

Test routing after description or task-scope changes, or model/host migrations
that could affect discovery. Otherwise retain existing regression coverage. Honor
`--skip-trigger-test` and report the omission.

Freeze realistic should-trigger and near-miss prompts before tuning the description.
Include competing skill descriptions from the actual host, user phrasing, and both
over-triggering and under-triggering cases. Do not generate only paraphrases of the
current description. Reserve final queries the editor has not seen.

Observe skill selection in the target host's event stream where possible. Before
using skill-creator's trigger runner, check its skill loading, permissions, and
temporary-file paths. It exits after selection and does not test task completion.
Follow host permission rules.

If routing cannot be observed, independently classify prompts against the skill
catalog and label the result a routing simulation. This cannot prove registration
or invocation. A different sampling temperature is not a distinct model or effort
setting.

Record expected selection, observed selection, trial count, and target configuration.
Report false positives and false negatives separately. Preserve correct near-miss
behavior when fixing missed triggers. Add exclusion wording only where it clarifies
a real boundary; no particular phrase is mandatory.

Queries used to guide edits become development queries. Test the frozen final
description on fresh acceptance queries or report limited coverage. Store cases
and observations in the run directory, without duplicate live suites. Do not infer
confidence from an arbitrary overall accuracy threshold.
