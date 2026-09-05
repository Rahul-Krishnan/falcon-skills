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
   test -f "$HONE_DIR/../{arg}/SKILL.md" && echo "skill"
   ls -d ~/.claude/plugins/*/skills/{arg} ~/.claude/plugins/*/*/skills/{arg} 2>/dev/null | head -1 | grep -q . && echo "skill"
   test -f ~/.claude/commands/{arg}.md && echo "command"
   test -f ~/.claude/hooks/{arg}.sh && echo "hook"
   ls ~/.claude/scripts/{arg}* 2>/dev/null | head -1 && echo "script"
   ```
   The `$HONE_DIR/../{arg}` check covers skills shipped in the same plugin as
   hone (including hone itself under a marketplace install, where none of the
   local-skill paths exist); the `~/.claude/plugins` globs cover skills from
   any other installed plugin.
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
- `{workers}` — `--workers N` (default 6) parallel eval runner workers. A 12-case suite at 2 workers runs as 6 sequential waves, and wall clock is set by the slowest case in each wave rather than by total work. Lower it only for a documented resource constraint (rate limit, memory), never to economize on tokens.
- `{with_baseline}` — `--with-baseline` to run without-skill baseline comparison
- `{skip_trigger}` — `--skip-trigger-test` to skip description trigger testing

## Workflow State

**MANDATORY ORDER:** Write the state file as the VERY FIRST action — before reading any phase reference file (`phase1-evaluation.md`, `phase2-improvement.md`, `phase3-reevaluation.md`), before running any script, before any other operation. The Phase 1 STOP directive to read `references/phase1-evaluation.md` governs phase execution order, not state initialization. Writing the state file after reading a reference file is a sequencing violation.

**Run ID (compute once, at start):**
```bash
RUN_ID="hone-{name}-$(date +%Y%m%d-%H%M)"   # eg hone-recap-20260713-0022
```
The state file is keyed per RUN, not per session. Two `/hone` runs in one session, which is what happens when you sweep several artifacts back to back, would otherwise collide on a single session-keyed file and overwrite each other's scores.

**Resume protocol (after compaction, or across sessions).** If the glob below returns a path, you are resuming. Re-read this SKILL.md and the active phase's reference file, resume at the first step not marked `done`, then record the resumption in two places:

1. Set `"resumed": true` at the top level of the state file.
2. Emit the `resume` gate event.

Do **not** re-emit events already on disk; they survived, the resume is what did not. A resume that records nothing is indistinguishable from a skipped one.

Both records matter. The `resumed` field is what makes the `resume` event *required*: the exit gate below runs `validate_gates.py` with no flags, and it reads that field to decide whether to demand the event. Setting the field and omitting the event fails the exit gate, which is the point. Do not run the gate check here — mid-run, steps are still `pending`, the derived mode is `error-halt`, and its only required event is the `workflow_exit` you have not reached yet, so it reports a failure every time. Validate at the exit gate, once, like every other run.

**Resuming a state file written by an older version of this skill.** `eval_results.output_dir` and `eval_results.power_verdict` are both required by the pre-Phase-2 gate, and both arrived after some state files were already on disk. A run resumed across that boundary fails `validate_handoff.py --all` with `required field missing` on one or the other. That is the gate working; migrate the record rather than deleting fields from the schema:

- **`output_dir` missing** — set it to the directory holding that round's `results.json` and `deterministic_scores.json`. It is a **directory**, not the path to `results.json`; the older meaning was the file. An old value pointing at the file is reported as `expected a directory, got a path to a file`, because Phase 3 step 3a appends to it (`$PRIOR_OUTPUT_DIR/deterministic_scores.json`) and a file path there resolves to nothing. Keep the file path in `results_path`.
- **`power_verdict` missing** — re-run the Step 9a comparison and record its top-level `verdict`. The script's one positional argument is the **eval criteria file**, never a directory (a directory there exits 2); the rounds are passed as files:
  ```bash
  python3 <skill-dir>/scripts/check_eval_power.py {eval_criteria_path} --artifact-type {artifact_type} \
    --before $PRIOR_OUTPUT_DIR/deterministic_scores.json \
    --after $ROUND_OUTPUT_DIR/deterministic_scores.json
  ```
  A round with no earlier round to compare against runs the Step 6b sizing half alone (same command without `--before`/`--after`) and records that verdict (`powered` or `underpowered`) instead.

`validate_handoff.py` prints both remedies with the failure, so the state file is the only thing you need in hand.

**Recovering RUN_ID after compaction:** the timestamp is not reconstructible from memory, so do not try to recompute it. Recover the path by globbing for the most recent match:
```bash
STATE_FILE=$(ls -t /tmp/workflow-hone-{name}-*.json 2>/dev/null | head -1)
```
If that returns nothing, no run is in progress and you are starting fresh.

Write state to `/tmp/workflow-${RUN_ID}.json` at start:
```json
{"workflow": "hone", "steps": {"phase1_structural_audit": "pending", "phase1_criteria_audit": "pending", "phase1_evaluate": "pending", "phase1_spec_artifacts": "pending", "phase1_reference_validation": "pending", "phase2_fresh_eyes": "pending", "phase2_trigger_test": "pending", "phase2_improve": "pending", "phase3_reevaluate": "pending"}, "iteration": {"current": 0, "target": <max_rounds>}}
```
Every key in this template maps to a step contract in `scripts/validate_handoff.py` `STEP_CONTRACTS` (including `phase1_spec_artifacts`, Phase 1 Step 10, and `phase2_trigger_test`, Phase 2 Step 7); seed all of them — the Mechanical Exit Gate iterates only keys present in `steps`, so an unseeded step can silently never run. Update each step to `"in_progress"` then `"done"` as you go. Before any exit, re-read the file: if steps remain non-done or iterations remain, keep going.

**Corrupt state file:** If the state file cannot be parsed (corrupt or truncated JSON), emit

```json
{"step": "workflow_exit", "judge": "self-check", "result": "fail", "reason": "corrupt_state_file", "ts": "<ISO8601>"}
```

and then halt with an error message including the file path. Do not proceed without reliable state tracking. **When the state file itself is the failure, the event cannot be appended to `gates[]` — print the JSON inline in your response instead.** An error halt that emits no gate event scores 0.0 on `gate_compliance`.

**Mechanical Exit Gate (required before any exit):** Before stopping for any reason (score met, rounds exhausted, error halt, or all steps done), re-read the state file and verify: (1) all steps are `"done"` or `"skipped"`, (2) no step is `"pending"` or `"in_progress"`, (3) `iteration.current` equals completed rounds. If any step is non-done with no error: resume from that step, do not exit. Append a final gate event to `gates[]`: `{"step": "workflow_exit", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}` (use `"fail"` when exiting on an error halt).

Then run the deterministic gate check as the last action before stopping:

```bash
python3 <skill-dir>/scripts/validate_gates.py /tmp/workflow-${RUN_ID}.json --json
```

The expected event set is derived from the state file's `steps{}` map via the run-shape table in `scripts/hone_common.py` (`derive_gate_mode`): normal, fix-only, no-improvement (Phase 1 found nothing to improve, so it requires only `phase1_to_phase2` and `workflow_exit`), or error-halt when non-done, non-skipped steps remain. Do not pass `--mode` in normal operation — it exists only as an explicit override and draws a warning when it contradicts the derived shape.

If it exits non-zero, emit the missing or malformed events before stopping. This compiles the gate-emission constraint into a check rather than relying on the executor remembering four separate prose warnings.

## Handoff Validation Protocol

Every handoff interface block defines a typed schema. After writing handoff data to the workflow state file, validate deterministically:

```bash
python3 <skill-dir>/scripts/validate_handoff.py \
  /tmp/workflow-${RUN_ID}.json \
  --handoff <handoff_name> --json
```

If validation fails: fix the state file, re-validate. Do not proceed with invalid handoff data.

Both `--step` and `--all` consult the run-shape table in `scripts/hone_common.py` (`RUN_SHAPE_ACTIVE_STEPS` / `derive_run_shape`): a handoff is required exactly when its producing step is active in the derived run shape (normal, fix-only, no-improvement) and actually ran. Run shapes that legitimately skip producers therefore validate cleanly without fabricated handoff blocks — on `--fix-only` this covers done steps too (`--step phase2_improve` does not demand `eval_results`, and `--all` at Phase 2 entry passes with zero handoff blocks). The contract is stated once, in that table; do not restate it here.

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

19. **Eval power, not just eval score.** A score is only actionable if the criteria set could have produced a verdict. Before scoring, run `check_eval_power.py` on the criteria: fewer than 5 distinct *deterministically scorable* test cases means the run is `underpowered`, which is neither a pass nor a regression. It is a warning the run carries, not a halt — hone generates suites smaller than that floor by design on the lightweight tier, so a blocking floor would only have bought padding — and what the warning buys is that the round cannot be promoted or auto-reverted on. Cases whose profile can never produce a composite (`knowledge_extraction`) do not count toward the floor, because they never reach the comparison either -- on skills and commands, which is what `--artifact-type` scopes: the hook and script scoring paths score the same dimensions whatever the profile (the profile only selects which dimension's floor caps the composite there), so a `knowledge_extraction` case produces a composite, pairs, and counts. When comparing rounds, the same script runs an exact one-sided sign test over discordant (non-tied) cases; ties hold the discordant count down rather than being discarded. Median-of-three resampling (Phase 3) controls variance and cannot substitute for power.

    **Difficulty calibration.** A case that every model tier passes, and one that every tier fails, both carry zero ranking signal. Run the suite against a cheap and an expensive tier: if both agree on a case, it is not discriminating and its cost buys nothing. A suite saturating near 1.0 is not evidence of a good artifact, it is evidence the suite stopped measuring. Replace saturated cases with ones drawn from observed failures rather than adding more of what already passes.
20. **Criteria measure outcomes, not recitation.** Criteria derived from the artifact test whether the artifact was recited, so an artifact that grows a section and a matching check scores better every round while behaving identically. Run `check_overfit.py` after criteria generation or enrichment: it classifies each scored item as `outcome`, `technique`, or `vocabulary` and fails over the ratio threshold. Rewrite flagged items to describe the result the user needed, never the procedure or the artifact's wording; each flagged entry names the `case_id` and `location` it came from. `required_absent` lists are exempt by construction (they assert the vocabulary must NOT appear).
21. **Edits stay inside the artifact.** Preference 11 stops hone clobbering someone else's change; it does not stop hone changing a file nobody asked it to touch. Snapshot before Phase 2 edits and verify after, with `check_scope.py`, pointed at `{edit_path}` (the tree that gets written) rather than `{artifact_path}` (the tree that gets read). Git diff alone is insufficient in both directions: an untracked file produces no diff, and a file already dirty before the run shows one it did not cause. The snapshot answers whether a file changed; it cannot answer who changed it, since a second session and the user's own editor leave the same marks. **Attribution comes from the run's declaration of what it wrote** (`applied_edits.edited_paths`, handed to `--verify`): a declared out-of-scope change is this run's and may be undone, an undeclared one is somebody else's and is reported and never reverted. A violation halts the round, and so does a verify that could not answer -- `clean` means the tree was read, the run said what it wrote, and nothing outside scope came back, never that nothing could be seen.
22. **Retire constraints, not just add them.** Preference 17 only ever compiles constraints in, so artifacts grow monotonically and nothing removes a rule that stopped earning its context. When a constraint looks like dead weight, ablate it: remove it, re-run the existing criteria, and keep it only if a test regresses. An ablation that changes no score is evidence the constraint was inert, and removing it is an improvement under preference 6 (the artifact gets cheaper to follow at identical quality).

    **Start with scaffolding written against an older model.** Verification nudges ("include a final verification step", "use a subagent to verify"), retry ladders, and elaborate task decomposition were written for models that under-verified. Current models over-verify when told to, to the point of stalling in self-verification loops, so these instructions now cost accuracy as well as tokens. Ablate them first, one at a time, and re-run. Deterministic checks are not in this category: a script that reads a file back is a gate, not a nudge, and stays.

**Preference interactions:** When proposing improvements, certain preferences pull in opposite directions. Surface and resolve these tensions explicitly in the Phase 2 improvement plan:
- **Efficiency vs Eval power (5 vs 19):** Raising the case count to clear the power floor costs eval wall-clock. Resolve in favor of 19: a fast verdict that cannot distinguish improvement from noise is not a cheaper answer, it is no answer. (The Quality vs Efficiency bullet below is about adding checks, not cases, and does not apply here.)
- **Constraint-compilation vs Constraint-ablation (17 vs 22):** These pull in opposite directions by design. Resolve by direction of evidence: compile a constraint in when a failure was observed, ablate one out when removing it moves no test. Never ablate a constraint that guards an irreversible action, regardless of score.
- **Programmatic enrichment vs Outcome criteria (Phase 1 Step 6 vs 20):** Enrichment derives checks from the artifact, which is exactly what 20 flags, and every anchor it adds is a `required_present` entry (it never writes `required_absent`), so every one of them is a `vocabulary` lift by construction. Resolve by running `check_overfit.py` after every enrichment pass, Phase 1 Step 6a and again after the Phase 3 refresh, and rewriting flagged anchors until the verdict is `within_threshold`. The refresh re-derives anchors from the current artifact, so an anchor removed in Step 6a can come back; the Phase 3 gate is what catches it.
- **Quality vs Efficiency (6 vs 5):** Adding quality checks adds latency. Resolve by adding checks only where they gate irreversible actions (edits, publishes, pushes).
- **Never-reduce-parallelism vs Constraint-compilation (2 vs 17):** Converting an LLM instruction to a deterministic check may serialize previously parallel steps. Resolve by running checks in parallel with independent steps where possible, or document the sequential dependency.
- **Never-fewer-tokens vs Description-guardrails (3 vs 12):** Anti-pattern guidance adds tokens to descriptions. Resolve by treating guardrail additions as quality improvements (pref 6 overrides pref 3 when the token increase directly serves output quality).
- **Never-fewer-tokens vs Auto-mode-required (3 vs 18):** Documenting `--auto` adds text to every skill. Same resolution as pref 12: guardrail additions are quality improvements (pref 6 overrides pref 3).
Document your tension resolution in the improvement plan table (Phase 2 Step 5).

## Model Selection

Name tiers, never specific models. Model names go stale within a release or two, and a hardcoded name silently pins this skill to whatever was current when the line was written.

- **Main thread:** the session model, inherited. Synthesis, improvement strategy, and edit design are the highest-judgment work in the pipeline, so this is the one place not to economize.
- **Subagents:** inherit unless there is a reason to pin. Reach for **reasoning effort before model choice**: every current model exposes a graded effort ladder, and dropping effort on a mechanical stage is cheaper and less disruptive than swapping tiers. Manual thinking-token budgets have been removed from current models, so effort is the knob that still exists.
- **Fan-out stages** (per-test analysis, per-finding verification) are the candidates for a lower tier or lower effort. **Fresh-eyes analysis is not**: its whole value is independent judgment quality.
- **eval runner:** uses its own configured model.

Effort tiers and their token cost are worth measuring on your own suite rather than assumed. Escalating effort on uncertainty buys accuracy at *extra* token cost rather than saving any, so escalate deliberately.

**Python dependency note:** Every bundled script is stdlib-only and works with system Python, including `structural_audit.py`, `score_execution.py`, `analyze_results.py`, `validate_eval_criteria.py` (no PyYAML required since criteria are JSON), and the four preference-19-to-22 checks: `check_eval_power.py`, `check_overfit.py`, `check_scope.py`, `check_convergence.py`. Each has a `test_*.py` beside it; run `python3 -m unittest discover -s <skill-dir>/scripts` after changing any of them.

## Execution Efficiency Rules

Apply these to every phase and every step. Violations are latency bugs.

**Batch independent reads.** When a phase or step begins by reading multiple files (eg artifact + reference file + state file), issue all Read calls in a single parallel tool-use turn. Never read independent files sequentially one at a time. This applies at every phase boundary: Phase 1 start, Phase 2 start, Phase 3 start, and after any context compaction.

**Batch independent Bash calls.** The multi-type artifact search already specifies a single Bash call for all type checks. Apply the same rule everywhere: if multiple Bash or Glob operations are independent, combine them into one tool-use turn.

**Parallel subagents.** When spawning multiple eval subagents in Step 8, launch all of them concurrently in a single message — not in a sequential loop. Say the width you want explicitly; models do not fan out wide on their own and default to a couple of calls per turn unless asked. **Turn count, not token count, is what drives wall clock**, so collapsing fifteen sequential turns into five wide ones is the single largest latency win available here.

**These rules apply WITHIN steps, not BETWEEN phases.** Execution efficiency rules govern HOW to run tool calls inside a step — they do not replace phase-level gate events. After any phase boundary parallel operations, still append the phase transition gate event to `gates[]` in the workflow state file before proceeding. Demonstrating parallel reads without a corresponding gate event scores 0.0 on gate_compliance.

## Gate Events

Every event below is appended to `gates[]` in the state file AND, when running in SIMULATION MODE, printed as the same JSON inline in your response. A transition with no emitted event caps `gate_compliance` at 0.7 no matter how many gate checklists you narrated, because the scorer counts structured events, not keywords like `GATE:` or `CHECKPOINT:`.

Emit these flat, with keys in this order, and `result` set to `"pass"` or `"fail"` only:

| `step` | When emitted | Typical `result` | Mandatory |
|---|---|---|---|
| `resume` | resuming a run from an existing state file (after compaction, across sessions, or after a `--confirm` grant of more rounds) | `pass` | yes, on any resume |
| `phase1_to_phase2` | entering Phase 2 after evaluation | `pass` | yes, unless `--fix-only` |
| `fixonly_entry` | `--fix-only` run, in place of `phase1_to_phase2` | `pass` | yes, on `--fix-only` |
| `handoff_<name>` | each handoff validation attempt | `fail` then `pass` on repair | yes, when validation runs |
| `scope_verify` | after Phase 2 edits | `pass` only on `clean`; **`fail` on an out-of-scope change AND on a check that could not answer** | yes, when edits were applied |
| `phase2_to_phase3` | entering Phase 3 after edits | `pass` | yes |
| `phase3_exit` | leaving Phase 3 (reference step 6) | `pass`, or **`fail` on regression auto-revert** | yes |
| `convergence` | after each Phase 3 convergence check (reference step 7), including the exit-2 ledger-repair re-run | `pass`, or **`fail` on escalate or capped, carrying `"reason"`** | yes |
| `workflow_exit` | before any exit | `pass`, or **`fail` on error halt** | yes |

**Two kinds of `fail`, and which kind it is decides what can settle it.** `hone_common.fail_is_accounted` is the one statement of this; the rest of this section is what it enforces.

- A **validation verdict** rejects an INPUT and orders nothing about the run. `handoff_<name>` is the whole family: repair the document, re-run the same validator. A later `pass` for that step is affirmative evidence the input was fixed, whatever happened in between, because nothing in between was forbidden.
- A **halt order** IS the instruction to stop, and every other row is one: `convergence:fail` is `escalate` or `capped`, `phase3_exit:fail` is the regression auto-revert, `scope_verify:fail` is an out-of-scope edit or a scope check that could not answer, a failed phase transition is a run that could not enter the next phase, `workflow_exit:fail` is the error halt. Doing another round is exactly what such a `fail` forbade, so nothing a later round emits is evidence about it -- a passing `convergence` on the next round does not settle an `escalate` on this one, it demonstrates the halt was ignored.

**A failing `convergence` declares WHY, and the declaration is read only as a reason to REFUSE.** Emit `"reason"` alongside `result: "fail"`, from the closed vocabulary `escalate` / `capped` / `ledger_missing` (`hone_common.HALT_REASONS` is authoritative; `references/gate-event-schema.json` mirrors it). The value is the verdict `check_convergence.py` returned, or `ledger_missing` for its exit 2, so there is nothing to invent. The three are one step with one name and one result and three opposite settlement rules, and they produce identical event sequences. Nothing else in the gate table needs a declaration, and `reason` stays free-form annotation everywhere else (`corrupt_state_file` on a `workflow_exit`, "prior evaluation reused" on a `fixonly_entry`).

**A declaration can only ever take a settlement away, never grant one.** `reason` is written by the same run being scored and nothing in the pipeline corroborates it, so it is never read as evidence that a claim is true -- only as the run ruling a settlement out. Measured against the behaviour before the field was read, every value forfeits something and no value gains anything: `escalate` gives up both the retry and the restart, `ledger_missing` gives up the restart, and `capped` gives up the retry.

**And no NON-declaration may buy anything either.** An absent, empty, misspelled, or invented `reason` reads as "not declared", and a non-declaration gives up **both** the restart and the in-place retry -- the strictest reading in the table, tied with `escalate` and below the other two. That is the other half of the same invariant and it is the half that is easy to lose: if silence kept a settlement that a truthful `escalate` forfeits, declaring the truth would cost gate score and the field would be a tax on honesty rather than a way to be read correctly. An out-of-vocabulary value is additionally a `validate_gates.py` error.

**What that buys, stated precisely.** Two things hold, and each has been broken once, in opposite directions, so the loose wording is worth avoiding. First, nothing you write reaches a settlement the events alone did not already reach, measured against the behaviour before the field was read. Second, nothing you leave out and no junk you write beats a vocabulary value. Together: **on the events an honest run actually emits, declaring the truth scores at least as well as anything else it could have written, and usually better.** The honest exit-2 repair emits the adjacent re-run, where only `ledger_missing` is accounted; the honest `capped` emits the exit and the `resume`, where only `capped` is accounted; the honest `escalate` emits its halt tail, where every answer is accounted and the truth ties. What is *not* claimed is that a false declaration is worthless on some arbitrary sequence -- reading a field the run writes about itself gives that up -- only that a liar is capped at what the same events scored before this field existed, and that the laundering shape this whole area exists to catch is refused under every reading.

| declared | halt tail | restart | in-place retry |
|---|---|---|---|
| before this field existed | yes | yes | yes |
| `ledger_missing` | yes | no | yes |
| `capped` | yes | yes | no |
| `escalate` | yes | no | no |
| nothing, or anything else | yes | no | no |

A halt order is settled in one of three ways, and only these:

- **The halt itself** -- `workflow_exit`, plus whatever the documented order places strictly after the failing gate. This is the ordinary case.
- **Nothing in between, then the same gate again** -- the in-place retry both repair loops perform. The exit-2 ledger repair writes a file and re-runs the check, emitting no gate event in between; the second `convergence` may itself `fail` when the re-run returns `escalate` or `capped`, so a correct repair produces no later `pass` at all. **The retry is also constrained from behind: what follows it must be that halt's tail too**, which is what stops a chain of fails from laundering each other and stops the extra round from simply moving to the far side of the retry. This rule is blunt on purpose and a declaration does not lift it. It has a known cost -- a `ledger_missing` repair whose re-run comes back `in_progress` and legitimately runs another round reads as not accounted, and loses gate score for it -- and that cost is accepted, because the honest repair and the laundering of an ignored `escalate` emit the same steps in the same order and nothing available can tell them apart. **Only a `fail` that declared `"ledger_missing"` reaches this retry at all.** `escalate` and `capped` are re-attempted nowhere in the docs, and a fail that declared nothing (or something outside the vocabulary) cannot say which failure it was, so all of them forfeit the retry and are accounted for by the halt tail -- or, for `capped`, by a restart -- or not at all. The declaration you write here is the string `check_convergence.py` just handed you, so declaring costs nothing and omitting costs a settlement.
- **The halt, then a passing `resume`** -- an authorized restart, and only for a `convergence` that declared `"reason": "capped"`. `capped` in `--confirm` mode is the one path in the whole workflow where a human legitimately restarts the loop, and the reference puts that gate outside and after the FORCED halt: the loop stops and emits `workflow_exit` first, and only then is the human asked. So emit the halt's `workflow_exit`, then `resume` when more rounds are granted, then re-enter Phase 2. A `resume` with no exit event in front of it describes a run that never stopped, and reads as an ignored halt. A `resume` carrying `result: "fail"` is a restart that did not happen and settles nothing. A `resume` behind a `convergence` that declared `escalate` -- or declared nothing -- settles nothing either: the reference is explicit that continuing after `escalate` is a fresh `/hone` invocation, not an extension of this one. No other gate has a documented restart, so appending an exit and a `resume` behind a failed phase transition or a failed `phase3_exit` does not settle it either.

**Migration, and what a state file written before `reason` existed loses.** Such a file declares nothing on its failing `convergence` events, so it takes the bottom row of the table above and forfeits BOTH settlements: an undeclared halt is no longer resumable, and an undeclared exit-2 in-place repair is no longer accounted either. Both cost gate score on a run that was correct, and both are the cheap error in the only direction this area is allowed to err. A false "not accounted" costs score; a false "accounted" credits a run for ignoring a halt, and anything that lets silence or a typo outscore a truthful `escalate` is a standing incentive not to declare at all, which defeats the field. Such files stay **valid** -- the missing `reason` is a `validate_gates.py` warning, not an error -- so nothing stops validating; only the settlement is withdrawn. Emitting `reason` is one string the run already holds, and it restores both.

**`handoff_<name>` and `convergence` are emitted once per ATTEMPT; every other row is emitted once per occasion.** That distinction is scored, not decorative: it is what says a retry of that gate exists at all. A gate the docs never re-attempt -- `phase3_exit`, the phase transitions -- has no retry to be settled by, so its `fail` is accounted for by the halt or the authorized restart, and by nothing else.

**The rows are in emission order, and Phase 3 emits `phase3_exit` BEFORE `convergence`** (references/phase3-reevaluation.md steps 6 and 7). The order is load-bearing, not cosmetic: `hone_common.is_halt_tail` decides whether the tail behind a failed gate is a halt or forward progress, and it reads that order. So the documented regression auto-revert halt is `[phase3_exit:fail, convergence, workflow_exit]`.

The template below is the clean six-event run: the sequence a normal run that applies at least one Phase 2 edit owes. `validate_gates.py` hard-errors on a missing `convergence` in `normal` and `fix-only` runs, and on a missing `scope_verify` whenever the state file records `applied_edits.edit_count > 0`, so a state file that omits either is invalid, not merely under-scored. A run that applied no edits at all omits `scope_verify` and emits the other five.

```json
{"step": "phase1_to_phase2", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "scope_verify",     "judge": "automated",  "result": "pass", "ts": "<ISO8601>"}
{"step": "phase2_to_phase3", "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "phase3_exit",      "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "convergence",      "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
{"step": "workflow_exit",    "judge": "self-check", "result": "pass", "ts": "<ISO8601>"}
```

Use `"fail"` for a gate that did not clear (entering Phase 2 with nothing to improve, a Phase 3 regression that triggered auto-revert, an error halt).

**Failure-path events are mandatory, not optional.** A documented recovery path that emits no event caps `gate_compliance` at 0.7, exactly as a skipped transition does. A `fail` event is scored as compliant when it is settled the way its kind of `fail` allows (see the two kinds above). Emitting the correct `fail` never costs you score: reporting a failure honestly is the behavior being measured. What does cost score is emitting a `fail` and then carrying on as though it had not happened.

**A perfect `gate_compliance` needs at least four compliant events.** The scorer divides the compliant count by the number of events emitted or by four, whichever is larger, and counts the shortfall as neutral (`GATE_EVIDENCE_FLOOR` in `scripts/score_execution.py`, which carries the measured distribution the four is read off). The mandatory sequence above is six events, two more than the floor, so a run that emits every event it owes is never padded; a run that emits one event and stops cannot score 1.0 off it. This cuts both ways by construction — a single malformed or unrepaired event no longer scores 0.0 either — and it is why emitting more truthful events is never worse than emitting fewer.

## Phase 1: Evaluate

**STOP. You MUST read [references/phase1-evaluation.md](references/phase1-evaluation.md) before executing any step below.** The bullets below are a navigation map only — the actual instructions, handoff schemas, gate checklists, and execution commands are in the reference file. Do not execute from this summary.

Load before Step 1 (Discover). Skip this phase entirely if `--fix-only` flag is set. **When skipping due to `--fix-only`, follow these steps in order:**
1. Mark all Phase 1 steps as `"skipped"` in the state file.
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
6a. **Step 6a: Overfit Classification** -- Run `check_overfit.py <criteria> --artifact <path> --json` (preference 20). Rewrite flagged items until the verdict is `within_threshold`; a run that already reports `within_threshold` needs no rewriting, since some flagged items are legitimate and vaguening them to empty the flagged list degrades the suite. Enrichment derives checks from the artifact, so this is not optional after Step 6 runs.
6b. **Step 6b: Power Check** -- Run `check_eval_power.py <criteria> --artifact-type <type> --json` (preference 19). Pass the artifact type from Step 1: the always-inconclusive profile exclusion applies to skills and commands only, and without the flag a hook or script suite is reported `underpowered` on cases that do score. **Below the floor the check is advisory, not a halt:** it exits 0 with `"blocking": false`, and Phase 1 records `underpowered` and continues. That is what reconciles the floor with hone's own generation minimums (2 cases on the lightweight tier, 3 at the Step 3/5 gates, 4 standard), which stay exactly as they are; the gate yields, not the generator. The verdict travels with the run: an `underpowered` round justifies neither a promotion nor a revert (Step 9a, and Phase 3 step 5's auto-revert precondition). Below the floor, add cases discriminating a different property; never add repeats to clear the floor. The floor counts `scorable_cases`, not every case: cases listed in `excluded_cases` carry a profile that is always inconclusive deterministically, so adding more of them cannot clear it. What still exits 1 from the sizing half is a duplicate test case id, which breaks the identity the next round pairs on; Step 6a (overfit) is unaffected and stays blocking.
7. **Step 7: Side-Effect Guard** -- Sandbox dangerous commands (git push, gh pr create, external messaging) in eval criteria.
8. **Step 8: Run eval runner** -- Pre-launch `validate_eval_criteria.py` gate (mandatory), then execute evaluation.
9. **Step 9: Deterministic Scoring** -- Run `score_execution.py` on results.
9a. **Step 9a: Power Verdict** -- Re-run `check_eval_power.py` with `--artifact-type <type> --before <prior deterministic_scores.json> --after <this round's>` when a prior round exists (the sizing half runs again, under the artifact type both rounds' deterministic files record, and suppresses the comparison when it fails). Record `power_verdict` (`powered`, `underpowered`, `improved`, `regressed`, `inconclusive`, `not_measurable`) in the state file alongside the composite. A composite without a power verdict is a number, not a result. An `underpowered` run is neither a pass nor a regression: record it, report it as `underpowered`, never as a pass, and never let it justify a promotion or a revert. It does not halt the run and does not send it back through Phase 1; adding cases that discriminate a different property is the standing remedy, not a precondition for continuing. `not_measurable` means the two rounds shared no test id, were scored by different scorers (a judge/deterministic swap, or two different versions of the deterministic scorer, which `metadata.scorer_fingerprint` identifies -- an absent fingerprint counts as an unknown scorer), both lack a deterministic file (judge scores are not compared), or cases that scored before came back inconclusive after (`inconclusive_after`) -- fix the inputs, not the criteria set.
10. **Step 10: Spec Artifacts** -- Generate evals.json, grading.json, timing.json, benchmark.json.
11. **Step 11: Reference Validation** -- Check all file/script/skill references exist on disk.
12. **Step 12: Report** -- Generate score report and HTML visualization.

**Exit to Phase 2** if any of these hold:
- any test scored below 0.8, or
- `--target` is set and composite < target, or
- `reference_validation.broken` is non-empty, or any structural finding has `effective_priority: "HIGH"`.

**Skip Phase 2** only when all three are false. Scores alone do not clear this gate: an artifact can score grade A while referencing a script that no longer exists, and a broken reference means the artifact is functionally broken regardless of its text quality. Check `reference_validation.broken` in the state file before deciding to skip.

Before entering Phase 2, write a gate event to `gates[]` in the workflow state file. Gate events for successful transitions MUST use `result: "pass"` — the schema only accepts `"pass"` or `"fail"` (never `"enter_phase2"`, `"continue"`, or any other value).

## Phase 2: Improve

**STOP. You MUST read [references/phase2-improvement.md](references/phase2-improvement.md) before executing any step below.** Load if Phase 1 found actionable failures OR HIGH-priority structural/reference findings. **Skip this phase only when the Phase 1 exit conditions above all evaluate false** — including the broken-reference check, not scores alone.

**Navigation map:**

1. **Step 1: Triage** -- Run `analyze_results.py --triage`, classify failures as criteria_bug/variance/real_issue.
2. **Step 2: Criteria Self-Repair** -- Fix criteria bugs via pattern table (if any).
3. **Step 3: Fresh-Eyes Analysis** -- Parallel fresh-eyes subagent (inherits session model) for independent improvement proposals.
4. **Step 4: Reconcile + Analyze** -- Merge main thread + fresh-eyes findings, performance audit.
5. **Step 5: Improvement Plan** -- Table of proposed changes with fix type and source. When multiple preferences apply to the same change, note any tension between them and state which takes precedence.
5a. **Step 5a: Scope Snapshot** -- Before any edit, snapshot the guarded tree by handing the script **`{edit_path}`** and its type (preference 21):

   ```bash
   python3 <skill-dir>/scripts/check_scope.py --artifact "{edit_path}" --type <skill|command|hook|script> \
     --manifest /tmp/scope-${RUN_ID}.json --snapshot --json
   ```

   `{edit_path}`, not `{artifact_path}`. Phase 1 Step 1 sets both, and for a marketplace or plugin skill they are different files: `{artifact_path}` is the installed copy hone reads, `{edit_path}` is the repo source Step 6 edits. Snapshotting the read path watches a tree nothing writes to, so `--verify` returns `clean` however much the checkout changed. Pass the artifact, not a hand-derived root. The watch root and the permitted scope are independent and derive differently -- a hook's watch is its install directory while its scope is that one file, and a skill inside a repository needs a watch that reaches the *whole repository* so an edit to a sibling `commands/` directory or a repo-root `scripts/` is visible. No single dirname chain produces both, which is why the script derives them. `--root` and `--scope` remain available as explicit overrides for the rare case that needs them.
6. **Step 6: Apply Edits** -- Stale-write guard, apply edits, generate companion validators if needed.
6a. **Step 6a: Scope Verify** -- Run the guard, handing it the paths this run wrote:

   ```bash
   python3 <skill-dir>/scripts/check_scope.py --manifest /tmp/scope-${RUN_ID}.json \
     --verify --json --declared-file $STATE_FILE
   ```

   Root and scope are read back out of the manifest, so nothing from Step 5a has to be reproduced here -- Step 5a and Step 6a are separate Bash calls and shell state does not survive between them. `--json` is not optional: without it the output is prose and the field names this step tells you to branch on and report are never printed.

   **The declaration is the whole basis of attribution, and it comes from you.** `--declared-file $STATE_FILE` reads `applied_edits.edited_paths`, the list Step 6 records. No comparison of two tree states can tell your write from the user's editor saving the same file mid-round, so the guard does not try: a change out of scope that you declared is one you caused and may revert, and a change out of scope you did not declare belongs to someone else and is only reported. Record **every** path you wrote, including generated companion files, not just the artifact. An in-scope change you failed to declare is caught (`undeclared_in_scope`) and makes the verdict `not_measurable`, because a declaration proven incomplete says nothing reliable about what it omits elsewhere. `--declared <path>` (repeatable) and `--declared-none` are the other two forms; **omitting the declaration entirely is `not_measurable`, never `clean`**.

   **Branch on the exit code, not on the prose.** There are four and only one is a pass: `0 clean` -> emit `scope_verify` with `result: "pass"` and continue; `1 scope_violation` -> undo **only** the paths in `violations`, then halt; `2` -> the check never ran (manifest missing or unreadable, or the declaration was malformed), revert nothing, halt; `3 not_measurable` -> the check ran and could not answer (git went quiet, a nested repository or submodule could not be hashed, a path under the watch root could not be read or classified -- an unlistable directory, a dangling symlink, a symlink loop, a socket -- an out-of-scope file changed that you did not declare, or no declaration was supplied), revert nothing, halt. **Never emit `result: "pass"` on 1, 2, or 3** -- "I could not determine the answer" recorded as a pass is worse than no guard, because the run then carries a `scope_verify: pass` for a tree nothing looked at.

   **How to undo a violation.** `git checkout -- <path>` is right for a file that was clean and tracked when the run began, and wrong for every other shape: on a file already uncommitted at snapshot it discards the user's earlier work along with yours, and on a file you created it does nothing at all. The report separates them -- anything also listed under `violations_manual_revert` needs your edit undone by hand (or the file deleted, if you created it), never a checkout.

   **Halting has a gate shape, and this is it.** `scope_verify` is a halt-ordering gate, so nothing a later round emits can settle its `fail`. Emit `scope_verify` with `result: "fail"`, then `workflow_exit` with `result: "fail"`, and stop the run -- do not start another round. Report `violations`, `unattributed_out_of_scope`, `undeclared_in_scope`, and `not_measurable_reasons` in the summary so the halt is actionable. The clean path is the one that gets forgotten, and it is the one the exit gate requires. A `root_fallback` field means the watch narrowed to the install directory because the repository held more uncommitted work than `--max-files` allows: a real gap in coverage, so say so rather than taking `clean` at face value.

   **Revert nothing listed under `preexisting_dirty_out_of_scope` or `unattributed_out_of_scope`.** Neither is attributable to this run. The first was already uncommitted at snapshot and is byte-identical to it. The second did change during the round, but you did not declare writing it, so a concurrent session, the user's editor, or a build step is what changed it: reverting it destroys work this run never created. That is why both are separate lists from `violations`, and why their presence makes the verdict `not_measurable` rather than `clean`.
6b. **Step 6b: Constraint Ablation** -- For each constraint flagged as possible dead weight (preference 22), remove it, re-run the existing criteria, and restore it only if a test regresses. Record each ablation and its outcome in the ledger. Skip constraints guarding irreversible actions.

   **Ablation writes, so it is inside the guarded window and not behind it.** These are the last Phase 2 edits, and they land after Step 6a has already run. If any ablation wrote to disk -- including one restored after a regression, which is two writes -- add the paths it touched to `applied_edits.edited_paths` and run Step 6a's command again before Phase 3, branching on its exit code exactly as before. The manifest still holds the pre-Phase-2 snapshot, so the second verify covers every Phase 2 edit rather than only the ablation's; emit its `scope_verify` event too, and let it be the one that decides.
7. **Step 7: Description Trigger Testing** -- Test whether the description triggers correctly on realistic prompts.
8. **Step 8: Ledger Append** -- Append this round's findings to `~/skill-eval/{name}/findings-ledger.json`. The ledger is the run's memory across rounds and across runs: a resumed run reloads it instead of re-deriving findings, and rejections are not re-litigated without new evidence. **Give the round entry a `"run"` field carrying `${RUN_ID}`, the same value for every round of this invocation.** The file is per artifact and permanent while `round` restarts at 1 and `max_rounds` is a per-run budget, so `run` is what tells `check_convergence.py` where one invocation's rounds end and the next begins. Without it the boundary is inferred from repeated round numbers, which cannot see a run that never restarts its numbering. Findings go inside the round entry; a ledger with no `rounds` array, or with findings at the top level, is rejected with exit 2.

   Findings are nested under the round that produced them, and `check_convergence.py` reads that wrapper. A bare array of findings, or findings appended at the top level, parses as zero rounds: the verdict is `in_progress` with `rounds_run: 0` on every round, forever, and the script reports no error. Write exactly this shape:

   ```json
   {
     "artifact": "{name}",
     "max_rounds": <max_rounds>,
     "rounds": [
       {"round": 1,
        "run": "${RUN_ID}",
        "findings": [
          {"id": "F1", "severity": "critical", "file": "SKILL.md",
           "summary": "Step 4 has no stated exit condition", "status": "open"}
        ]}
     ]
   }
   ```

   Both `max_rounds` and `run` are per-run values; everything else in the file accumulates. Resolve them before writing: `<max_rounds>` is this run's `--rounds N` budget, the same value the state-file template binds to `iteration.target`, and `run` is the resolved `${RUN_ID}` string, not the literal `${RUN_ID}`. A hardcoded `3` is the one that bites silently, because `check_convergence.py` reads `max_rounds` to decide `capped` and `capped` is a FORCED halt: a `--rounds 6` run stops at round 3 and reports itself capped. Leaving `<max_rounds>` unresolved is the other way to get it wrong -- valid JSON, an unparseable int -- and `check_convergence.py` exits 2 on it rather than running with `capped` disabled.

   `severity` is `critical`, `major`, or `minor` (`critical` and `major` are the blocking ones the convergence check counts); `status` is `open`, `fixed`, or `rejected`. Both are closed vocabularies and the check exits 2 on a value outside them, rather than reading an unknown `severity` as non-blocking or an unknown `status` as not-open -- either of which turns a ledger with blocking work outstanding into `verdict: converged`. Case is folded, so `Open` is accepted. Each round appends a new entry to `rounds` and restates every finding still live, including ones carried over unchanged -- that repetition is what lets the check see a finding stay open across rounds.

## Phase 3: Re-Evaluate

**STOP. You MUST read [references/phase3-reevaluation.md](references/phase3-reevaluation.md) before executing any step below.** Load only after Phase 2 edits are applied. Key branch: if any dimension regresses > 0.1, auto-revert and halt.

**Navigation map:**

1. Refresh enrichment (skills and commands), then re-run `check_overfit.py` (Step 6a's gate): the refresh re-derives anchors from the current artifact, so an anchor Step 6a had you remove can come back. If either step changed the criteria file, re-score the prior round's `results.json` against the current criteria before comparing, with `--artifact-path` pointed at the pre-edit snapshot (`applied_edits.artifact_before_snapshot`), never the current artifact: `quality_checks` is a ratio over `required_present`, so a criteria edit alone moves every composite, and the step and heading dimensions are derived from the artifact, so a heading Phase 2 added reads as a step the baseline skipped. Record `baseline_original` and `baseline_adjusted` (composite and `per_test`) so the step 3 regression check compares against the same adjusted baseline as the power comparison. Then re-run eval runner with same criteria (blind evaluation — no mention of improvements).
2. Run deterministic scoring on re-eval results, then the Step 9a power comparison (`check_eval_power.py --artifact-type <type> --before <prior round> --after <this round>`) and record `power_verdict` beside the composite. `underpowered` and `not_measurable` are never reported as an improvement, and neither justifies an auto-revert either: Phase 3 step 5 withholds the restore on both and hands the suspected regression to the human. On the first round of a `--fix-only` run there is no prior round: run the sizing half alone and record `powered`/`underpowered`; the comparison starts on round 2.
3. Compare before/after per-dimension. A drop > 0.1 in any dimension flags a regression, but resample first: re-run the tests feeding that dimension twice more and take the median. Auto-revert only if the median still shows the drop. If the prior round's `deterministic_scores.json` records a different `metadata.scorer_fingerprint` than this round's -- or records none, which means an unidentified scorer, not an unchanged one -- re-score the prior round's results with the current scorer before comparing, so a measurement change is not read as an artifact change. The trigger is the recorded fingerprint, not whether this run edited `score_execution.py`: a scorer change that lands through a merged PR touches no run at all. `check_eval_power.py` returns `not_measurable` on a mismatch or an absence, which is already a verdict Phase 3 must not revert on.
4. Write scores to state file. Append a gate event to `gates[]` — use `result: "pass"` for a successful round (no regression), `result: "fail"` only if regression triggered auto-revert. Never use `"exit"`, `"continue"`, or descriptive values — only `"pass"` or `"fail"` are valid.
5. Run `check_convergence.py ~/skill-eval/{name}/findings-ledger.json --json`. It returns `converged`, `capped`, `escalate`, or `in_progress`. **Branch on the JSON `verdict`, not the exit code:** only `converged` exits 0, so `in_progress` -- the ordinary continue case in step 7 below -- exits 1 alongside the two halt verdicts. Treat exit 1 as "not converged yet" and read the verdict; only exit 2 (missing or unparseable ledger) is a real failure.

   **Emit the `convergence` gate event on every verdict, not only the halting ones.** `result: "pass"` for `converged` and `in_progress` (the check ran and did not stop the loop), `result: "fail"` for `escalate` and `capped`. **Every `fail` carries `"reason"` set to that verdict** (or `ledger_missing` for exit 2) -- see the Gate Events section: the three failures have opposite settlement rules and the declaration is the only thing that tells them apart. `validate_gates.py` hard-errors on a missing `convergence` in normal and fix-only runs, so an omitted event on the ordinary round invalidates the state file.

   - `escalate`: the loop is not converging (a finding open four rounds, a blocking count flat for four, or a finding closed in one file that reopens as a NEW finding in another, each measured within this run). The two counting bars sit one above the templated `max_rounds: 3` on purpose, so a run that merely spends its budget reports `capped` -- which reaches the `--confirm` human gate -- and `escalate` is reserved for a loop still going nowhere with rounds to spare. `scripts/check_convergence.py` is authoritative for both numbers. Emit `fail` with `"reason": "escalate"`, report the finding ids, then halt. There is no restart from here: continuing is a fresh `/hone` invocation with the findings triaged by hand.
   - `capped`: the round budget ran out with blocking findings still open. Emit `fail` with `"reason": "capped"` -- this is the one halt the `--confirm` human gate may restart, and the declaration is what entitles it to -- report it as **capped, not converged**, list the open blocking findings, then halt. Never present a capped run as success.
   - `converged` / `in_progress`: emit `pass` and continue to step 6.
   - **Exit 2** (no ledger, or one the script cannot parse) is a Phase 2 Step 8 omission, and it is repairable rather than fatal. Emit `convergence` with `result: "fail"` and `"reason": "ledger_missing"` -- the declaration records which failure this is, and it is the only reason a retry may settle at all; it does not exempt the retry from having to sit inside the halt. Then write the ledger from this round's findings per Phase 2 Step 8, and re-run the check once. The re-run's verdict then drives the branches above, and its `pass` closes the failed event as the repair loop the Handoff Validation Protocol already describes. If the second run still exits 2, that is an error halt: report the path and the script's stderr, emit `workflow_exit` with `result: "fail"`, and stop.
6. Check the mechanical exit gate. It runs **after** the convergence check, never before it: the gate emits `workflow_exit`, and a `convergence` event recorded after that exit reads as forward progress past the halt rather than as the halt itself. Same order as the Phase 3 reference (steps 7 then 8).
7. The mechanical exit gate decides whether to loop, not the convergence verdict. Exit only when the gate FORCES an exit or ALLOWS one; in every other case increment the round and loop back to Phase 2 (reference step 9). Only `escalate` and `capped` halt on the verdict alone. `converged` and `in_progress` are inputs the gate weighs, not loop conditions of their own, so `converged` on round 2 of 3 with the score still moving is BLOCKED and keeps looping -- treating the verdict as the loop condition left that case with no defined action.

**Mechanical exit gate** decides when to stop (state file, not LLM judgment). See Phase 3 reference for full BLOCKED/ALLOWED conditions.

## Common Executor Mistakes

1. **Printing text instead of using AskUserQuestion.** When the STOP section says "Call AskUserQuestion", you must call the tool. Text output does NOT satisfy the gate.
2. **Proceeding past a STOP gate.** When a gate says "STOP immediately", no further workflow steps should execute.
3. **Narrating the workflow in an error stop.** When stopping on a validation error, the error message and its options are the whole response. Say what is wrong and what the valid choices are, then stop. This applies to every halt, not just argument validation: on a corrupt state file or any mid-run error halt, report the failure, the file path, and how to resume. Do not inventory the work you are declining to do: a list of the steps you did not reach is workflow narration in an error stop, where the error and the options are the whole response.

   A denial is not itself the violation. `score_execution.py` scopes each negation cue to the clause it governs and walks back through commas, so a single denial covering a comma-separated list ("did not reach the audit, criteria generation, or the eval runner") is read as one denial and is not scored as a forbidden phrase. What still scores as a violation is the phrase appearing outside a denial, and a semicolon is a hard clause break: "did not run the audit; proceeded to Phase 2" negates only the first clause.
4. **Naming internal machinery in fallback output.** When AskUserQuestion is unavailable and the fallback fires, the response is the question and its options and nothing else. Internal section names, step names, and script names belong in the state file, not in a user-facing stop message — a response that names them fails even when the question itself is correct.
5. **Sequential reads for independent files.** When a phase starts by reading multiple unrelated files (artifact, reference file, state file), issuing them one at a time is a latency violation. Batch all independent Read calls into a single parallel tool-use turn.
6. **Wrong result values in gate events.** Gate events only accept `"result": "pass"` or `"result": "fail"`. Using `"enter_phase2"`, `"enter_phase3"`, `"exit"`, `"continue"`, or any other value is a schema violation and causes gate_compliance to score 0.0. Use `"pass"` for all successful phase transitions.
7. **Writing state file after reading reference files.** The state file MUST be written as the very first action. The Phase 1 STOP to read `phase1-evaluation.md` governs what you read before executing Phase 1 steps — it does NOT override state initialization. State file written after references = sequencing violation.
8. **Omitting gate events when demonstrating parallel operations.** The Execution Efficiency Rules describe parallelism optimization for tool calls within steps — they do not replace inter-phase gate events. In SIMULATION MODE, include the corresponding gate event as JSON inline in your response. An executor showing parallel reads with no gate event scores 0.0 on gate_compliance.

## Context Compaction Protection

This workflow runs 30+ minutes per eval round. After generating/editing eval criteria, re-read from disk. After each eval runner run, record output path and scores. After applying edits, re-read to confirm. Before re-evaluation, re-read both files. **After context compaction, re-read the active phase's reference file** (references/phase1-evaluation.md, phase2-improvement.md, or phase3-reevaluation.md) — reference file content is lost on compaction just like conversation history.
