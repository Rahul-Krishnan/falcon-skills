## Phase 1: Evaluate

### Step 1: Discover the artifact

Use the artifact profile to locate the file. For skills, check local paths (`~/.claude/skills/{name}/SKILL.md` and cross-client paths). For commands/hooks/scripts, check the local path directly.

Read the artifact content and store as `{artifact_content}`. Set `{artifact_path}` (read path) and `{edit_path}` (edit path; for marketplace skills, this is the repo source path).

**Original backup (MANDATORY):** Immediately after reading the artifact, save the original to a session-scoped temp file at `/tmp/hone-original-{name}-$(date +%Y%m%d_%H%M%S).md`.

**Which tool to use is a size decision.** Check first: `wc -c {artifact_path}`.

- **Under 10 KB:** use the Write tool, passing the content you already read. Dedicated tools give the user better visibility into what was backed up, and re-emitting a file this small costs little.
- **10 KB or larger:** use `cp {artifact_path} {backup_path}` in Bash. Re-emitting a large file through Write burns output tokens and wall-clock latency in proportion to file size, and buys no visibility that matters: this backup exists for rollback and diff, not for review. Improvement preference 5 (efficiency = latency) governs above the threshold.

Either way, confirm the backup landed (`test -s {backup_path}`) before proceeding.

Store the backup path as `{original_backup_path}` in the workflow state file alongside `artifact_context`. This backup serves three purposes: (1) cumulative diff at session end (`diff {original_backup_path} {artifact_path}`), (2) rollback if all improvement rounds degrade quality, (3) reference during improvement to avoid drift from original intent. The stale-write guard in Phase 2 Step 4 compares against `{artifact_content}` (in-memory snapshot), not this backup file.

**Scope/Intent Signal (inferred, not interactive):**

After reading the artifact, infer two signals that scope downstream evaluation. This adds ~0 latency (pure text analysis, no tool calls) and prevents the "torque wrench on a thumbscrew" problem.

1. **Complexity tier** (from line count and structure):
   - `lightweight` (< 100 lines, no multi-step workflow): skip structural audit (Step 2), use 2 eval test cases minimum instead of 4, skip fresh-eyes (Phase 2 Step 3). Hooks and simple scripts are typically here.
   - `standard` (100-500 lines, or multi-step workflow): full pipeline. Most skills and commands.
   - `complex` (500+ lines, or 3+ phases): full pipeline + extra eval test cases (6-8 instead of 4-6).

2. **Primary quality dimension** (from artifact description and content):
   - `correctness` — the artifact's value is in producing correct outputs (scripts, hooks, data processing)
   - `instruction_clarity` — the artifact's value is in guiding LLM behavior (skills, commands)
   - `orchestration` — the artifact's value is in coordinating multi-step workflows (pipeline commands like forge, smelt)

   This signal weights eval dimensions: `correctness` artifacts get heavier weight on correctness/error_handling dimensions; `instruction_clarity` artifacts get heavier weight on task_completion/invocation; `orchestration` artifacts get heavier weight on best_practices/efficiency.

Record both signals in the workflow state file alongside `artifact_context`. These are **heuristic defaults**, not hard constraints. If the eval criteria or structural audit reveals that the inferred tier was wrong (eg a 50-line hook that's actually very complex), the pipeline continues at full depth.

**Gate: Step 1 → Step 2 (checklist)**
- [ ] Artifact file exists at the expected path
- [ ] `{artifact_content}` is non-empty (file was successfully read)
- [ ] `{artifact_path}` and `{edit_path}` are both set
- [ ] If artifact not found: STOP with a helpful error that includes: (a) all paths checked (profile path, marketplace search), (b) suggestion to list available artifacts (`ls ~/.claude/skills/` for skills, `ls ~/.claude/commands/` for commands, etc.), (c) similar name suggestions if a close match exists. Do not proceed to Step 2.

**Handoff interface (Step 1 → Step 2):**
```
artifact_context: {
  artifact_content: string,       // full file content
  artifact_path: string,          // read path
  edit_path: string,              // edit path (may differ for marketplace)
  original_backup_path: string,   // /tmp/ backup for rollback and cumulative diff
  artifact_type: "skill" | "command" | "hook" | "script",
  artifact_name: string,
  scope_intent: {                 // inferred, not interactive
    complexity_tier: "lightweight" | "standard" | "complex",
    primary_dimension: "correctness" | "instruction_clarity" | "orchestration",
    line_count: number
  }
}
```
Step 2 validates: `artifact_content` is non-empty, `artifact_type` is one of the four valid types. If validation fails, STOP (Step 1 gate should have caught this, so this is a defensive check).

### Step 2: Structural Audit (skills and commands only)

**Skip this step for hooks and scripts** (they don't have multi-step workflow structure).
**Skip this step if `scope_intent.complexity_tier` is `lightweight`** (lightweight artifacts don't benefit from structural enforcement; the scope/intent signal already determined this artifact is too small for structural overhead).

Run the deterministic structural audit script:

```bash
python3 <skill-dir>/scripts/structural_audit.py {artifact_path} --type {artifact_type} --name {artifact_name} --complexity-tier {scope_intent.complexity_tier} $(test -d "{artifact_dir}/scripts" && echo "--scripts-dir {artifact_dir}/scripts") --json
```

Parse JSON output. The script scans for 14 structural pillars using regex patterns. Each pillar now has an `effective_priority` field (HIGH, LOW, or skip) that varies by the artifact's complexity tier. Pillars with `effective_priority: "skip"` are excluded from the audit entirely. Security violations still cap the structural score at 0.3 regardless of tier.

**Pillar applicability:** Not all pillars apply to every artifact. The structural audit script handles this automatically, but for knowledge extraction tasks, here is the explicit mapping:

| Pillar | Applies When | Skips When |
|--------|-------------|------------|
| 1. progress_gates | Always (if artifact has 2+ steps) | Single-step artifacts |
| 2. handoff_interfaces | Always (if artifact has 2+ steps) | Single-step artifacts |
| 3. state_persistence | Always (if artifact has 2+ steps) | Single-step artifacts |
| 4. schema_validation | Always (if handoff interfaces present) | No handoff interfaces |
| 5. anti_laziness | Multi-step pipeline commands/skills | Simple single-pass artifacts |
| 6. research_depth | Artifacts with a dedicated research phase | Artifacts without research phases |
| 7. complexity_aware | **temper-review only** (big-brain trigger for complex diffs) | All other artifacts |
| 8. data_provenance | Artifacts that produce scores consumed by decisions | Artifacts with no scoring output |
| 9. security | **Bash-using artifacts only** (scans for credential refs, exfil, base64) | Pure markdown artifacts with no script invocations |
| 10. description_guardrails | Skills and commands (scans description for "when NOT to use" anti-pattern guidance) | Hooks and scripts (no triggering descriptions) |
| 11. script_quality | Skills/commands with a `scripts/` directory (checks bundled scripts for agentic design: no interactive prompts, --help, structured output, exit codes, self-contained deps) | Artifacts without `scripts/` directory. Advisory only (WARNING_ONLY). |
| 12. compaction_protection | Multi-step skills/commands (2+ steps). Checks 5 categories: explicit compaction section, re-read instructions, intermediate persistence, reference re-read anchors, resume instructions. Need 2+ to pass. Priority: complex=HIGH, standard=LOW, lightweight=skip. | Hooks, scripts, single-step artifacts. Advisory only (WARNING_ONLY). |
| 13. spec_compliance | Skills and commands (checks description length 1-1024 chars, body < 500 lines, no root-level custom frontmatter fields). Advisory only (WARNING_ONLY). | Hooks and scripts (no frontmatter spec). |
| 14. autonomous_execution | Skills/commands that advertise `--auto` or non-interactive mode — checks that blocking calls (AskUserQuestion) are in validation gates, not mid-flow. Advisory only (WARNING_ONLY). N/A (not penalized) for artifacts that don't advertise autonomous mode. | Artifacts with no `--auto` documentation. |

When a pillar is inapplicable, it is marked `applicable: false` in the script output and excluded from the structural_score denominator.

Map the script output to the handoff interface:
- `pillars[].passed` → `transitions[].status` (gated/ungated)
- `structural_score` → `structural_score`
- `findings` → `findings`

**Gate: Step 2 → Step 3 (checklist)**
- [ ] All step transitions in the artifact have been enumerated
- [ ] Each transition is classified as gated or ungated
- [ ] Each handoff is classified as typed or implicit
- [ ] State persistence check is complete
- [ ] Anti-laziness self-check presence verified (for multi-step artifacts)
- [ ] Research depth enforcement verified (for research-bearing artifacts)
- [ ] Complexity-aware analysis verified (for temper-review only)
- [ ] Data provenance verified (for artifacts producing scores consumed by decisions)
- [ ] Compaction protection verified (for multi-step artifacts with 2+ steps)
- [ ] If structural_score < 1.0: findings are recorded for Phase 2

**Handoff interface (Step 2 → Phase 2 structural findings):**
```
structural_audit: {
  structural_score: number,              // 0.0-1.0
  transitions: [{
    from: string,                        // eg "Step 1"
    to: string,                          // eg "Step 2"
    gate_type: "checklist" | "rubric" | "crucible" | "interaction_schema" | "none",
    status: "gated" | "ungated"
  }],
  handoffs: [{
    from: string,
    to: string,
    has_interface: boolean,
    has_validation: boolean
  }],
  has_state_persistence: boolean,
  state_persistence_needed: boolean,     // true if 2+ steps
  has_anti_laziness_check: boolean,      // true if references ANTI-LAZINESS SELF-CHECK
  anti_laziness_needed: boolean,         // true if 2+ steps (pipeline command)
  has_research_depth_enforcement: boolean, // true if invokes temper-research in research phase
  research_depth_needed: boolean,        // true if artifact has a research phase
  has_complexity_aware_analysis: boolean, // true if has big-brain trigger for complex diffs
  complexity_aware_needed: boolean,      // true only for temper-review
  findings: string[]                     // human-readable list of structural gaps
}
```
Write this to workflow state file. Phase 2 reads structural findings from the file. **Only findings with `effective_priority: "HIGH"` drive Phase 2 improvements.** Findings with `effective_priority: "LOW"` are reported in the final output but do not become actionable improvement targets. This prevents context bloat from hone adding gates and handoff interfaces to artifacts that don't need them.

### Step 3: Check for existing eval criteria

Look for `eval_criteria.json` using the resolution order in the artifact profile.

**If more than one candidate exists**, report every path found with its test-case count and test IDs, then use the canonical-for-writes path. Do not silently pick the first hit: divergent suites accumulate at these paths (a stale suite may not contain the failure-mode tests at all), and scoring against the wrong one produces a grade for tests the artifact was never evaluated on. Cleaning up the surplus files is a separate manual action, and per the file-safety rule that means `trash`, never `rm`.

- **If `--reuse-criteria` AND criteria exist:** Proceed to Step 4 (Criteria Audit), then Step 8.
- **If `--fix-only`:** Skip to Phase 2.
- **If criteria exist AND `--auto`:** Proceed to Step 4 (Criteria Audit), then Step 8.
- **If criteria exist AND `--confirm`:** Ask whether to reuse or regenerate. If reuse: proceed to Step 4.
- **If no criteria exist:** Proceed to Step 5 (skip Step 4, nothing to audit).

**Gate: Step 3 → Step 4 or Step 5 (checklist)**
- [ ] Eval criteria path was checked on disk
- [ ] Routing decision is one of: reuse (Step 4 -> Step 6 -> Step 8), regenerate (Step 5 -> Step 6 -> Step 8), fix-only (Phase 2)
- [ ] If reusing: file is non-empty and contains at least 3 test cases (quick line count check)
- [ ] If criteria file exists but is empty or corrupt: treat as "no criteria exist", proceed to Step 5

**Handoff interface (Step 3 → Step 4 or Step 5):**
```
routing_decision: {
  has_existing_criteria: boolean,     // file exists on disk
  criteria_path: string,             // full path to eval_criteria.json
  criteria_valid: boolean,           // non-empty, 3+ test cases
  route: "reuse" | "regenerate" | "fix_only"
}
```
Step 4 validates: `route` is "reuse" and `criteria_valid` is true. Step 5 validates: `route` is "regenerate". Step 8 validates: `criteria_path` is non-empty and `criteria_valid` is true.

### Step 4: Criteria Audit (skills and commands only)


**Skip this step for hooks and scripts** (they use direct input/output test cases, not JSON eval criteria with dimension weightings and semantic prompts; criteria audit does not apply).

**Skip this step if Step 3 routed to "regenerate" or "fix_only"** (nothing to audit if criteria will be freshly generated or skipped entirely).

This step audits existing eval criteria for common setup and effectiveness issues BEFORE the eval/improve loop begins. Once the eval loop starts (Step 8+), criteria are frozen on disk and not modified.

**Gaming protection (honest assessment):** The protection is temporal, not architectural. The audit runs before any artifact scoring happens, so the audit agent has no knowledge of artifact scores and no incentive to make tests easier. The same session context runs both the audit and the eval loop, so there is no hard isolation. The protection comes from sequencing: criteria are fixed before scores exist.

**Flow:**

1. **Backup:** Preserve `eval_criteria.json` as `eval_criteria.json.pre-audit` so the audit can be reverted if it makes things worse. Same 10 KB threshold as Step 1: below it, Read then Write; at or above it, `cp`. Eval criteria files routinely run tens of KB, so `cp` is the normal path here.

2. **Run deterministic audit:**
   ```bash
   python3 {validate_script_path} --audit {criteria_path} --artifact-path {artifact_path}
   ```
   Parse JSON output from stdout. The script checks: missing runner_context, missing/incomplete allowed_tools, missing target_skills, keyword-only semantic checks (heuristic), minimum test count.

3. **Apply fixable findings:** For each finding with `severity == "fixable"`, use the Edit tool to apply the suggested fix to the criteria JSON file on disk. The script identifies the issue and suggests the value; the main thread (session model) applies it via Edit.

4. **LLM classification sub-task (main thread, not subagent):** For each test case's semantic_checks, classify the `question` field as "behavioral" (measures what the skill DOES when invoked) or "keyword" (checks for string presence without measuring actual behavior). This is synthesis work (holistic judgment over the full criteria set), not per-test analysis. The classification does NOT modify the criteria file. It produces labels that feed into the regeneration decision.

5. **Regeneration decision:** Combine the script's `should_regenerate` flag with the LLM classification results. If >50% of test cases have unfixable issues (missing runner_context warnings + LLM-confirmed keyword-only checks), delete the criteria file and override Step 3's routing to "regenerate" so Step 5 runs. Log: "Criteria audit: >50% of test cases have unfixable issues, regenerating."

6. If `should_regenerate` is false and fixable findings were applied: log "Criteria audit: applied {N} fixes. {M} warnings remain."

7. If no findings: log "Criteria audit: clean."

**Gate: Step 4 → Step 5 or Step 8 (checklist)**
- [ ] Audit script ran and produced valid JSON output (parseable, has `findings` array)
- [ ] All fixable findings were applied via Edit tool
- [ ] If `should_regenerate`: criteria file was deleted (verified), routing overridden to "regenerate"
- [ ] If fixes applied: re-read criteria from disk to confirm changes persisted
- [ ] LLM classification completed for all test cases with semantic_checks
- [ ] Workflow state updated with `criteria_audit` result

**Handoff interface (Step 4 → Step 5 or Step 8):**
```
criteria_audit: {
  criteria_existed: boolean,
  backup_path: string,               // path to .pre-audit backup
  audit_ran: boolean,
  fixable_applied: number,           // fixes applied via Edit tool
  warnings: string[],                // unfixable issues (for logging)
  should_regenerate: boolean,
  criteria_deleted: boolean,          // true if file was removed for regeneration
  classification_results: [{         // from main-thread LLM pass
    test_id: string,
    classification: "behavioral" | "keyword" | "mixed",
    evidence: string
  }]
}
```
Write to workflow state file. If Step 4 triggers regeneration, Step 5 reads `criteria_audit` and knows WHY regeneration was needed (can produce better criteria by avoiding the same issues).

### Step 5: Generate eval criteria

**For skills:** Generate inline. Read the skill's SKILL.md file, extract its purpose, allowed-tools, argument-hint, and workflow steps. Create 4-8 test cases:

1. **Standard invocation** -- the most common use case with typical arguments. Prompt: "Run /skill-name with [typical args]". Evaluate: did it follow the documented workflow steps in order? Did it produce the expected output format?
2. **No-args invocation** -- invoke without arguments when the skill expects them. Evaluate: did it ask for missing args via AskUserQuestion (or proceed with defaults if the skill supports that)?
3. **Edge case** -- unusual but valid input (eg empty codebase state, no matching files, no diffs). Evaluate: graceful handling, no crashes, informative output about the empty state.
4. **Task completion** -- a realistic end-to-end scenario matching the skill's primary use case. Evaluate: did it achieve the stated goal, not just run through motions?
5. **Tool usage efficiency** -- does the skill parallelize independent tool calls? Does it avoid redundant reads? Evaluate: tool call count and parallelism.
6. **Business impact** -- does the output actually help the user make a decision or take action? Evaluate: actionability of the final output, not just format compliance.
7. **Compaction resilience** (multi-step artifacts only, skip for lightweight/single-step) -- simulate a mid-execution context compaction. The runner_context should state: "You are resuming this skill after context compaction. You have NO memory of prior conversation turns. A workflow state file exists at /tmp/workflow-test-session.json with the first 1-2 steps marked done and the next step marked in_progress. The skill's instructions and reference files are available on disk but not in your conversation history." The prompt should invoke the skill normally. Evaluate: does it re-read its own instruction file? Does it check the workflow state file? Does it announce what it's resuming from? Does it skip completed steps instead of restarting? A skill that restarts from scratch or ignores the state file fails this test.

8. **Failure-mode test cases** (`test_profile: "failure_mode"`, complex artifacts only, 2-4 cases — skip for lightweight and standard): Exercise structural patterns under adversarial/failure conditions. Only generate these for `complex` artifacts with multi-phase workflows. Each case injects a failure condition via `runner_context` and evaluates whether the skill's instructions handle it correctly. The `runner_context` MUST start with `SIMULATION MODE: do not issue real tool calls.` followed by `FAILURE INJECTION: [description]`.

   Standard failure scenarios to cover (pick 2-4 based on which structural pillars the artifact exercises):
   - **Corrupt state file**: inject malformed JSON as the workflow state. Evaluate: does the skill halt with an error rather than silently continuing with broken state tracking?
   - **Handoff validation failure**: simulate `validate_handoff.py` returning exit code 1. Evaluate: does the skill fix the state and re-validate before proceeding (not skip or ignore the failure)?
   - **Mid-execution compaction**: inject a state file with steps 1-2 done and step 3 in_progress. Evaluate: does the skill re-read its own instruction files AND resume from the correct step (not restart from step 1)?
   - **Regression auto-revert**: inject Phase 3 re-eval scores showing a dimension dropped >0.1. Evaluate: does auto-revert fire AND does the improvement loop halt (not continue to the next round)?

   `runner_context` template:
   ```
   SIMULATION MODE: do not issue real tool calls.
   FAILURE INJECTION: [brief description of injected condition]

   [Simulated state or script output — JSON blob or command output to treat as real]

   You are executing the [skill name] skill. Read ~/.claude/skills/[name]/SKILL.md.
   [Setup: which steps have completed before the failure point]

   The failure condition above has just occurred. Follow what the skill instructs
   you to do when this condition is encountered.

   SAFETY SANDBOX — side-effect simulation mode
   Do NOT execute git push, gh pr create, or any destructive commands for real.
   Do NOT invoke side-effecting sub-skills (anything that commits, pushes, opens a PR, or posts) for real.
   ```

   Rubric template:
   - Score 5: detects the failure AND follows the documented recovery path exactly (or halts correctly)
   - Score 3: detects the failure but takes an incomplete or incorrect recovery path
   - Score 1: does not detect the failure, proceeds as if nothing happened (critical failure)

   Scoring: `test_profile: "failure_mode"` uses `FAILURE_MODE_WEIGHTS` in `score_execution.py` — `gate_compliance` (0.51), `error_handling` (0.34), `quality_checks` (0.15). `quality_checks` **is** scored for failure-mode tests, so `required_present` / `required_absent` assertions apply. The execution dimensions (`workflow_sequence`, `parallel_efficiency`, `state_persistence`, `output_structure`) are not scored.

   **Writing `required_absent` entries:** use forward-progress markers, not topic nouns. Good entries are phrases an executor can only produce while continuing past the failure ("proceeding to Step 3", "launching the eval runner", "generating test cases now"). Bad entries are bare nouns a correct halt message must name in order to explain what it is declining to do ("structural audit", "Phase 1", "criteria generation"). Matching is substring-based, so a denial like "does NOT run the structural audit" still counts as a violation unless the scorer's negation check clears it.

Dimensions: task_completion (0.3), invocation (0.2), efficiency (0.2), best_practices (0.15), business_impact (0.15).

**For commands:** Generate inline. Read the command's `.md` file, extract its purpose from the description and instructions. Create 4-6 test cases:

1. **Standard invocation** — the most common use case with typical arguments. Prompt: "Run /command-name with [typical args]". Evaluate: did it follow the documented workflow steps in order? Did it produce the expected output format?
2. **No-args invocation** — invoke without arguments when the command expects them. Evaluate: did it ask for missing args via AskUserQuestion (or proceed with defaults if the command supports that)?
3. **Edge case** — unusual but valid input (eg empty file state, no diffs, no calendar entries). Evaluate: graceful handling, no crashes, informative output about the empty state.
4. **Output quality** — focus on whether the output matches the command's documented format (tables, markdown headers, structured sections). Use semantic checks, not substring matching.
5. **Compaction resilience** (multi-step commands only, skip for single-step) — simulate a mid-execution context compaction. The runner_context should state: "You are resuming this command after context compaction. You have NO memory of prior conversation turns. A workflow state file exists at /tmp/workflow-test-session.json with the first 1-2 steps marked done and the next step marked in_progress. The command's instructions and reference files are available on disk but not in your conversation history." The prompt should invoke the command normally. Evaluate: does it re-read its own instruction file? Does it check the workflow state file? Does it announce what it's resuming from? Does it skip completed steps instead of restarting? A command that restarts from scratch or ignores the state file fails this test.

Dimensions: task_completion (does it complete the documented workflow?), invocation (does it parse args correctly?), efficiency (parallel tool calls where possible?), best_practices (error handling, no hardcoded assumptions), output_quality (matches documented format, actionable).

**For hooks:** Generate inline. Read the hook script, understand its trigger event (Stop, PostToolUse, UserPromptSubmit, etc.) and what it checks. Create 4-6 test cases as **input/output pairs** piped through the script:

1. **True positive** — input that should trigger the hook. Pipe JSON matching the hook's event schema through stdin. Verify the hook produces output (non-empty stdout with the expected JSON structure).
2. **True negative** — input that should NOT trigger. Pipe clean input through stdin. Verify the hook produces no output (empty stdout, exit 0).
3. **Edge case input** — malformed JSON, missing fields, empty message. Verify the hook doesn't crash (exit 0, no stderr stacktrace).
4. **Throttle behavior** — if the hook has throttling, test that it suppresses output on rapid re-invocation.
5. **Performance** — time the hook execution. It should complete in under 1 second for PostToolUse/Stop hooks (they block the response).

Dimensions: trigger_accuracy (does it fire on the right inputs?), false_positive_rate (does it stay silent on clean inputs?), performance (execution time under budget), output_quality (correct JSON structure in output), resilience (handles malformed input without crashing).

Test methodology: Write JSON test inputs to `/tmp/hone_test_input.json` via heredoc, then pipe from file. Do NOT use inline `echo '<json>' |` which causes shell quoting errors. These are unit tests of the script, not eval runner tests.

**Bash invocation error protocol (hooks and scripts):**

Two classes of errors can occur when running hook/script tests:

1. **Tool call error** (the Bash tool itself fails or times out): This is a transient infrastructure error, not a hook failure. Retry the exact command once. If the retry also fails: record the test case as `status: "tool_error"`, log the error message, and count it against coverage (a tool_error is NOT a pass). After all test cases run, if any tool_error cases exist, attempt one more retry batch for the failed cases only before proceeding to the gate.

2. **Non-zero exit from the hook/script**: This is EXPECTED for hooks that trigger. A hook that fires (true positive case) may exit 1 or 2 to signal the Claude Code runtime. Evaluate the hook's stdout/stderr output against the test expectation — do NOT classify a non-zero exit as a test failure without first checking whether the expected behavior was a trigger.

**Shell quoting rule (MANDATORY — apply BEFORE every hook/script Bash call):**

All hook/script test inputs MUST use file-based piping. Do NOT use inline `echo '<json>' |` which is error-prone with quoting.

Step 1 — Write input to a temp file:
```bash
cat > /tmp/hone_test_input.json << 'EOF'
<json_input_here>
EOF
```

Step 2 — Pipe from file:
```bash
cat /tmp/hone_test_input.json | /path/to/hook.sh
```

This eliminates all shell quoting errors. The heredoc `'EOF'` (single-quoted) prevents variable expansion. For multiple test cases, reuse the same temp file path (overwrite each time).

**For scripts:** Generate inline. Read the script, understand its inputs (args, stdin, env vars) and expected outputs (stdout, files, exit codes). Create 4-6 test cases as **direct invocations**:

1. **Happy path** — run with expected inputs. Verify correct stdout output and exit code 0.
2. **Missing input** — omit a required argument or input file. Verify informative error message and non-zero exit code.
3. **Invalid input** — provide malformed data (bad JSON, wrong file format, non-existent path). Verify graceful error, no stacktrace.
4. **Idempotency** — run twice with the same input. Verify output is identical (scripts should be deterministic).
5. **Edge case** — empty input, very large input, special characters in paths/args.

Dimensions: correctness (right output for right input), performance (execution time reasonable), error_handling (informative errors, no crashes on bad input), output_format (consistent, parseable output), maintainability (readable code, no magic numbers).

Test methodology: direct Bash invocation, same as hooks. Capture stdout, stderr, and exit code. Compare against expected values.

**For all inline-generated criteria:** Follow eval criteria best practices (no brittle `required_present`, semantic checks over substring matching, `"##"` as a minimal structural check). Write the generated criteria to `{eval_criteria_path}` and validate with the script before proceeding.

**Gate: Step 5 → Step 6 (checklist)**
- [ ] Eval criteria file was written to `{eval_criteria_path}` (file exists on disk)
- [ ] File contains at least 3 test cases
- [ ] `validate_eval_criteria.py` passes with no errors (warnings acceptable)
- [ ] If validation fails: fix criteria inline, re-validate. Do not proceed with broken criteria.
- [ ] **For hooks:** All mandatory profile test case types are present: `true_positive`, `true_negative`, `edge_case_input`, `performance`. `throttle_behavior` is required only if the hook script contains throttling logic (scan for `last_run`, `throttle`, or `debounce` patterns before generating criteria). If a required type is absent: add it before marking this gate done. Do NOT proceed with fewer than 4 hook test cases.
- [ ] **For scripts:** All mandatory profile test case types are present: `happy_path`, `missing_input`, `invalid_input`, `idempotency`. `edge_case` is required only if the script has conditional branches. If a required type is absent: add it before marking this gate done.

**Handoff interface (Step 5 → Step 6):**
```
generated_criteria: {
  criteria_path: string,             // path to eval_criteria.json
  test_count: number,                // number of test cases generated
  validation_passed: boolean,        // validate_eval_criteria.py exit 0
  dimensions: string[]               // dimension names used
}
```
Step 6 validates: `validation_passed` is true and `test_count >= 3`. If validation fails, STOP.

### Step 6: Programmatic Checks Enrichment (skills and commands only)

**Skip this step for hooks and scripts** (they use direct Bash testing, not JSON-based eval criteria with semantic checks).

After criteria exist (from Step 4 audit or Step 5 generation), enrich them with deterministic `required_present` checks extracted from the artifact. This adds a stable floor of verifiable facts alongside the LLM judge's semantic evaluation.

**Pre-enrichment backup (MANDATORY):**
```bash
cp {eval_criteria_path} {eval_criteria_path}.pre-enrich
```

```bash
python3 <skill-dir>/scripts/enrich_programmatic_checks.py \
  --artifact-path {artifact_path} \
  --criteria-path {eval_criteria_path} \
  --json
```

Parse JSON output. Log: "Enrichment: added {N} programmatic checks across {M} test cases."

If the script exits with code 2 (nothing to enrich), proceed normally. If it exits with code 1 (error), log the error but do not block the pipeline.

After enrichment, re-validate the criteria:
```bash
python3 <skill-dir>/scripts/validate_eval_criteria.py {eval_criteria_path}
```

If validation fails after enrichment, the enrichment introduced invalid entries. Restore from the `.pre-enrich` backup and proceed without enrichment.

### Step 7: Side-Effect Guard (all artifact types)

Scan the artifact for commands and tool invocations that cause real-world side effects (PR submissions, messaging service posts, source control mutations). If found, modify the eval criteria to sandbox them so eval runner evaluates the skill's logic without executing dangerous commands.

This step is critical for unattended runs where no human is present to catch accidental side effects.

```bash
python3 <skill-dir>/scripts/side_effect_guard.py \
  --artifact-path {artifact_path} \
  --criteria-path {eval_criteria_path} \
  --json
```

Parse JSON output. The script:
1. Scans the artifact (and `references/` directory if present) for:
   - Bash side effects: `git push`, `git push --force`, `gh pr create`, `gh pr merge`, `git commit`
   - MCP tool patterns: `google_chat`, `send_message`, `send_message_as_user`
   - Delegated side-effecting skills: any skill the artifact invokes that commits, pushes, opens a PR, or posts to a service. The default list lives in `side_effect_guard.py`; extend it for your own pipeline skills.
2. If side effects detected:
   - Removes dangerous MCP tools from `allowed_tools` in each test case
   - Prepends a SAFETY SANDBOX block to `runner_context` instructing the executor to simulate (not execute) dangerous commands
3. Exit codes: 0 = criteria modified, 1 = error, 2 = no side effects (no changes needed)

Log: "Side-effect guard: {action}. Bash: {bash_commands}. MCP removed: {tools_removed}. Delegated: {delegated_skills}."

If exit code 2: proceed normally (artifact is safe). If exit code 1: log warning but do not block.

### Step 8: Validate and run eval runner

**Pre-launch validation (MANDATORY):**

```bash
python3 <skill-dir>/scripts/validate_eval_criteria.py {eval_criteria_path}
```

Fix any warnings. Re-read from disk after edits.

**Run in-session subagent evaluation:**

```bash
ARTIFACT_NAME="{name}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="$HOME/skill-eval/$ARTIFACT_NAME/$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"

# Timing capture
EVAL_START_NS=$(date +%s%N)
```

Spawn one subagent per test case in the eval criteria (parallel, up to `{workers}` concurrent). Each subagent receives:
- The full artifact content (skill dir or file)
- The `runner_context` from the test case
- The `prompt` from the test case
- The `allowed_tools` from the test case
- Instruction to write its response to `$OUTPUT_DIR/tc-{test_id}.json`

Collect all subagent results. Write a merged `$OUTPUT_DIR/results.json` in the standard format: `{"results": [{test_id, score, agent_response, ...}]}`.

```bash
EVAL_END_NS=$(date +%s%N)
EVAL_DURATION_MS=$(( (EVAL_END_NS - EVAL_START_NS) / 1000000 ))
```

Record `EVAL_DURATION_MS` in the workflow state file as `timing.duration_ms`. Estimate tokens from the total agent_response character count in results.json: `tokens_estimate = total_chars / 4` (rough approximation). Record as `timing.tokens_estimate`.

**Baseline runs (optional):**

Run a baseline (without-skill) evaluation when:
- `--with-baseline` flag is set, OR
- This is the first eval of the artifact (no prior results exist at `{eval_criteria_path}`)

If baseline should run:
```bash
BASELINE_DIR="$OUTPUT_DIR/baseline"
mkdir -p "$BASELINE_DIR"

BASELINE_START_NS=$(date +%s%N)
```

Run the same subagent evaluation without loading the skill context. Write results to `$BASELINE_DIR/results.json`.

```bash
BASELINE_END_NS=$(date +%s%N)
```

Record `BASELINE_DIR` in the workflow state file as `baseline_dir`. If baseline was not run, set `baseline_dir` to null.

**Error Recovery — Subagent evaluation failure:**
If a subagent exits with an error or produces no output:
1. Check for criteria issues: run `validate_eval_criteria.py`, fix any warnings, retry the failing test cases once
2. If retry also fails: log `"eval_fallback": true`, record partial results. Do NOT abort the session.

**Error Recovery — Evaluation timeout:**
If eval runner or subagent evaluation exceeds 10 minutes per test case:
1. Kill the hung process
2. Record completed test results (partial results are better than none)
3. Log `"evaluation_timeout": true, "completed_tests": N, "total_tests": M` in workflow state
4. Proceed with partial results. Phase 2 will work with whatever scores are available. Flag the incomplete tests in the report.

**Error Recovery — File write failure (Phase 2):**
If an Edit tool call fails during Phase 2 Step 4 (artifact modification):
1. Re-read the artifact from disk to check current state
2. If the file is locked or read-only: log `"file_write_failed": true`, report the error, STOP. Do not retry writes on locked files.
3. If the file was deleted or moved: log `"file_missing": true`, report the error, STOP.
4. If transient error (disk full, permissions): retry the edit once. If retry fails, restore from `{original_backup_path}` and report.

**Gate: Step 8 → Step 9 (checklist)**
- [ ] Subagent evaluation completed without crash
- [ ] Results file exists at `$OUTPUT_DIR/results.json`
- [ ] At least one test case has a numeric score
- [ ] If evaluation crashed: check criteria, fix, retry once. If retry fails: report error and exit.

**Handoff interface (Step 8 → Step 9):**
```
judge_results: {
  output_dir: string,                // directory containing results.json
  results_path: string,              // full path to results.json
  completed: boolean,                // evaluation exited without crash
  test_count: number,                // number of test cases evaluated
  method: "eval_runner" | "subagent"
}
```
Step 9 validates: `completed` is true, `results_path` file exists on disk, `test_count >= 1`. If validation fails, STOP.

### Step 9: Deterministic Scoring


After eval runner completes, run deterministic scoring on the same execution data:

```bash
python3 <skill-dir>/scripts/score_execution.py $OUTPUT_DIR/results.json --type {artifact_type} --artifact-path {artifact_path} --criteria-path {eval_criteria_path} --json
```

Output written to: `$OUTPUT_DIR/deterministic_scores.json`

**During parallel phase, report BOTH scores:**
```
Score (deterministic): 0.82 (B)
Score (LLM judge, ref): 0.78 (B)
```

Phase 2 decisions use the deterministic score. The LLM judge score is a reference signal only.

### Step 10: Generate Spec-Format Artifacts

After deterministic scoring, generate Agent Skills open standard eval artifacts:

```bash
python3 <skill-dir>/scripts/generate_spec_artifacts.py \
  $OUTPUT_DIR \
  --criteria {eval_criteria_path} \
  --timing-ms $EVAL_DURATION_MS \
  --timing-tokens $(python3 -c "import json; r=json.load(open('$OUTPUT_DIR/results.json')); print(sum(len(t.get('agent_response','')) for t in r.get('results',[]))//4)") \
  $(test -n "$BASELINE_DIR" && echo "--baseline-dir $BASELINE_DIR") \
  --json
```

Output written to `$OUTPUT_DIR/`: `evals.json`, `grading.json`, `timing.json`, `benchmark.json`.

**Gate: Step 10 → Step 11 (checklist)**
- [ ] `evals.json` exists in `$OUTPUT_DIR` and is valid JSON
- [ ] `grading.json` exists and contains `assertion_results` array
- [ ] `timing.json` exists with `duration_ms` field
- [ ] `benchmark.json` exists with `run_summary.with_skill` object
- [ ] If baseline ran: `benchmark.json` has non-null `without_skill` and `delta`

If any check fails, log a warning but proceed (spec artifacts are supplementary, not blocking).

**Handoff interface (Step 10 → Step 11):**
```
spec_artifacts: {
  evals_path: string,               // path to evals.json
  grading_path: string,             // path to grading.json
  timing_path: string,              // path to timing.json
  benchmark_path: string,           // path to benchmark.json
  has_baseline: boolean,            // true if baseline delta was computed
  generation_success: boolean       // false if script errored (non-blocking)
}
```

### Step 11: Reference Validation (skills and commands only)

**Skip this step for hooks and scripts** (they are self-contained executables without cross-references to other artifacts).

This step checks that every file path, script reference, and skill/command invocation mentioned in the artifact actually exists on disk. It is purely read-only (no execution, no writes, no external actions) and safe for `--auto` overnight runs.

**Why this matters:** Hone can improve a skill's text quality to grade A while the skill references a script that was deleted, a path that was renamed, or a command that no longer exists. Without reference validation, hone is blind to broken references, which are among the most common post-improvement regressions (especially when hone adds new script invocations or path references during Phase 2).

**Flow:**

1. **Extract references from the artifact content.** Scan for:
   - File paths: regex patterns like `~/...`, `~/.claude/...`, `$HOME/...`, `/home/...`, and relative paths in code blocks (eg `path/to/script.py`)
   - Script invocations: `python3 <path>`, `bash <path>`, `/path/to/script.sh`
   - Skill/command references: `/skill-name`, `Skill tool` invocations with specific skill names, `~/.claude/skills/<name>/`, `~/.claude/commands/<name>.md`
   - Environment variables used in paths: `$CLAUDE_CODE_CURRENT_SESSION_ID`, `${CLAUDE_PLUGIN_ROOT}` (resolve known vars, skip dynamic ones)

2. **Validate each reference (parallel Bash calls where possible):**
   ```bash
   # For file paths (expand ~ and known env vars first):
   test -e "<expanded_path>" && echo "EXISTS" || echo "MISSING: <path>"

   # For scripts with shebang expectations:
   test -x "<script_path>" && echo "EXECUTABLE" || echo "NOT_EXECUTABLE: <path>"

   # For bash scripts referenced in the artifact:
   bash -n "<script_path>" 2>&1 && echo "SYNTAX_OK" || echo "SYNTAX_ERROR: <path>"

   # For skill references:
   test -f ~/.claude/skills/<name>/SKILL.md && echo "EXISTS" || echo "MISSING_SKILL: <name>"

   # For command references:
   test -f ~/.claude/commands/<name>.md && echo "EXISTS" || echo "MISSING_COMMAND: <name>"
   ```

3. **Classify results:**
   - `MISSING` or `MISSING_SKILL` or `MISSING_COMMAND`: broken reference, recorded as a finding
   - `NOT_EXECUTABLE`: warning (script exists but isn't executable)
   - `SYNTAX_ERROR`: broken script, recorded as a finding
   - Template/variable paths (eg `{artifact_path}`, `$OUTPUT_DIR`): skip validation, these are runtime-resolved

4. **Report:** Add reference validation results to the Phase 1 → Phase 2 handoff. Broken references become `priority: "HIGH"` findings in Phase 2 (a skill that references nonexistent files is functionally broken regardless of its text quality score).

**Exclusions (do NOT validate):**
- URLs (http/https) — these are external and change independently
- Paths inside code block examples that are clearly illustrative (eg `path/to/example:target`)
- Paths with unresolvable runtime variables (eg `$OUTPUT_DIR/results.json` where `OUTPUT_DIR` is set during execution)

**Gate: Step 11 → Step 12 (checklist)**
- [ ] Reference extraction completed (at least one reference found, or artifact has no references)
- [ ] All extractable references were checked via `test -e` or equivalent
- [ ] Results classified (MISSING, NOT_EXECUTABLE, SYNTAX_ERROR, or OK)
- [ ] Broken references recorded in workflow state for Phase 2

**Handoff interface (Step 11 → Step 12, merged into eval_results):**
```
reference_validation: {
  total_references: number,           // count of references extracted
  checked: number,                    // count actually validated (excludes skipped)
  skipped: number,                    // template vars, URLs, illustrative paths
  broken: [{
    path: string,                     // the reference as written in the artifact
    expanded_path: string,            // after ~ and env var expansion
    type: "file" | "script" | "skill" | "command",
    issue: "missing" | "not_executable" | "syntax_error",
    line_context: string              // line from artifact containing the reference
  }],
  warnings: [{
    path: string,
    issue: string
  }]
}
```
Write to workflow state file. Phase 2 reads `reference_validation.broken` and injects each as a `priority: "HIGH"`, `fix_type: "reference"` finding alongside structural and content findings.

#### Script Test Coverage Report (informational, read-only)


After reference validation completes, check test coverage for all referenced `.py` scripts:

1. For each `.py` script found during reference extraction (excluding test files themselves), check if a corresponding `test_*.py` exists in the same directory:
   ```bash
   # For script at /path/to/my_script.py, check for:
   test -f "/path/to/test_my_script.py" && echo "HAS_TESTS" || echo "NO_TESTS"
   ```

2. **Report in the Phase 1 output** (informational only, NOT a finding, NOT blocking):
   ```
   Script Test Coverage: 4/6 scripts have companion tests
     ✓ structural_audit.py → test_structural_audit.py
     ✓ score_execution.py → test_score_execution.py
     ✗ my_utility.py → (no test file)
     ✗ helper.py → (no test file)
   ```

3. **Do NOT:**
   - Run any tests (that belongs to a code-review pass, not to hone)
   - Generate any tests (that belongs to a code-review pass, not to hone)
   - Block the exit gate on missing tests
   - Add test coverage to Phase 2 improvement findings
   - Execute any scripts

4. Add to `reference_validation` handoff:
   ```
   script_test_coverage: {
     total_scripts: number,
     scripts_with_tests: number,
     scripts_without_tests: [{path: string, expected_test: string}]
   }
   ```

This information is surfaced to the user in the Step 12 Report so they know which scripts lack tests and can run a code-review pass on them if desired.

### Step 12: Report

Generate report. If `--no-visualize` not set, generate HTML visualization.

**Gate: Phase 1 → Phase 2 (checklist)**
- [ ] At least one test case scored below 0.8 (improvement is warranted)
- [ ] If all scores >= 0.8: report grade, skip Phase 2, go to Final Output. No improvement needed.
- [ ] Failure triage classification is complete (all 0.00 = criteria bug, single 0.00 = variance, low semantic = real opportunity)

**Gate event (write to `gates[]` in workflow state before entering Phase 2):**
```json
{"step": "phase1_to_phase2", "judge": "self-check", "result": "pass", "ts": "<ISO timestamp>"}
```
Set `result` to `"fail"` and halt if entering Phase 2 when all scores >= 0.8 (no improvement warranted). Append to the `gates` array in the state file, not replace. Example state update:
```python
state["gates"] = state.get("gates", [])
state["gates"].append({"step": "phase1_to_phase2", "judge": "self-check", "result": "pass", "ts": "<ts>"})
```

**Handoff interface (Phase 1 → Phase 2):**
```
eval_results: {
  output_dir: string,                // path to results.json
  results_path: string,              // full path to results.json file
  composite_score: number,           // 0.0-1.0
  grade: "A" | "B" | "C" | "D" | "F",
  per_test: [{
    test_id: string,
    score: number,
    status: "pass" | "fail" | "error",
    failure_type?: "criteria_bug" | "variance" | "real_issue",
    dimension_scores: {[dimension: string]: number}
  }],
  actionable_failures: number        // count of real_issue failures
}
```
Write this to workflow state file before entering Phase 2. Phase 2 reads from the file, not from memory.

Additionally, if Step 2 (Structural Audit) produced findings, include the `structural_audit` object from the workflow state. If Step 11 (Reference Validation) produced broken references, include the `reference_validation` object. Only findings with `effective_priority: "HIGH"` drive Phase 2 improvements; LOW findings are reported but not acted on. Broken references from Step 11 are always HIGH priority (a skill referencing nonexistent files is functionally broken).
