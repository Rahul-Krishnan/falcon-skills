# Improve a candidate

Read the frozen task requirements, baseline observations, and the relevant source.
Classify each failure by inspecting evidence: artifact defect, fixture/grader defect,
harness failure, or uncertain observation. This classification requires judgment;
a numeric threshold cannot decide which component is wrong.

Fix an invalid experiment before comparing candidates. Version changed cases,
fixtures, or graders and run both arms again under the corrected setup. Re-scoring
old outputs is sufficient only when the measurement changes without changing the
task or execution environment. Never silently relax a check to make the candidate win.

## Plan a small coherent change

For each proposed edit, state the task failure or maintenance problem it addresses,
the requirement it preserves, and how it will be checked. Prefer a focused change
to an unrelated cleanup. Do not change the product, user preferences, project
settings, or companion skills just to improve a grade.

Challenge instructions that duplicate model capabilities, repeat a check already
enforced by tooling, or impose unnecessary decomposition. Remove a coherent group
and compare against the frozen baseline. Preserve explicit user preferences and
permission boundaries across arms. Absence of a failure in a small suite is limited
evidence, not proof that an instruction is universally unnecessary.

Keep deterministic helpers when they provide reliable useful work. Check callers
before consolidating or retiring one. Add a validator only for an actual contract
or demonstrated failure; do not compile every emphatic word into a new program.

An independent reviewer receives task requirements, source, and raw failure evidence,
without the editor's proposed answer. Review ambiguous or consequential changes;
do not require a panel for every small correction. Resolve disagreement against
sources or tests. Record unresolved judgment explicitly instead of averaging it away.

## Protect the edit boundary

Keep originals and trial candidates outside one another. Before any maintained-source
write, snapshot the exact edit target using the existing guard:

```sh
python3 "$HONE_DIR/scripts/check_scope.py" --artifact "$EDIT_PATH" --type "$ARTIFACT_TYPE" --manifest "$RUN_DIR/scope.json" --snapshot --json
```

Resolve `HONE_DIR` from the host-injected skill directory, or the discovered actual
hone directory. `EDIT_PATH` is maintained source, not the installed copy used for
reading. The guard derives its watch root and artifact scope separately. Report
narrowed coverage (`root_fallback`) instead of describing it as complete protection.

Preserve a byte-for-byte original snapshot of every file to be changed, including
pre-existing uncommitted content. Record which paths did not exist. Before applying
an edit, check that the live contents still match the expected pre-edit contents.
If another actor changed the file, stop and reconcile; do not overwrite their work.

Record every source or companion-file write in `applied_edits.edited_paths` in v3
state. Trial artifacts and run records live separately and are not source edits.
After edits, run:

```sh
python3 "$HONE_DIR/scripts/check_scope.py" --manifest "$RUN_DIR/scope.json" --verify --declared-file "$STATE_FILE" --json
```

Interpret exit codes exactly:

- `0`: clean within the reported coverage.
- `1`: a declared scope violation. Halt and undo only attributable edits using the
  original snapshots, provided the live contents still match this run's last write.
- `2`: invalid inputs or a check that could not run. Halt; revert nothing blindly.
- `3`: not measurable, including concurrent/unattributed changes. Halt and preserve
  other work. A check unable to answer is not a pass.

Never revert `preexisting_dirty_out_of_scope` or `unattributed_out_of_scope` paths.
A source file already dirty at the start must be restored to the saved contents,
not to its committed version. Remove only this run's newly created files, using the
host's reversible deletion mechanism. Run the guard again after any later writes,
including restored ablations. Candidate-only work still needs isolated permissions;
the post-hoc scope guard does not make an unrestricted executor safe.

## Handoff to verification

Save the candidate identity, exact edits, active scope manifest, current file hashes,
and the checks to rerun. Re-read changed files to confirm the edit landed. Update
state to `verify`; do not record a successful outcome until the candidate's actual
results have been inspected.
