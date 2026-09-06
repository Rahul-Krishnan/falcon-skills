# Improve a candidate

Read frozen requirements, baseline observations, and source. Use the evidence to
classify failures as artifact defects, fixture/grader defects, harness failures, or
uncertain observations. Classification requires judgment, not a numeric threshold.

Fix an invalid experiment before comparing candidates. Version changed cases,
fixtures, or graders and run both arms again under the corrected setup. Re-scoring
old outputs is sufficient only when the measurement changes without changing the
task or execution environment. Never silently relax a check to make the candidate win.

## Plan a small coherent change

For each edit, state the failure or maintenance problem, the requirement preserved,
and the check. Keep changes focused. Do not change the product, user preferences,
project settings, or companion skills to improve a grade.

Challenge instructions that duplicate model capabilities or tool checks, or split
tasks unnecessarily. Remove a coherent group and compare with the frozen baseline,
preserving user preferences and permissions across arms. A small passing suite
cannot prove that an instruction is universally unnecessary.

Keep useful, reliable deterministic helpers and check callers before consolidation
or retirement. Add validators only for actual contracts or demonstrated failures.

Give an independent reviewer the requirements, source, and raw failure evidence,
withholding the editor's proposal. Review ambiguous or consequential changes; small
corrections do not need a panel. Resolve disagreements through sources or tests
and record those that remain unresolved.

## Protect the edit boundary

Keep originals and trial candidates outside one another. Before any maintained-source
write, snapshot the exact edit target using the existing guard:

```sh
python3 "$HONE_DIR/scripts/check_scope.py" --artifact "$EDIT_PATH" --type "$ARTIFACT_TYPE" --manifest "$RUN_DIR/scope.json" --snapshot --json
```

Resolve `HONE_DIR` from the host-injected or discovered hone directory. Set
`EDIT_PATH` to maintained source. The guard derives its watch root and artifact
scope separately; report `root_fallback` as narrowed coverage.

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

Save the candidate identity, edits, active scope manifest, file hashes, and checks
to rerun. Read back changes and set state to `verify`. Record success only after
inspecting the candidate's results.
