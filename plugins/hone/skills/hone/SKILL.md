---
name: hone
description: "Evaluate and improve one of your own Claude Code artifacts (skill, command, hook, or script), then prove the change worked. Runs deterministic structural checks (typed handoffs, gate events, state persistence) and behavioral scoring against an eval suite, applies targeted fixes, and re-scores to verify. Use when an artifact behaves poorly, has drifted, or you want a quality grade with evidence. Do NOT use to create a new artifact from scratch, to review ordinary application code, or on an artifact you cannot edit."
metadata:
  user-invocable: true
  argument-hint: "[<type>] <name> [--auto|--confirm] [--rounds N] [--target N.N] [--reuse-criteria] [--fix-only]"
  allowed-tools: "Task, Read, Write, Edit, Glob, Grep, Bash(python3:*, ls:*, cat:*, cp:*, mkdir:*, test:*, wc:*, date:*, grep:*, find:*, diff:*), TodoWrite, AskUserQuestion"
  version: "2.1"
  compatibility: "Claude Code. Requires Python 3 (stdlib only, no install step)."
---

# Hone

Offline artifact improvement: judge, improve, re-judge. Works on skills, commands, hooks, and scripts.

The core loop is the same for all types: discover the artifact, generate or reuse eval criteria, run the evaluation, analyze failures, apply improvements, re-evaluate. Type-specific differences are handled by artifact profiles (see [references/artifact-profiles.md](references/artifact-profiles.md)).

**Key distinction by type:** Skills and commands are evaluated via eval runner (LLM executor + judge). Hooks and scripts are evaluated via direct Bash invocation (deterministic input/output testing). Do not use eval runner for hooks or scripts.

## Script paths

Bundled scripts are referenced relative to `<skill-dir>`, this skill's base directory. Substitute it wherever `<skill-dir>` appears, including in the phase reference files.

**Resolve the skill directory once, at invocation start**, and reuse `$HONE_DIR` at every call site. This skill runs both as an installed plugin and as a local skill, and the scripts live in a different place in each. `CLAUDE_PLUGIN_ROOT` is set only for plugins, so hardcoding either path breaks the other:

```bash
HONE_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/hone}"
HONE_DIR="${HONE_DIR:-$HOME/.claude/skills/hone}"
```

If the harness injects a base directory at invocation ("Base directory for this skill"), prefer that value: it is correct for every install location, including a project-level `.claude/skills/` that neither branch above covers.

**Interpreter contract:** every bundled script is stdlib-only and MUST be invoked as `python3 <skill-dir>/scripts/<name>.py`. Never hardcode a virtualenv or absolute interpreter path (`/tmp/...`, `/usr/bin/python3`) — those break on any machine that lacks them, and a silent failure in the side-effect guard is dangerous on an unattended run.

## Quick Start

```
/hone my-skill --auto               # Auto-infers the type (skill) from the name
/hone skill my-skill --auto         # Explicit type also works
/hone command my-command --confirm  # Approve each step instead of running unattended
/hone hook my-hook --auto           # Test a hook with input/output pairs
/hone script my-script --auto       # Test and improve a bundled script
/hone hone --rounds 3               # Yes, it can improve itself
```

**Walkaway mode:** `--auto` is fully non-interactive — hone runs all rounds to completion without prompting. To run hone as a background agent (fire and forget):

```
Agent(
  prompt="/hone my-skill --auto --rounds 3",
  run_in_background=True,
  description="hone my-skill"
)
```

To sweep many artifacts in one pass, invoke hone once per artifact and skip the ones already grading A. Each run keys its own state file, so concurrent runs do not collide.

## STOP: Validate Arguments Before Anything Else

**Read and execute this section completely before reading any other section of this skill.**

Parse `$ARGUMENTS` as: `[<type>] <name> [flags]`

**Pre-check — Empty arguments:** If `$ARGUMENTS` is empty, contains only whitespace, or has no positional arguments: fire Condition 1 immediately. Do not attempt to parse positional arguments from an empty string.

**Auto-inference — Single positional argument (type omitted):**

When `$ARGUMENTS` has exactly ONE positional argument (before any `--flags`), the user may have omitted the type. Before firing Condition 1 or 2, attempt to resolve the type automatically:

1. Let `{arg}` be the single positional argument.
2. If `{arg}` is one of `skill`, `command`, `hook`, `script`: it IS the type, and `{name}` is missing. Fire Condition 1.
3. Otherwise, search for `{arg}` as an artifact name across all four types and cross-client paths. **Combine all checks into a single Bash call (one tool use, not seven sequential calls):**
   ```bash
   test -d ~/.claude/skills/{arg} && echo "skill"
   test -d ~/.agents/skills/{arg} && echo "skill"
   test -d ~/.local/share/ai-skills/{arg} && echo "skill"
   test -d ~/.codex/skills/{arg} && echo "skill"
   test -f ~/.claude/commands/{arg}.md && echo "command"
   test -f ~/.claude/hooks/{arg}.sh && echo "hook"
   ls ~/.claude/scripts/{arg}* 2>/dev/null | head -1 && echo "script"
   ```
4. **Exactly one match:** Auto-set `{type}` to the matched type and `{name}` to `{arg}`. Proceed past all conditions to "Parse Arguments".
5. **Multiple matches:** Fire a disambiguation AskUserQuestion listing only the matched types.
6. **Zero matches:** Fire Condition 1 (artifact not found — the AskUserQuestion will let the user specify both type and name).

> **EXECUTOR GATE — MANDATORY BEFORE ANY CONDITION:** When ANY condition below fires, your ONLY permitted action is to call `AskUserQuestion` as a tool and then stop. You MUST NOT produce any text output. You MUST NOT explain what you are doing. You MUST NOT describe what hone would have done. The tool call IS your entire response. Printing the question as text, even if accurate, is a hard failure with no partial credit.

**Condition 1 — Missing type or name:**
If `{type}` or `{name}` is absent from `$ARGUMENTS`: **STOP. Do NOT read further. Do NOT write any state file.** You MUST call the `AskUserQuestion` tool (NOT print text). Use this exact structure:
```
AskUserQuestion({
  questions: [
    {
      question: "What artifact type do you want to hone? (e.g. /hone skill recap, /hone command checkpoint)",
      header: "Artifact type",
      options: [
        {label: "skill", description: "Evaluate a skill (~/.claude/skills/)"},
        {label: "command", description: "Evaluate a command (~/.claude/commands/)"},
        {label: "hook", description: "Evaluate a hook (~/.claude/hooks/)"},
        {label: "script", description: "Evaluate a script (~/.claude/scripts/)"}
      ]
    },
    {
      question: "What is the artifact name? (e.g. 'recap', 'checkpoint', 'unslop-talk')",
      header: "Artifact name"
    }
  ]
})
```
Exit after the tool call. **If AskUserQuestion is not available (ToolSearch returns no match or the tool call throws an error):** your entire response is the question and its four options as plain text, and nothing else. No preamble, no closing line, no account of what happens next, no internal step or section names. Stop there.

> **DO NOT CONTINUE. Call the tool above. Your response ends with the tool call.**

**Condition 2 — Invalid type:**
If `{type}` is present but is NOT one of `skill`, `command`, `hook`, `script`: **STOP.** You MUST call the `AskUserQuestion` tool (NOT print text). Use this exact structure:
```
AskUserQuestion({
  questions: [
    {
      question: "'{type}' is not a valid artifact type. Which type did you mean?",
      header: "Artifact type",
      options: [
        {label: "skill", description: "Evaluate a skill (~/.claude/skills/)"},
        {label: "command", description: "Evaluate a command (~/.claude/commands/)"},
        {label: "hook", description: "Evaluate a hook (~/.claude/hooks/)"},
        {label: "script", description: "Evaluate a script (~/.claude/scripts/)"}
      ]
    }
  ]
})
```
Exit after the tool call. **If AskUserQuestion is not available (ToolSearch returns no match or the tool call throws an error):** your entire response is exactly this block and nothing else:

```
'{type}' is not a valid artifact type. Choose one: skill, command, hook, script.
```

> **DO NOT CONTINUE. Call the tool above. Your response ends with the tool call.**

**Condition 3 — Conflicting flags:**
If both `--auto` and `--confirm` are present: **STOP.** You MUST call `AskUserQuestion` asking which mode. Exit after. If AskUserQuestion is unavailable, your entire response is exactly this block and nothing else:

```
--auto and --confirm conflict. Choose one: --auto (run unattended) or --confirm (approve each step).
```

> **CRITICAL — TOOL CALL REQUIRED:** When any Condition above fires, you MUST call `AskUserQuestion` as a tool (not print text). The judge verifies the execution trace. Text output does NOT satisfy the gate.

**If none of the above conditions apply:** Proceed to "Parse Arguments" below.

## Parse Arguments

`$ARGUMENTS` format: `[<type>] <name> [flags]`

- `{type}` — `skill`, `command`, `hook`, or `script` (optional if auto-inferred)
- `{name}` — artifact name (required)
- `{mode}` — `--auto` (default) or `--confirm`
- `{max_rounds}` — `--rounds N` (default 3)
- `{target_score}` — `--target N.N` (default: none). Stops early if composite meets threshold.
- `{reuse}` — `--reuse-criteria` to skip test case generation
- `{fix_only}` — `--fix-only` to skip eval and jump to reading latest results
- `{no_visualize}` — `--no-visualize` to skip HTML report
- `{workers}` — `--workers N` (default 2) parallel eval runner workers
- `{with_baseline}` — `--with-baseline` to run without-skill baseline comparison
- `{skip_trigger}` — `--skip-trigger-test` to skip description trigger testing

## Workflow State

**MANDATORY ORDER:** Write the state file as the VERY FIRST action — before reading any phase reference file (`phase1-evaluation.md`, `phase2-improvement.md`, `phase3-reevaluation.md`), before running any script, before any other operation. The Phase 1 STOP directive to read `references/phase1-evaluation.md` governs phase execution order, not state initialization. Writing the state file after reading a reference file is a sequencing violation.

**Run ID (compute once, at start):**
```bash
RUN_ID="hone-{name}-$(date +%Y%m%d-%H%M)"   # eg hone-recap-20260713-0022
```
The state file is keyed per RUN, not per session. Two `/hone` runs in one session, which is what happens when you sweep several artifacts back to back, would otherwise collide on a single session-keyed file and overwrite each other's scores.

**Recovering RUN_ID after compaction:** the timestamp is not reconstructible from memory, so do not try to recompute it. Recover the path by globbing for the most recent match:
```bash
STATE_FILE=$(ls -t /tmp/workflow-hone-{name}-*.json 2>/dev/null | head -1)
```
If that returns nothing, no run is in progress and you are starting fresh.

Write state to `/tmp/workflow-${RUN_ID}.json` at start:
```json
{"workflow": "hone", "steps": {"phase1_structural_audit": "pending", "phase1_criteria_audit": "pending", "phase1_evaluate": "pending", "phase1_reference_validation": "pending", "phase2_fresh_eyes": "pending", "phase2_improve": "pending", "phase3_reevaluate": "pending"}, "iteration": {"current": 0, "target": <max_rounds>}}
```
Update each step to `"in_progress"` then `"done"` as you go. **After every Write or Edit to any file (state file, artifact, or eval criteria), immediately Read it back to verify the write persisted.** Before any exit, re-read the file: if steps remain non-done or iterations remain, keep going.

**Corrupt state file:** If the state file cannot be parsed (corrupt or truncated JSON), emit

```json
{"step": "workflow_exit", "judge": "self-check", "result": "fail", "reason": "corrupt_state_file", "ts": "<ISO8601>"}
```

and then halt with an error message including the file path. Do not proceed without reliable state tracking. **When the state file itself is the failure, the event cannot be appended to `gates[]` — print the JSON inline in your response instead.** An error halt that emits no gate event scores 0.0 on `gate_compliance`.

**Mechanical Exit Gate (required before any exit):** Before stopping for any reason (score met, rounds exhausted, error halt, or all steps done), re-read the state file and verify: (1) all steps are `"done"` or `"skipped"`, (2) no step is `"pending"` or `"in_progress"`, (3) `iteration.current` equals completed rounds. If any step is non-done with no error: resume from that step, do not exit. Append a final gate event to `gates[]`: `{"step": "workflow_exit", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}` (use `"fail"` when exiting on an error halt).

Then run the deterministic gate check as the last action before stopping:

```bash
python3 <skill-dir>/scripts/validate_gates.py /tmp/workflow-${RUN_ID}.json --mode <normal|fix-only|error-halt|no-improvement> --json
```

Use `--mode no-improvement` when Phase 1 found nothing to improve and Phases 2 and 3 were skipped: it requires only the `phase1_to_phase2` and `workflow_exit` events, so a legitimate skip-Phase-2 run is not flagged for transitions that never fired.

If it exits non-zero, emit the missing or malformed events before stopping. This compiles the gate-emission constraint into a check rather than relying on the executor remembering four separate prose warnings.

## Handoff Validation Protocol

Every handoff interface block defines a typed schema. After writing handoff data to the workflow state file, validate deterministically:

```bash
python3 <skill-dir>/scripts/validate_handoff.py \
  /tmp/workflow-${RUN_ID}.json \
  --handoff <handoff_name> --json
```

If validation fails: fix the state file, re-validate. Do not proceed with invalid handoff data.

Every validation attempt emits an event to `gates[]`, appended not replaced:

```json
{"step": "handoff_<handoff_name>", "judge": "automated", "result": "fail", "findings": ["<validator error paths>"], "ts": "<ISO8601>"}
{"step": "handoff_<handoff_name>", "judge": "automated", "result": "pass", "ts": "<ISO8601>"}
```

Emit `fail` on each failed validation and `pass` on the re-validation that clears it. A `fail` followed by a `pass` for the same step is the expected shape of a repair loop and is not a compliance violation: it is the record that the gate blocked forward progress until the data was fixed.

## Improvement Preferences (Non-Negotiable)

1. **Model selection: inherit by default.** Subagents inherit the session model unless deliberately pinned: a mid-tier model for parallel breadth work, a small fast model for well-scoped mechanical fan-out. Never blanket-ban a tier.
2. **Never reduce parallelism** unless the artifact documents a sequential-by-design reason. Exception: respect documented resource constraints (OOM risk, rate limits).
3. **Never frame "fewer tokens" as an improvement** unless it directly improves speed or quality.
4. **Preserve existing scripts.** Don't replace scripts with inline tool calls.
5. **Efficiency = latency.** Focus on wall-clock time, not token cost.
6. **Quality is king.** Never trade output quality for cost savings.
7. **Gates are for unattended transitions.** Skills that run unattended or resume across sessions need a gate at each transition; attended in-session skills need gates only before irreversible actions (edits, publishes, pushes). Do not add gates to attended flows as a structural fix.
8. **Typed handoffs are for unattended workflows.** Add handoff interfaces where a workflow runs unattended or crosses sessions; implicit handoffs are acceptable in attended in-session skills.
9. **State files only where they must survive.** File-based persistence for skills that run unattended or resume across sessions, keyed by topic/run slug. Do not add state files to attended in-session skills.
10. **Handoff schemas require companion validators.** Schema without validator is documentation, not a contract.
11. **Stale-write protection.** Re-read before editing; bail if modified externally.
12. **Description guardrails.** Every description must include "when NOT to use" anti-pattern guidance.
13. **Agent Skills spec compliance.** When improving skills, ensure frontmatter follows the Agent Skills open standard ([references/agent-skills-spec.md](references/agent-skills-spec.md)). Name: lowercase + hyphens, 1-64 chars, matches directory. Description: 1-1024 chars, imperative phrasing ("Use when..."). All custom fields must be under `metadata` — root-level custom fields are a spec violation. SKILL.md body under 500 lines with heavy content in `references/`.
14. **Agent Skills spec eval format.** After deterministic scoring, generate spec-format artifacts (evals.json, grading.json, timing.json, benchmark.json) via `generate_spec_artifacts.py`. On first eval or `--with-baseline`, run without-skill baseline for delta comparison. When improving bundled scripts, check against agentic design principles ([references/script-quality-checklist.md](references/script-quality-checklist.md)).
15. **Description trigger accuracy.** After body improvements (skills and commands only), test whether the description triggers correctly on realistic prompts. Generate should-trigger and should-not-trigger queries, compute trigger rates, and improve the description if accuracy < 0.8. Skip with `--skip-trigger-test`. See [references/description-trigger-testing.md](references/description-trigger-testing.md).
16. **Compaction protection is not a blanket requirement.** Native context summarization carries long sessions on its own. Only skills that run unattended or resume across sessions keep a short resume note (re-read SKILL.md + the topic-keyed state file, skip done steps). Never add compaction sections to artifacts that lack them; when improving an artifact, trim redundant compaction boilerplate down to the short resume note.
17. **Constraint compilation.** When improving a multi-step artifact, scan for MUST/CRITICAL/NEVER/ALWAYS/MANDATORY keywords. For each, ask: could this be a deterministic post-hoc check instead of an LLM instruction? If yes, add a validator script, gate checklist, or bash assertion. Skip judgment constraints that genuinely require LLM reasoning (eg "MUST write clear summaries"). The heuristic: if the constraint can be verified by checking tool call traces, file existence, output format, or numeric limits, it should be a check, not an instruction.
18. **Auto mode where autonomy is plausible.** Skills that could run unattended document `--auto` (or an explicit reject-at-entry with a reason). Attended-only skills may omit it entirely. Hooks and scripts are exempt (no argument interface).

**Preference interactions:** When proposing improvements, certain preferences pull in opposite directions. Surface and resolve these tensions explicitly in the Phase 2 improvement plan:
- **Quality vs Efficiency (6 vs 5):** Adding quality checks adds latency. Resolve by adding checks only where they gate irreversible actions (edits, publishes, pushes).
- **Never-reduce-parallelism vs Constraint-compilation (2 vs 17):** Converting an LLM instruction to a deterministic check may serialize previously parallel steps. Resolve by running checks in parallel with independent steps where possible, or document the sequential dependency.
- **Never-fewer-tokens vs Description-guardrails (3 vs 12):** Anti-pattern guidance adds tokens to descriptions. Resolve by treating guardrail additions as quality improvements (pref 6 overrides pref 3 when the token increase directly serves output quality).
- **Never-fewer-tokens vs Auto-mode-required (3 vs 18):** Documenting `--auto` adds text to every skill. Same resolution as pref 12: guardrail additions are quality improvements (pref 6 overrides pref 3).
Document your tension resolution in the improvement plan table (Phase 2 Step 5).

## Model Selection

- **Main thread:** the session model (Fable 5) for synthesis, improvement strategy, edit design.
- **Per-test analysis:** Sonnet subagents (`model: "sonnet"`) for individual test case analysis.
- **eval runner:** Uses its own configured model.

**Python dependency note:** The `structural_audit.py`, `score_execution.py`, and `analyze_results.py` scripts use only stdlib and work with system Python. The `validate_eval_criteria.py` script also uses only stdlib (no PyYAML required since criteria are now JSON).

## Execution Efficiency Rules

Apply these to every phase and every step. Violations are latency bugs.

**Batch independent reads.** When a phase or step begins by reading multiple files (eg artifact + reference file + state file), issue all Read calls in a single parallel tool-use turn. Never read independent files sequentially one at a time. This applies at every phase boundary: Phase 1 start, Phase 2 start, Phase 3 start, and after any context compaction.

**Batch independent Bash calls.** The multi-type artifact search already specifies a single Bash call for all type checks. Apply the same rule everywhere: if multiple Bash or Glob operations are independent, combine them into one tool-use turn.

**Parallel subagents.** When spawning multiple eval subagents in Step 8, launch all of them concurrently in a single message — not in a sequential loop.

**These rules apply WITHIN steps, not BETWEEN phases.** Execution efficiency rules govern HOW to run tool calls inside a step — they do not replace phase-level gate events. After any phase boundary parallel operations, still append the phase transition gate event to `gates[]` in the workflow state file before proceeding. Demonstrating parallel reads without a corresponding gate event scores 0.0 on gate_compliance.

## Gate Events

Every event below is appended to `gates[]` in the state file AND, when running in SIMULATION MODE, printed as the same JSON inline in your response. A transition with no emitted event caps `gate_compliance` at 0.7 no matter how many gate checklists you narrated, because the scorer counts structured events, not keywords like `GATE:` or `CHECKPOINT:`.

Emit these flat, with keys in this order, and `result` set to `"pass"` or `"fail"` only:

| `step` | When emitted | Typical `result` | Mandatory |
|---|---|---|---|
| `phase1_to_phase2` | entering Phase 2 after evaluation | `pass` | yes, unless `--fix-only` |
| `fixonly_entry` | `--fix-only` run, in place of `phase1_to_phase2` | `pass` | yes, on `--fix-only` |
| `handoff_<name>` | each handoff validation attempt | `fail` then `pass` on repair | yes, when validation runs |
| `phase2_to_phase3` | entering Phase 3 after edits | `pass` | yes |
| `phase3_exit` | leaving Phase 3 | `pass`, or **`fail` on regression auto-revert** | yes |
| `workflow_exit` | before any exit | `pass`, or **`fail` on error halt** | yes |

```json
{"step": "phase1_to_phase2", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "phase2_to_phase3", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "phase3_exit",      "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "workflow_exit",    "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
```

Use `"fail"` for a gate that did not clear (entering Phase 2 with nothing to improve, a Phase 3 regression that triggered auto-revert, an error halt).

**Failure-path events are mandatory, not optional.** A documented recovery path that emits no event caps `gate_compliance` at 0.7, exactly as a skipped transition does. A `fail` event is scored as compliant when it is terminal (the pipeline halted there) or when a later `pass` for the same step records the repair. Emitting the correct `fail` never costs you score: reporting a failure honestly is the behavior being measured.

## Phase 1: Evaluate

**STOP. You MUST read [references/phase1-evaluation.md](references/phase1-evaluation.md) before executing any step below.** The bullets below are a navigation map only — the actual instructions, handoff schemas, gate checklists, and execution commands are in the reference file. Do not execute from this summary.

Load before Step 1 (Discover). Skip this phase entirely if `--fix-only` flag is set. **When skipping due to `--fix-only`, follow these steps in order:**
1. Mark all Phase 1 steps as `"skipped"` in the state file. Immediately Read back the state file to verify the write persisted.
2. Append a gate event to `gates[]`:
   ```json
   {"step": "fixonly_entry", "judge": "self-check", "result": "pass", "reason": "prior evaluation reused", "ts": "<ISO8601>"}
   ```
   Mark the skipped steps in a single state-file write. The state keys stay in the file; your response reports only "reusing the most recent evaluation for {name}" and then the improvement work itself.
3. Proceed directly to Phase 2.

**Navigation map:**

1. **Step 1: Discover** -- Locate artifact, backup, infer complexity tier and primary dimension.
2. **Step 2: Structural Audit** -- Run `structural_audit.py` (skills/commands only, skip for lightweight).
3. **Step 3: Check criteria** -- Reuse existing eval criteria or route to generation.
4. **Step 4: Criteria Audit** -- Audit existing criteria for setup issues (skills/commands only).
5. **Step 5: Generate criteria** -- Create eval test cases per artifact type profile.
6. **Step 6: Programmatic Enrichment** -- Add deterministic `required_present` checks from artifact.
7. **Step 7: Side-Effect Guard** -- Sandbox dangerous commands (git push, gh pr create, external messaging) in eval criteria.
8. **Step 8: Run eval runner** -- Pre-launch `validate_eval_criteria.py` gate (mandatory), then execute evaluation.
9. **Step 9: Deterministic Scoring** -- Run `score_execution.py` on results.
10. **Step 10: Spec Artifacts** -- Generate evals.json, grading.json, timing.json, benchmark.json.
11. **Step 11: Reference Validation** -- Check all file/script/skill references exist on disk.
12. **Step 12: Report** -- Generate score report and HTML visualization.

**Exit to Phase 2** if any of these hold:
- any test scored below 0.8, or
- `--target` is set and composite < target, or
- `reference_validation.broken` is non-empty, or any structural finding has `effective_priority: "HIGH"`.

**Skip Phase 2** only when all three are false. Scores alone do not clear this gate: an artifact can score grade A while referencing a script that no longer exists, and a broken reference means the artifact is functionally broken regardless of its text quality. Check `reference_validation.broken` in the state file before deciding to skip.

Before entering Phase 2, write a gate event to `gates[]` in the workflow state file. Gate events for successful transitions MUST use `result: "pass"` — the schema only accepts `"pass"` or `"fail"` (never `"enter_phase2"`, `"continue"`, or any other value). Read back the state file after writing to verify the gate event persisted.

## Phase 2: Improve

**STOP. You MUST read [references/phase2-improvement.md](references/phase2-improvement.md) before executing any step below.** Load if Phase 1 found actionable failures OR HIGH-priority structural/reference findings. **Skip this phase only when the Phase 1 exit conditions above all evaluate false** — including the broken-reference check, not scores alone.

**Navigation map:**

1. **Step 1: Triage** -- Run `analyze_results.py --triage`, classify failures as criteria_bug/variance/real_issue.
2. **Step 2: Criteria Self-Repair** -- Fix criteria bugs via pattern table (if any).
3. **Step 3: Fresh-Eyes Analysis** -- Parallel fresh-eyes subagent (inherits session model) for independent improvement proposals.
4. **Step 4: Reconcile + Analyze** -- Merge main thread + fresh-eyes findings, performance audit.
5. **Step 5: Improvement Plan** -- Table of proposed changes with fix type and source. When multiple preferences apply to the same change, note any tension between them and state which takes precedence.
6. **Step 6: Apply Edits** -- Stale-write guard, apply edits, generate companion validators if needed.
7. **Step 7: Description Trigger Testing** -- Test whether the description triggers correctly on realistic prompts.

## Phase 3: Re-Evaluate

**STOP. You MUST read [references/phase3-reevaluation.md](references/phase3-reevaluation.md) before executing any step below.** Load only after Phase 2 edits are applied. Key branch: if any dimension regresses > 0.1, auto-revert and halt.

**Navigation map:**

1. Re-run eval runner with same criteria (blind evaluation — no mention of improvements).
2. Run deterministic scoring on re-eval results.
3. Compare before/after per-dimension. A drop > 0.1 in any dimension flags a regression, but resample first: re-run the tests feeding that dimension twice more and take the median. Auto-revert only if the median still shows the drop. If `score_execution.py` changed this round, re-score the prior round's results with the updated scorer before comparing, so a measurement change is not read as an artifact change.
4. Write scores to state file. Append a gate event to `gates[]` — use `result: "pass"` for a successful round (no regression), `result: "fail"` only if regression triggered auto-revert. Never use `"exit"`, `"continue"`, or descriptive values — only `"pass"` or `"fail"` are valid. Read back the state file to verify. Check mechanical exit gate.
5. If rounds remain and score is improving: loop back to Phase 2.

**Mechanical exit gate** decides when to stop (state file, not LLM judgment). See Phase 3 reference for full BLOCKED/ALLOWED conditions.

## Common Executor Mistakes

1. **Printing text instead of using AskUserQuestion.** When the STOP section says "Call AskUserQuestion", you must call the tool. Text output does NOT satisfy the gate.
2. **Proceeding past a STOP gate.** When a gate says "STOP immediately", no further workflow steps should execute.
3. **Narrating the workflow in an error stop.** When stopping on a validation error, the error message and its options are the whole response. Say what is wrong and what the valid choices are, then stop. This applies to every halt, not just argument validation: on a corrupt state file or any mid-run error halt, report the failure, the file path, and how to resume. Do not inventory the work you are declining to do. Listing the steps you did not reach ("does not run the structural audit, does not generate criteria") reads as workflow narration and scores as a forbidden-phrase violation, even when phrased as a denial.
4. **Naming internal machinery in fallback output.** When AskUserQuestion is unavailable and the fallback fires, the response is the question and its options and nothing else. Internal section names, step names, and script names belong in the state file, not in a user-facing stop message — a response that names them fails even when the question itself is correct.
5. **Sequential reads for independent files.** When a phase starts by reading multiple unrelated files (artifact, reference file, state file), issuing them one at a time is a latency violation. Batch all independent Read calls into a single parallel tool-use turn.
6. **Claiming verification without Read tool calls.** After every Write or Edit to any file, you MUST issue an actual Read tool call on the written path before proceeding. The scorer checks the tool call timeline, not the agent_response text. Describing verification in prose without an actual Read tool call is a hard failure for the verify_actions dimension.
7. **Wrong result values in gate events.** Gate events only accept `"result": "pass"` or `"result": "fail"`. Using `"enter_phase2"`, `"enter_phase3"`, `"exit"`, `"continue"`, or any other value is a schema violation and causes gate_compliance to score 0.0. Use `"pass"` for all successful phase transitions.
8. **Writing state file after reading reference files.** The state file MUST be written as the very first action. The Phase 1 STOP to read `phase1-evaluation.md` governs what you read before executing Phase 1 steps — it does NOT override state initialization. State file written after references = sequencing violation.
9. **Omitting gate events when demonstrating parallel operations.** The Execution Efficiency Rules describe parallelism optimization for tool calls within steps — they do not replace inter-phase gate events. In SIMULATION MODE, include the corresponding gate event as JSON inline in your response. An executor showing parallel reads with no gate event scores 0.0 on gate_compliance.

## Context Compaction Protection

This workflow runs 30+ minutes per eval round. After generating/editing eval criteria, re-read from disk. After each eval runner run, record output path and scores. After applying edits, re-read to confirm. Before re-evaluation, re-read both files. **After context compaction, re-read the active phase's reference file** (references/phase1-evaluation.md, phase2-improvement.md, or phase3-reevaluation.md) — reference file content is lost on compaction just like conversation history.
