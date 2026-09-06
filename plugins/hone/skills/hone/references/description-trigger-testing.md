# Description triggering

Run this when the description changed, the supported task scope changed, or a
model/host migration could affect discovery. Otherwise existing routing coverage
can remain a regression test. Honor `--skip-trigger-test` and report the omission.

Freeze realistic should-trigger and near-miss prompts before tuning the description.
Include competing skill descriptions from the actual host, user phrasing, and both
over-triggering and under-triggering cases. Do not generate only paraphrases of the
current description. Reserve final queries the editor has not seen.

Prefer observing actual skill selection in the target host's event stream. The
installed skill-creator trigger runner can help after checking how it loads skills,
what permissions it uses, and where it writes temporary files. Its early exit tests
selection only, not completion of the task. Follow the host's permission rules.

If actual routing is unavailable, use an independent text classification exercise
with the skill catalog. Label it a routing simulation. It cannot prove that the
host registered or invoked the skill. Never claim that specifying a different
sampling temperature exercised a distinct model or effort setting.

Record expected selection, observed selection, trial count, and target configuration.
Report false positives and false negatives separately. Preserve correct near-miss
behavior when fixing missed triggers. Add exclusion wording only where it clarifies
a real boundary; no particular phrase is mandatory.

Once queries influence edits, they are development queries. Judge the final frozen
description on fresh acceptance queries, or report that coverage remains limited.
Store cases and observations in the run directory, without creating duplicate live
suites in multiple installation paths. Do not manufacture a confidence claim from
an arbitrary overall accuracy threshold.
