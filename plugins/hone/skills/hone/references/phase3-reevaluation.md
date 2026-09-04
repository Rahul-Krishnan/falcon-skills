## Phase 3: Re-Evaluate (MANDATORY after improvement)

**Re-evaluation is not optional.** Every improvement round MUST end with a re-evaluation using the same eval criteria as Phase 1. This produces the after_score that gets compared to the before_score. Without re-evaluation, improvements are ungraded assertions, not measured results. The `--rounds N` flag controls how many improve-then-reevaluate cycles to run, but there is always at least one re-evaluation after improvement. Setting `--rounds 0` would mean evaluate-only (dry run, no improvement).

If `{current_round} >= {max_rounds}`, go to Final Output (but only AFTER the re-evaluation for this round is complete).

**CRITICAL: Anti-bias protocol for re-evaluation.** The re-evaluation after improvement is the highest-risk point for self-confirmation bias. The agent that made the improvements is now asking itself whether the improvements worked. Three mandatory countermeasures:

1. **Blind evaluation.** The re-evaluation judge prompt MUST NOT mention: that improvements were made, what was changed, what round this is, or any before/after framing. The judge receives ONLY (response_text, rubrics). It scores a response, not an improvement. This prevents anchoring bias.

2. **Multi-perspective judge panel.** For the 1-2 test cases with scores in the 0.4-0.7 ambiguous zone (or the lowest-scoring test if all pass), do not accept a single judge's number. Spawn 3 subagents in parallel, one each with the Pragmatist, Skeptic, and Systems Thinker roles, each scoring the blind judge prompt independently, then average their composites. Perspective diversity is the point here: a lone judge scoring output from its own session is the weakest link in the loop. There is no silent fallback to a single judge.

   Give each role the same blind prompt:
   ```
   Score this response on the rubric below. Do not mention improvements or what round this is.
   Response: [agent_response for {test_id}]
   Rubric checks: [checks array from eval criteria for {test_id}]
   Return: score (1-5 per check), one-sentence rationale per check, composite (0.0-1.0).
   ```

   Average the panel composite with the standard judge score and log `"cross_model_judge": "inline_3role"` in the workflow state.

   If you have a cross-model consensus command installed (one that fans the same question out to several model CLIs), use it here instead and log `"cross_model_judge": "cross_model"`. Genuinely different models disagree in more useful ways than three roles on one model, but the 3-role panel is the portable default and is what runs unless you wire something else in.

3. **Same criteria, same judge framing.** Use identical eval criteria and identical judge system prompt as Phase 1. The only variable is the executor output. This makes before/after scores directly comparable.

**Pre-re-evaluation: Refresh enrichment (skills and commands only).** Phase 2 may have modified the artifact, making `required_present` entries from Phase 1 Step 6 stale (referencing identifiers no longer in the artifact). Before re-running eval runner, re-run enrichment to refresh:

```bash
python3 <skill-dir>/scripts/enrich_programmatic_checks.py \
  --artifact-path {artifact_path} \
  --criteria-path {eval_criteria_path} \
  --json
```

This is idempotent: it strips stale identifier-shaped `required_present` entries (no longer occurring in the artifact — hand-written phrases are untouched) and adds any new identifiers introduced by Phase 2 edits, so a Phase 2 rename cannot leave a permanently-MISSING check behind.

**Then re-run the overfit gate (Phase 1 Step 6a), because the refresh re-derives anchors from the current artifact.** An identifier Step 6a had you remove as a `vocabulary` lift is still in the artifact and still in the check text, so the refresh adds it back, and without a gate here Phase 3 scores the recitation anchors Step 6a rejected:

```bash
python3 <skill-dir>/scripts/check_overfit.py {eval_criteria_path} --artifact {artifact_path} --json
```

Apply Step 6a's rule unchanged: rewrite flagged items, `vocabulary` anchors first, until the verdict is `within_threshold`, and do not touch a set that already reports it. Do this before re-running eval runner, so the after-round is scored against the same kind of criteria the before-round was.

**A criteria edit invalidates the prior round's scores, exactly as a scorer edit does.** The refresh and the overfit rewrites both change the criteria file, and `score_execution.py`'s `quality_checks` dimension is `passed / total` over `required_present`: remove three anchors the before-round output was missing and every composite rises with byte-identical executor output, which step 3a then reads as a clean sweep and reports `improved`. So, once the refresh and the overfit gate are done, compare the criteria file against the copy that scored the prior round (the `.pre-enrich` backup the refresh wrote, or a hash of the file recorded with that round's scores). If anything changed, re-score the prior round's `results.json` against the *current* criteria before step 3a runs, the same way the scorer-change rule in step 5 re-scores it against a changed scorer.

**Re-score against the artifact the prior round ran against, not the current one.** `--artifact-path` is not a formality here: `score_workflow_sequence` and `score_output_structure` derive the expected steps and headings from that file, so passing the post-Phase-2 artifact re-measures the baseline against a step list its executor never saw. A `## Step N` heading Phase 2 added is then counted as a step the baseline skipped, its composite drops, and step 3a reports `improved` on byte-identical executor behaviour, which is the measurement change this re-score exists to remove. Phase 2 Step 6 wrote the pre-edit content to `applied_edits.artifact_before_snapshot` in the state file; on round 1 it is the same content as `artifact_context.original_backup_path`. Read the path from the state file and pass it:

```bash
python3 <skill-dir>/scripts/score_execution.py $PRIOR_OUTPUT_DIR/results.json --type {artifact_type} --artifact-path {artifact_before_snapshot} --criteria-path {eval_criteria_path} --require-timeline --json
```

The only input that differs between this re-score and the prior round's original scoring is the criteria file. Everything else (trace, artifact, scorer) is held fixed, so the delta is the criteria change alone.

That rewrites `$PRIOR_OUTPUT_DIR/deterministic_scores.json`, which is the file step 3a's `--before` reads. Step 5's per-dimension regression check reads something else, `eval_results.per_test` or `round_{N-1}_scores.per_test` in the state file, and those still hold the scores the retired criteria produced. Leaving them there gives the two checks two different baselines: 3a compares against the adjusted one while step 5 compares against the stale one, so a criteria change that moved `quality_checks` either masks a real drop or fires a phantom regression that survives resampling (the criteria delta is deterministic) and auto-reverts working edits. So, before overwriting the deterministic file, record two fields inside the prior round's own record in the state file (`eval_results` on round 1, `round_{N-1}_scores` after that; both schemas in `scripts/validate_handoff.py` carry them as optional fields):

```json
"baseline_original": {"composite_score": 0.71, "per_test": [...]},
"baseline_adjusted": {"composite_score": 0.74, "per_test": [...]}
```

`baseline_original` copies the record's own `composite_score` and `per_test`; `baseline_adjusted` carries the same two fields from the re-score, with `per_test` in the shape step 6 writes (each entry's `dimension_scores` included). Step 5 reads `baseline_adjusted.per_test` when it is present and the record's own `per_test` otherwise; both rounds are then compared against one criteria set on every check, and the sign test and the regression check measure the artifact change alone. `check_eval_power.py` cannot detect a criteria change for you: `deterministic_scores.json` records the artifact type that scored it, not the criteria that did.

Steps:
1. Re-read artifact and eval criteria from disk (compaction protection).
2. Re-run eval runner with `--reuse-criteria`.
3. Run deterministic scoring on re-eval results:
   ```bash
   python3 <skill-dir>/scripts/score_execution.py $REEVAL_OUTPUT_DIR/results.json --type {artifact_type} --artifact-path {artifact_path} --criteria-path {eval_criteria_path} --require-timeline --json
   ```
3a. **Power verdict (Phase 1 Step 9a).** The composite from step 3 is a number, not a result, until the sign test says whether the round's movement is distinguishable from noise. Run the comparison Step 9a specifies, with `$PRIOR_OUTPUT_DIR` read from the workflow state file, never from memory: on round 1 it is `eval_results.output_dir` (Phase 1's baseline); on round N >= 2 it is `round_{N-1}_scores.output_dir`, which step 6 records for exactly this purpose. Comparing round 2 against Phase 1's baseline would credit round 1's gain to round 2. Point `--after` at this round:
   ```bash
   python3 <skill-dir>/scripts/check_eval_power.py {eval_criteria_path} --artifact-type {artifact_type} --before $PRIOR_OUTPUT_DIR/deterministic_scores.json --after $REEVAL_OUTPUT_DIR/deterministic_scores.json --json
   ```
   Record `power_verdict` (the top-level `verdict`), `power_p_improved` (`comparison.p_improved`), and `power_discordant` (`comparison.discordant`) beside this round's composite in the state file, and read the verdict as Step 9a does: `underpowered` and `not_measurable` are neither a pass nor a regression, and neither is ever reported as an improvement. The per-dimension comparison and regression check below still run; the power verdict is recorded alongside them, and the Final Output reports it with the grade.

   **`underpowered` and `not_measurable` gate the auto-revert in step 5.** Phase 1 Step 6b is advisory below its floor, so an under-floor round no longer halts at Phase 1 — it arrives here. Step 9a's rule ("never let it justify a promotion or a revert") has to hold in both directions or making 6b non-blocking would just route more runs into a revert the same rule calls unjustified. Concretely: on either verdict, step 5 does not auto-revert and this round claims no improvement. Record the composite with the verdict beside it as the qualifier, report a suspected regression as suspected, and leave the call to the human.

   **On a `--fix-only` run there is no baseline on round 1.** That run shape skips Phase 1 entirely (`eval_results` is never written, and Step 6b never ran either), so `$PRIOR_OUTPUT_DIR` is empty and the comparison has nothing to pair; pointing `--before` at `/deterministic_scores.json` is a usage error (exit 2), not a verdict. On the first Phase 3 round of a fix-only run, run the sizing half alone and record its verdict, which is the same thing Phase 1 records on a first round with nothing to compare against:
   ```bash
   python3 <skill-dir>/scripts/check_eval_power.py {eval_criteria_path} --artifact-type {artifact_type} --json
   ```
   Record `power_verdict` as `powered` or `underpowered` with no `power_p_improved` or `power_discordant`; this round's scores are the baseline, not a delta, and steps 4 and 5 below have no previous round to compare against, so skip them and write step 6's record. The comparison starts on round 2, against `round_1_scores.output_dir`.
4. Compare scores: before/after table per-dimension using deterministic scores from workflow state file (`baseline_adjusted.per_test` when the pre-re-evaluation section recorded it, the prior round's own `per_test` otherwise).
5. **Regression check (rubric):** Re-read previous scores from the workflow state file (`eval_results.per_test` or last round's recorded scores). Do NOT use in-memory scores from earlier in the conversation. If the pre-re-evaluation section recorded `baseline_adjusted` for the prior round, its `per_test` is the previous score: it is the same criteria set this round was scored against, and the file step 3a compared against. For each dimension, compare new score to previous score read from the state file. If ANY dimension dropped by more than 0.1, flag as regression, then run the variance control below before reverting anything.

   **Power precondition (checked before any revert, including after resampling).** If step 3a recorded `power_verdict` as `underpowered` or `not_measurable`, do NOT auto-revert, whatever this check and the resampling below conclude. Record the flagged dimensions, the medians, and the power verdict; report "Round N shows a suspected regression in {dimensions}, unverifiable at this suite size ({power_verdict}); edits left in place for review" and halt the improvement loop for the human rather than restoring the snapshot. The regression check and the sign test measure different quantities, so they are not statistically contradictory, but the evidence standard is the same one: a suite too small to promote on is too small to revert on, and reverting on it discards working edits on evidence Step 9a says cannot carry a verdict. The remedy is the one Step 6b names — add cases that discriminate a different property — not a revert. Everything below still runs and is still recorded; only the restore is withheld.

   **Before believing any before/after delta, bound it.** Run-to-run variance in agentic evals is large enough to swamp a small-N comparison, and a chunk of it is infrastructure rather than the artifact. Bootstrap a confidence interval over the per-test scores on each side and compare intervals, not point estimates. A delta whose interval straddles zero is not a result in either direction, and neither improving nor reverting on it is justified.

   **Comparability check (before treating any drop as a regression).** Re-scoring fixes a changed *formula*; it cannot fix a changed *input*. If the previous round's records lack a field the current round's records carry (most often `execution_timeline`), every dimension reading that field was unmeasured before and measured now, and will appear to collapse because the absent-evidence path returns a vacuous pass. Classify each regressed dimension as **comparable** (reads only fields present in both rounds) or **not comparable**. Auto-revert only on a comparable drop. Report a not-comparable dimension as a first measurement, never as a delta.

   **Variance control (required before auto-revert).** One re-eval run is a single sample per test, and executor behavior varies across runs on anything that depends on tool availability (whether an executor reached for `AskUserQuestion` before falling back to text, whether it emitted a structured gate event). A single noisy sample must not discard working improvements.

   - Identify which tests feed the regressed dimension. Re-run only those tests, twice more, using identical criteria and the same blind framing.
   - Recompute the dimension from the **median** of the three samples per test.
   - If the median still shows a drop > 0.1: the regression is real. Auto-revert and halt — unless the power precondition above withheld the revert, in which case halt and report without restoring.
   - If the median clears the threshold: record `"variance_confirmed": true` with the per-sample scores in the state file and continue. Do not revert.

   Record the attribution alongside the decision: which edits this round touched the sections or scripts that feed the regressed dimension. A regressed dimension with no edit touching its inputs is evidence for variance, but it does not replace the resampling. Resampling is the operative test, because attribution reasoning is exactly the kind of judgment call the mechanical gate exists to constrain.

   When the regression survives resampling and the power precondition allows a revert, auto-revert edits from the last round (restore from the pre-edit re-read) and report "Round N caused regression in {dimensions}; changes reverted." **After auto-revert, halt the improvement loop immediately — do not loop back to Phase 2.** A regression signals that the improvement direction was wrong; blindly retrying without understanding the failure would waste rounds. Present final output using pre-revert scores.

   **When the scorer itself changed this round** (any edit to `score_execution.py`), re-score the previous round's `results.json` with the updated scorer before comparing. Otherwise the before/after delta mixes an artifact change with a measurement change, and improvements to the scorer read as improvements to the artifact. Record both the original and adjusted baseline in the state file, in the `baseline_original` / `baseline_adjusted` shape the pre-re-evaluation section defines, and re-score against the artifact the prior round ran against (`artifact_before_snapshot`), not the current one. A criteria change is the same kind of measurement change and gets the same treatment; the pre-re-evaluation section above says when and how.
6. Write this round's scores to the workflow state file under `round_{N}_scores`, together with where they came from, so the next round can find its baseline after a compaction:
   ```json
   {"output_dir": "$REEVAL_OUTPUT_DIR", "composite_score": 0.82, "per_test": [...], "power_verdict": "improved", "power_p_improved": 0.0312, "power_discordant": 6}
   ```
   `output_dir` is the field step 3a reads on the following round; `per_test` is what step 5 reads (each entry carrying `test_id`, `score`, `status`, and `dimension_scores`, the shape `eval_results.per_test` uses). The record is validated against the `round_scores` schema in `scripts/validate_handoff.py`: `python3 <skill-dir>/scripts/validate_handoff.py $STATE_FILE --handoff round_{N}_scores`, and `--all` picks every `round_{N}_scores` key up on its own. Also append a gate event to `gates[]`:
   ```json
   {"step": "phase3_exit", "judge": "self-check", "result": "pass", "ts": "<ISO timestamp>"}
   ```
   Set `result` to `"fail"` if a regression was detected and edits were reverted. Append to `state["gates"]` — do not replace.
7. **Mechanical exit gate** (see Final Output below). The state file decides whether to continue or exit, not the LLM.
8. If gate says CONTINUE: increment round, loop back to Phase 2.

## Final Output


**MECHANICAL EXIT GATE (replaces introspective anti-laziness checklist):**

The prior approach was an LLM-evaluated checklist: the improving agent would ask itself "have I tried hard enough?" This is gameable — the same agent evaluating whether to continue has incentive to rationalize early exit. The mechanical gate replaces that checklist with a state-file-driven decision: the LLM reads objective data (scores, iterations, delta) and applies deterministic rules. The LLM can read the gate conditions but cannot override them.

The state file decides when to exit. The LLM cannot override these checks. Re-read `/tmp/workflow-${RUN_ID}.json` and evaluate each condition:

**PRECEDENCE: BLOCKED conditions are checked FIRST. If ANY BLOCKED condition is true, do NOT exit, regardless of ALLOWED conditions.** This prevents the failure mode where "all individual test scores >= 0.8" triggers exit while momentum exists and rounds remain. (The 0.8 per-test bar mirrors `ACTIONABLE_THRESHOLD` in `scripts/hone_common.py`, which is authoritative.)

**Exit BLOCKED (keep going) when ANY are true** (checked FIRST):
- [ ] Any step is `"pending"` or `"in_progress"` in the state file
- [ ] `open_questions` is non-empty
- [ ] `iteration.current < iteration.target` AND score improved this round by >= 0.02 AND composite < 0.9 AND (`{target_score}` is unset OR composite < `{target_score}`) (momentum, not plateau — but grade A artifacts or target-met artifacts are allowed to exit early)
- [ ] Any test has score < 0.5 AND `iteration.current < iteration.target` (significant failure with rounds remaining; the 0.5 bar mirrors `CRITERIA_BUG_THRESHOLD` in `scripts/hone_common.py`, which is authoritative)

**Exit ALLOWED when ALL are true** (checked ONLY if no BLOCKED conditions matched):
- [ ] All steps in `steps` object are `"done"` (no `"pending"` or `"in_progress"`)
- [ ] `open_questions` array is empty (all tracked questions resolved)
- [ ] Phase 3 re-evaluation completed for this round (`phase3_reevaluate` is `"done"`)
- [ ] One of:
  - `iteration.current >= iteration.target` (round budget exhausted), OR
  - Composite score >= 0.9 (grade A, nothing left to improve). Note: this is the COMPOSITE score, not individual test scores. A composite of 0.87 with all tests above 0.8 is still grade B and should keep improving if rounds remain., OR
  - `{target_score}` is set AND composite score >= `{target_score}` (user-specified convergence target met early), OR
  - Score delta between last two rounds < 0.02 AND zero actionable failures remain (genuine plateau)

**Forced exit with human gate (--confirm mode only):**
- If rounds exhausted but tests with score < 0.5 remain (0.5 mirrors `CRITERIA_BUG_THRESHOLD` in `scripts/hone_common.py`, which is authoritative): present the failures to the user and ask whether to add more rounds or accept the current state. In `--auto` mode: log `"exit_with_low_scores": true` and the test IDs in the state file, but do exit (the round budget is a hard cap in --auto to prevent infinite overnight loops).

**Anti-gaming note:** `open_questions` is auto-populated from structural data (eval scores in 0.4-0.7, failed structural pillars, fresh-eyes disagreements) BEFORE the main thread touches the array. Auto-generated questions are tagged `"source": "auto"` and cannot be removed by the LLM. The main thread can add `"source": "manual"` questions but cannot delete auto-generated ones. The remaining trust surface is limited to: the LLM choosing not to add manual questions it should have. This is a narrower gap than the original (LLM populating the entire array), and is partially covered by fresh-eyes reconciliation surfacing findings the main thread missed.

```
═══════════════════════════════════════════════════
Hone Complete: {type}/{name}

  Rounds: {rounds_completed}
  Grade:  {initial_grade} → {final_grade} ({initial_score} → {final_score})
  Structure: {structural_score} ({ungated_count} ungated, {untyped_count} untyped handoffs)

  Dimension Progress:
    {dim1}:  {before} → {after}
    {dim2}:  {before} → {after}
    ...

  Changes Applied: {count} edits ({structural_fixes} structural, {content_fixes} content)
═══════════════════════════════════════════════════
```

## Common Executor Mistakes

> **TOOL CALL REQUIRED, NOT TEXT OUTPUT:** When the STOP section says "Call `AskUserQuestion`", you MUST invoke the `AskUserQuestion` tool. The judge checks the execution trace for tool calls. Printing the question as assistant text is a gate failure even if the question text is correct.

When executing this skill, avoid these patterns:

1. **Printing text instead of using AskUserQuestion.** When the STOP section or argument validation says "Call AskUserQuestion", you must call the `AskUserQuestion` tool. Printing the question as assistant text does NOT satisfy the gate. The tool provides an interactive picker UI that text output cannot replicate.

2. **Proceeding past a STOP gate.** When a gate says "STOP immediately", that means no further workflow steps should execute. Do not write the workflow state file, do not run structural audit, do not launch eval runner. Stop and address the gate failure.

3. **Leaking workflow terms into error output.** When stopping on a validation error (invalid type, missing args, conflicting flags), your response must ONLY contain the error message and guidance. Do NOT describe what the skill would have done (phases, eval runner, structural audit, etc.). The `required_absent` checks in eval criteria will fail your response if workflow terms appear in error output.

4. **Using workflow-internal terms in fallback output.** When AskUserQuestion is unavailable and the fallback fires, your output must contain ONLY the question and options — no references to Phase 1, structural audit, eval criteria, or any other hone-internal step. Leaking internal terms into a user-facing stop message is a hard failure even if the question itself is correct.

5. **Sequential reads for independent files.** When a phase starts by reading multiple unrelated files (artifact, reference file, state file), issuing them one at a time is a latency violation. Batch all independent Read calls into a single parallel tool-use turn.

## Context Compaction Protection

This workflow runs 30+ minutes per eval round. After generating/editing eval criteria, re-read from disk. After each eval runner run, record output path and scores. After applying edits, re-read to confirm. Before re-evaluation, re-read both files.
