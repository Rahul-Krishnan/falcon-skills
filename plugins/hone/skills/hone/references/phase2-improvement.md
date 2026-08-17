## Phase 2: Improve

**Pre-phase validation:** Before starting Phase 2, run the comprehensive handoff validation:

```bash
python3 <skill-dir>/scripts/validate_handoff.py \
  /tmp/workflow-${RUN_ID}.json --all --json
```

This validates every handoff written during Phase 1 (`artifact_context`, `eval_results`, and optionally `structural_audit`, `reference_validation`). If any fail: fix the state file, re-validate, do not proceed. `structural_audit` and `reference_validation` may be absent (if steps were skipped per scope_intent tier), which is fine since `--all` only checks handoffs that are present.

### Step 1: Read and analyze results


Run deterministic failure triage:

```bash
python3 <skill-dir>/scripts/analyze_results.py $OUTPUT_DIR/results.json --triage
```

Parse JSON output. The script classifies each test as `criteria_bug`, `variance`, `real_issue`, or `pass`. When `deterministic_scores.json` exists alongside results.json (from Step 8), the script uses deterministic scores for classification.

**Persist triage output to disk** (compaction protection):
```bash
python3 <skill-dir>/scripts/analyze_results.py $OUTPUT_DIR/results.json --triage > $OUTPUT_DIR/triage.json
python3 <skill-dir>/scripts/analyze_results.py $OUTPUT_DIR/results.json > $OUTPUT_DIR/analysis.txt
```

Also run the human-readable analysis for context:

```bash
python3 <skill-dir>/scripts/analyze_results.py $OUTPUT_DIR/results.json
```

Route based on triage classifications:
- `criteria_bug` count > 0 → proceed to Step 2 (Criteria Self-Repair)
- `variance` → exclude from analysis
- `real_issue` → actionable improvement opportunity (Step 4)
- All `pass` → skip Phase 2

### Step 2: Criteria Self-Repair (V1: pattern table only)


**Skip this step if no `criteria_bug` classifications in triage.** Proceed to Step 4.

This step applies deterministic fixes to eval criteria when tests fail due to test design issues (not skill quality). It uses a pattern table, not an LLM, to avoid self-gaming. The pattern table is append-only during runs; new patterns require human review between runs.

**Anti-gaming boundary:** This step modifies eval criteria, not the artifact. It can only tighten tests (remove tools, add required_absent, clarify runner_context). It cannot weaken tests (lower rubrics, remove `checks` entries, add tools). The `checks` array and rubric scores are untouchable.

**Flow:**

1. **Run pattern matcher:**
   ```bash
   python3 <skill-dir>/scripts/criteria_self_repair.py {results_path} --json
   ```
   Parse JSON output. The script matches each failing test against known criteria bug patterns (recursive timeout, empty response, tool access errors) and outputs proposed fixes.

2. **For each matched fix, apply via Edit tool:**
   - `allowed_tools` with `action: "remove"`: remove the specified tools from the test's `allowed_tools` list
   - `required_absent` with `action: "add"`: add the specified strings to the test's top-level `required_absent` list (create the list if it doesn't exist; `required_present`/`required_absent` live at the top level of the test case, per the criteria schema)
   - `runner_context` with `action: "append"`: append the text to the end of the test's `runner_context` field
   - `allowed_tools` with `action: "add_if_missing"`: add only if the tool isn't already in the list (used sparingly, only for AskUserQuestion on error-handling tests)

3. **Validate repaired criteria:**
   ```bash
   python3 <skill-dir>/scripts/validate_eval_criteria.py {eval_criteria_path}
   ```
   If validation fails, revert the criteria file from backup and skip this step.

4. **Post-fix verification (per-test):** Re-run eval runner on ONLY the repaired tests. To isolate specific tests, create a temporary criteria file containing just the repaired test cases (a JSON object with only the target test entries), run eval runner against it, then delete the temp file. Use the same `--workers` and `--judge-rounds` settings.
   - If score >= 0.65: fix accepted. Update the test's triage classification from `criteria_bug` to `pass`. (The 0.65 bar mirrors `ACCEPTANCE_THRESHOLD` in `scripts/hone_common.py`, which is authoritative.)
   - If score < 0.65: revert that test's criteria changes. Reclassify as `real_issue` (the pattern table was wrong; this is a skill problem, not a test problem). Log: "Pattern {pattern_name} did not fix {test_id} (post-fix score: {score}). Reclassified as real_issue."

5. **Log unmatched failures:** For any `criteria_bug` tests that didn't match a pattern, log to workflow state as `unmatched_criteria_bugs`. These go to a human review queue (reported in Final Output). Do NOT attempt LLM-based fixes in V1.

6. **Update triage results:** After all repairs, rebuild the `triaged_results` handoff with updated classifications. Tests that were repaired and passed verification move to `excluded` (with reason `criteria_repaired`). Tests that failed verification move to `actionable_failures`.

**Safety constraints (NON-NEGOTIABLE):**
- NEVER edit the `checks` array or `rubric` fields. These define the quality bar.
- NEVER edit `prompt` fields. These define what's being tested.
- NEVER add tools to `allowed_tools` (except `AskUserQuestion` for error-handling tests via `add_if_missing`).
- NEVER remove items from `required_absent`.
- Pattern table updates happen between runs (human-gated), not during runs.

**Gate: Step 2 → Step 3 (checklist)**
- [ ] Pattern matcher ran and produced valid JSON
- [ ] All matched fixes were applied via Edit tool
- [ ] Repaired criteria passed validation
- [ ] Post-fix verification ran for each repaired test
- [ ] Tests with score >= 0.65 accepted; tests < 0.65 reverted and reclassified (0.65 mirrors `ACCEPTANCE_THRESHOLD` in `scripts/hone_common.py`, which is authoritative)
- [ ] Unmatched criteria bugs logged for human review
- [ ] Triage results updated with new classifications

**Handoff interface (Step 2 → Step 3):**
```
criteria_repair: {
  pattern_matched: number,              // tests fixed by pattern table
  pattern_verified: number,             // fixes that passed post-fix threshold (0.65; mirrors ACCEPTANCE_THRESHOLD in scripts/hone_common.py)
  pattern_reverted: number,             // fixes that failed verification, reclassified
  unmatched: number,                    // criteria bugs with no matching pattern
  unmatched_test_ids: string[],         // for human review queue
  repairs_applied: [{
    test_id: string,
    pattern: string,
    post_fix_score: number,
    status: "accepted" | "reverted"
  }]
}
```
Write to workflow state file. Step 2 reads the updated `triaged_results` which now reflects post-repair classifications.

**Handoff interface (P2 Step 1 → Step 2):**
```
triaged_results: {
  actionable_failures: [{test_id: string, score: number, failure_type: "real_issue"}],
  excluded: [{test_id: string, reason: "criteria_bug" | "variance" | "criteria_repaired" | "inconclusive"}],
  structural_findings: string[]   // from structural_audit, carried forward
}
```
Reason routing: `criteria_repaired` is set by Step 1.5 when a criteria fix was verified; `inconclusive` covers tests that `analyze_results.py --triage` classified as inconclusive (score null, no execution evidence) — they are excluded, never coerced into `actionable_failures`. This enum mirrors the `triaged_results` schema in `scripts/validate_handoff.py`, which is authoritative; update both together.

**Gate: P2 Step 1 → Step 3 (checklist)**
- [ ] Results file was read and parsed successfully (not empty, valid JSON)
- [ ] Each failure has been classified as `criteria_bug`, `variance`, or `real_issue`
- [ ] `structural_findings` array is populated from workflow state (may be empty if structural audit found no issues)
- [ ] If zero actionable failures AND zero structural findings: skip to Final Output

**Auto-populate `open_questions` (BEFORE Step 3):**

Extract questions mechanically from data already in the workflow state file. These are written to `open_questions` before the main thread or fresh-eyes subagent runs, so the LLM cannot omit them. The main thread may ADD more questions but cannot REMOVE auto-generated ones.

Sources:
1. **From eval results:** For each test with score in 0.4-0.7 (ambiguous zone), auto-generate: `"Is {test_id} (score: {score}) failing due to artifact quality or test design?"`
2. **From structural audit:** For each pillar with `applicable: true` AND `passed: false`, auto-generate: `"Structural gap '{pillar_name}' was not addressed. Intentional skip or oversight?"`
3. **From fresh-eyes reconciliation (post-Step 3):** For each finding with `agreement: "single_source"` from the fresh-eyes side, auto-generate: `"Fresh-eyes proposed '{description}' but main thread did not. Was this considered?"`
4. **From fresh-eyes reconciliation (post-Step 3):** For each finding with `agreement: "contradiction"`, auto-generate: `"Main thread and fresh-eyes contradict on '{section}'. Which direction is correct?"`

Source 1-2 are written before Step 3. Sources 3-4 are appended after Step 3 reconciliation completes (in Step 2).

Each auto-generated question is tagged `"source": "auto"` in the state file. LLM-added questions are tagged `"source": "manual"`. The exit gate treats both equally (all must be resolved), but the tags enable audit: if a run exits with zero manual questions but multiple auto questions were resolved, that's a signal the LLM wasn't self-reporting.

Questions are resolved by:
- The improvement in Phase 2 directly addressing the issue (link the finding ID)
- An explicit decision logged in the state file: `"resolved": true, "resolution": "addressed by F3"` or `"resolved": true, "resolution": "intentional: {reason}"`

### Step 3: Fresh-Eyes Analysis

**Purpose:** The same agent that will apply improvements should not be the sole judge of what to improve. This step spawns a clean-context subagent that independently analyzes failures and proposes improvements, providing a second perspective before the main thread commits to an improvement direction. It exists because an agent cannot reliably judge its own work.

**Skip this step if zero actionable failures AND zero structural findings** (nothing to analyze).

**Dispatch in parallel:**

1. **Fresh-eyes subagent** (Task agent, no `model` pin, inherits the session model per improvement preference #1; this is a high-judgment step where proposal quality matters more than fan-out latency): receives ONLY the artifact content, the failing test cases with scores and error output, structural audit findings, and the 18 improvement preferences. It does NOT receive: knowledge of prior hone rounds, what was already tried, the main thread's analysis, any "before" state or change history, or the workflow state file. Its prompt:

   > You are analyzing a Claude Code {artifact_type} that scored below threshold on evaluation. Below is the artifact, the failing tests with their scores and error output, and structural findings. Propose specific improvements: for each, cite the test case or structural finding that motivates it, identify the exact section to edit, and describe the change. Prioritize structural fixes over content fixes. Follow these improvement preferences: [inject ALL rules from SKILL.md's "Improvement Preferences (Non-Negotiable)" section verbatim — do not truncate the list or cite a fixed count, since the list grows]. If the artifact has a `scripts/` directory, also read [references/script-quality-checklist.md](references/script-quality-checklist.md) and check each bundled script against the 5 LLM-judged quality criteria.

2. **Main thread analysis** (existing Step 2 logic): runs concurrently with the subagent.

**Net latency impact:** ~0 seconds. Both analyses run in parallel and take roughly the same amount of time. The reconciliation adds ~10-15 seconds.

**Gate: P2 Step 3 → Step 4 (checklist)**
- [ ] Fresh-eyes subagent returned proposals (non-empty response)
- [ ] Main thread analysis completed (existing Step 2 logic ran)
- [ ] Both proposal sets are available for reconciliation

**Handoff interface (Step 3 → Step 4):**
```
fresh_eyes: {
  proposals: [{
    id: string,                            // eg "FE-1"
    section: string,                       // artifact section to edit
    description: string,                   // what to change
    source_test: string,                   // test case ID or "structural_audit"
    fix_type: "structural" | "content"
  }]
}
```
Write to workflow state file. Step 4 reads both `fresh_eyes` and its own analysis for reconciliation.

**Persist fresh-eyes proposals to disk** (compaction protection):
```bash
python3 -c "
import json
state = json.load(open('/tmp/workflow-${RUN_ID}.json'))
json.dump(state.get('fresh_eyes', {}), open('$OUTPUT_DIR/fresh_eyes.json', 'w'), indent=2)
"
```

### Step 4: Reconcile + Analyze + Performance Audit

**Pre-step validation:** Verify `triaged_results` from Step 1: `actionable_failures` is an array (may be empty), each entry has `test_id` and `score` fields. `structural_findings` is an array. If Step 3 ran, verify `fresh_eyes.proposals` is an array. If shape is malformed: STOP, report "P2 Step 4 handoff validation failed."

**Main thread analysis:** For 3+ failing tests, use parallel sonnet subagents. For 1-2, analyze inline. Map each failure to a specific section of the artifact. Classify fix type.

**Reconciliation (when fresh-eyes ran):** Compare the main thread's findings against `fresh_eyes.proposals`:

| Scenario | Action |
|---|---|
| Both propose the same fix for the same section | High confidence. Include in findings. Tag `agreement: "both"`. |
| Both identify the same problem but propose different fixes | Flag. In `--confirm`: present both to user with evidence. In `--auto`: pick the one citing more specific evidence (file line, test output, pattern name). Tag `agreement: "different_fix"`. |
| One proposes a fix the other missed | Include it, but tag `agreement: "single_source"`. Lower confidence. |
| They contradict (one says add X, other says remove X) | In `--confirm`: present both to user. In `--auto`: skip the edit entirely, log as `agreement: "contradiction"`. |

**Performance audit** (same pass): parallelization opportunities, model selection, script candidates, caching, output quality. One pass, under 60 seconds.

**Gate: P2 Step 4 → Step 5 (checklist)**
- [ ] At least one actionable finding is mapped to a specific section of the artifact
- [ ] Each finding has a fix type classification (content fix, structural fix, criteria fix)
- [ ] Reconciliation completed (if fresh-eyes ran): each finding tagged with agreement level
- [ ] If zero actionable findings after analysis: skip to Final Output (all failures were criteria bugs or variance)

**Handoff interface (P2 Step 4 → Step 5):**
```
improvement_findings: {
  findings: [{
    id: string,                          // eg "F1"
    fix_type: "structural" | "content" | "criteria",
    section: string,                     // artifact section to edit
    description: string,                 // what to change
    source: string,                      // test case ID or "structural_audit" or "performance_audit"
    priority: "HIGH" | "MED" | "LOW",
    agreement: "both" | "different_fix" | "single_source" | "contradiction" | null
  }],
  reconciliation_summary: {
    total_proposals_main: number,        // findings from main thread
    total_proposals_fresh: number,       // findings from fresh-eyes
    agreed: number,                      // both proposed same fix
    different_fix: number,               // same problem, different solution
    single_source: number,              // only one side proposed
    contradictions: number,             // opposing proposals, skipped in --auto
    skipped_contradictions: string[]    // finding IDs skipped due to contradiction
  }
}
```

### Step 5: Generate improvement plan

**Pre-step validation:** Verify `improvement_findings` from Step 4: `findings` is a non-empty array, each entry has `id`, `fix_type`, `section`, and `description` fields. If shape is malformed: STOP, report "P2 Step 5 handoff validation failed."

Present a table of proposed changes with fix type, section, change description, and what motivated it (test case or performance audit). Structural fixes first, then content fixes.

In `--auto` mode: apply all. In `--confirm` mode: ask which to apply.

**Persist improvement plan to disk** (compaction protection):
```bash
python3 -c "
import json
state = json.load(open('/tmp/workflow-${RUN_ID}.json'))
json.dump(state.get('improvement_findings', {}), open('$OUTPUT_DIR/improvement_plan.json', 'w'), indent=2)
"
```

**Gate: P2 Step 5 → Step 6 (checklist)**
- [ ] Improvement plan table was generated with at least one entry
- [ ] Each entry has: fix type, target section, change description, source
- [ ] Structural fixes are listed before content fixes
- [ ] In `--confirm` mode: user has selected which fixes to apply

**Handoff interface (P2 Step 5 → Step 6):**
```
improvement_plan: {
  edits: [{
    id: string,                          // matches finding ID
    target_section: string,              // section heading or line range
    change: string,                      // description of the edit
    approved: boolean                    // true in --auto, user-selected in --confirm
  }],
  total_approved: number
}
```

### Step 6: Apply Edits

**Pre-step validation:** Verify `improvement_plan` from Step 5: `edits` is a non-empty array, each entry has `id`, `target_section`, and `change` fields, `total_approved >= 1`. If shape is malformed: STOP, report "P2 Step 6 handoff validation failed."

**Stale-write guard (MANDATORY before editing):**

Re-read the artifact from disk and compare its content to the `artifact_content` snapshot saved in Step 1. If the content differs, another session (or the user) has modified the file since hone started.

- **If content changed:** STOP. Do NOT apply edits. Log: `"stale_write_guard_triggered": true, "reason": "artifact modified externally since discovery"` in workflow state. Report: "Artifact was modified by another session since hone started. Edits not applied to avoid overwriting concurrent changes. Re-run `/hone` to evaluate the updated version."
- **If content matches:** Proceed with edits.

This prevents the exact failure mode where two CC sessions edit the same artifact and last-write-wins silently destroys the other session's work.

Edit the artifact at `{edit_path}`. After all edits, re-read from disk to confirm they persisted.

**Validator Generation (multi-phase skills and commands only):**

If this hone pass added or modified handoff interface blocks in the artifact, and the artifact has 2+ phases with inter-step data flow, generate a companion validator script. This is not optional. Schema without validator is documentation, not a contract.

1. **Extract schemas from the artifact.** Parse each `Handoff interface (Step N → Step M):` block. For each, extract field names, types (`string`, `number`, `boolean`, `enum`, `array`, `object`), required vs optional markers, and enum values.

2. **Generate `validate_handoffs.py`** in the artifact's directory (eg `~/.claude/skills/{name}/validate_handoffs.py` or `~/.claude/commands/{name}-validator/validate_handoffs.py`). The script:
   - Takes a state file path and a `--handoff <name>` argument
   - Checks required fields exist with correct types
   - Validates enum values against allowed lists
   - Exits 0 (pass), 1 (validation failure with details), 2 (usage error)
   - Uses only Python stdlib (json, sys, argparse). No dependencies.
   - Typically 30-60 lines. Proportional to the schema count, not the skill's line count.

3. **Add invocation instructions to the artifact.** After each handoff interface block, add:
   ```
   Validate: `python3 {validator_path} $STATE_FILE --handoff <handoff_name>`
   ```

4. **Verify the generated script.** Run `python3 -c "import ast; ast.parse(open('{validator_path}').read())"` to confirm valid Python syntax.

**Skip validator generation when:**
- The artifact is single-phase (no inter-step handoffs to validate)
- The artifact is a hook or script (tested via direct Bash invocation, not state files)
- The artifact already has a validator (check for existing `validate_handoffs.py` in the directory)

**Gate: P2 Step 6 → Phase 3 (checklist)**
- [ ] All planned edits were applied (re-read from disk confirms changes present)
- [ ] No syntax errors introduced (for scripts/hooks: `bash -n` check; for skills/commands: markdown structure intact)
- [ ] Edit count matches improvement plan count (no silently skipped edits)
- [ ] If handoff schemas were added to a multi-phase artifact: companion validator script was generated and syntax-checked

**Handoff interface (P2 Step 6 → Phase 3):**
```
applied_edits: {
  edit_count: number,                    // number of edits applied
  confirmed_on_disk: boolean,            // re-read confirms changes present
  artifact_before_snapshot: string,      // pre-edit content path (for revert)
  syntax_check_passed: boolean           // bash -n or markdown structure ok
}
```
Write `artifact_before_snapshot` (pre-edit file content) to the workflow state file before applying edits. Phase 3 reads this for auto-revert on regression.

### Step 7: Description Trigger Testing (skills and commands only)

**Skip this step if:**
- The artifact is a hook or script (no triggering descriptions)
- `--skip-trigger-test` flag is set
- The artifact's description was not modified in Step 4 edits AND no body improvements could affect trigger relevance

**Purpose:** After improving the skill's body content, verify that its description still triggers correctly on relevant prompts. A skill with great body content but a bad description never gets activated. This step follows the methodology from the Agent Skills spec's "Optimizing descriptions" guide, documented in [references/description-trigger-testing.md](references/description-trigger-testing.md).

**Flow:**

1. **Generate trigger eval queries.** Read the artifact's current description and body content. Generate:
   - 8-10 should-trigger queries (realistic user prompts that should activate this skill)
   - 8-10 should-not-trigger queries (near-miss prompts that share keywords but need a different skill)
   - Follow the guidelines in [references/description-trigger-testing.md](references/description-trigger-testing.md)

2. **Collect competing descriptions.** Read `name` and `description` from all skills in the discovery paths (see artifact-profiles.md). This creates the catalog the LLM uses to decide which skill to activate.

3. **Test trigger rates.** For each query, present it alongside the full skill catalog and ask (as a lightweight LLM call, not eval runner): "Which skill(s) would you activate for this query? List skill names only." Run 3 times per query.

4. **Score results.**
   - Should-trigger: trigger rate > 0.5 = PASS
   - Should-not-trigger: trigger rate < 0.5 = PASS
   - Overall accuracy = total passes / total queries

5. **Improve description if needed.** If overall accuracy < 0.8:
   - Identify failure patterns (too narrow → missed triggers, too broad → false triggers)
   - Propose description improvements: generalize from failures (don't add specific keywords from failed queries), add specificity about what the skill does NOT do
   - Ensure description stays under 1024 characters (Agent Skills spec limit)
   - Apply the improved description via Edit tool
   - Re-test with improved description to verify improvement

6. **Store queries.** Write to `{artifact_dir}/{name}-evals/trigger_queries.json` for reuse on subsequent hone rounds.

**Gate: P2 Step 7 → Phase 3 (checklist)**
- [ ] Trigger queries were generated (or reused from prior round)
- [ ] Trigger test completed with accuracy score
- [ ] If accuracy < 0.8: description was improved and re-tested
- [ ] Queries saved to `trigger_queries.json`

**Gate event (write to `gates[]` in workflow state before entering Phase 3):**
```json
{"step": "phase2_to_phase3", "judge": "self-check", "result": "pass", "ts": "<ISO timestamp>"}
```
Append to `state["gates"]` (do not replace). Set `result` to `"fail"` only if the trigger test failed and description could not be improved.

**Handoff interface (Step 7 → Phase 3):**
```
trigger_test: {
  accuracy: number,                      // 0.0-1.0
  should_trigger_pass_rate: number,      // fraction of should-trigger queries that passed
  should_not_trigger_pass_rate: number,  // fraction of should-not-trigger queries that passed
  description_improved: boolean,         // true if description was modified
  queries_path: string                   // path to trigger_queries.json
}
```

After writing the handoff, set `steps.phase2_trigger_test` to `"done"` in the workflow state file (`"skipped"` when `--skip-trigger-test` is set) — the key is seeded as `"pending"` by the SKILL.md state template and the Mechanical Exit Gate checks it.

## Context Compaction Protection (Phase 2)

Phase 2 runs 20-40 minutes. Compaction will happen. After compaction:

1. **Re-read this file** (`references/phase2-improvement.md`)
2. **Re-read the workflow state file** (`/tmp/workflow-${RUN_ID}.json`) to determine current step
3. **Re-read persisted analysis from `$OUTPUT_DIR/`:**
   - `triage.json` -- failure classifications from Step 1
   - `analysis.txt` -- human-readable analysis from Step 1
   - `fresh_eyes.json` -- fresh-eyes proposals from Step 3
   - `improvement_plan.json` -- reconciled findings from Step 4
4. **Re-read the artifact** from `artifact_context.artifact_path` in the state file
5. **Resume from the first non-done step** in the state file
