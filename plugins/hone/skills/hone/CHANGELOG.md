# Hone Changelog

## 2026-08-15: Backup Tool Size Threshold (v2.5)

- **phase1-evaluation.md**: the blanket "Do NOT use `Bash(cp)`" prohibition on backups is replaced by a 10 KB size threshold. Below 10 KB, Read then Write, since dedicated tools give better user visibility and re-emission is cheap. At or above 10 KB, `cp`, since re-emitting a large file through Write costs output tokens and wall-clock latency proportional to file size and buys no visibility that matters for a rollback/diff artifact. Improvement preference 5 (efficiency = latency) governs above the threshold. Applies to the Step 1 original backup and the Step 4 criteria pre-audit backup; Step 6's pre-enrich backup already used `cp`.
- **phase1-evaluation.md**: Step 1 now specifies `wc -c` to check size before choosing, and `test -s` to confirm the backup landed either way.
- Motivated by the 2026-08-15 self-hone run: hone's own SKILL.md is 29 KB, where the Write mandate costs roughly 7.5k output tokens per backup for no gain.

## 2026-08-15: Model-Reference Refresh (v2.4)

- **phase2-improvement.md**: fresh-eyes subagent no longer pinned to `model: sonnet`; it inherits the session model per improvement preference #1, since it is a high-judgment step where proposal quality matters more than fan-out latency. Sonnet pins remain for parallel per-test analysis (SKILL.md Model Selection, phase2 Step 4 main-thread fan-out).
- **phase1-evaluation.md**: "the main thread (Opus)" in criteria-audit Step 3 now reads "the main thread (session model)"; the hard-coded model name was stale.
- **gate-event-schema.json**: added `model-judge` to the judge enum for session-model judges; `opus-judge` retained for back-compat with older state files.
- **SKILL.md, script-quality-checklist.md**: prose synced from "sonnet subagent" to "fresh-eyes subagent" where it refers to the fresh-eyes step.

## 2026-04-14: Step Numbering Pillar + Sandbox Hardening (v2.3)

- **structural_audit.py**: added STEP_NUMBERING as a standalone WARNING_ONLY pillar (lightweight=skip, standard=HIGH, complex=HIGH). Detects non-flat step labels (eg `Step 6b`), duplicate step numbers, decimal steps (`Step 1.2`), and gaps in the sequence. Extended `STEP_NUMBER_RE` to `\d+(?:\.\d+)?[a-z]?` so `_validate_step_sequence` can surface these cases.
- **structural_audit.py**: Pillar 1 (progress_gates) ownership trimmed to gate detection only — it no longer flips `passed=False` on sequence findings. Sequence issues now surface exclusively through STEP_NUMBERING.
- **side_effect_guard.py**: `BASH_SIDE_EFFECTS` extended with `mkdir`, `printf >`, `echo >`, `cp`. New `parse_allowed_tools_frontmatter()` reads artifact YAML frontmatter (`allowed-tools:` inline or block form); `guard_criteria()` now intersects each test case's `allowed_tools` with the artifact frontmatter value when present, and `main()` proceeds even when only the frontmatter signal exists.
- **validate_eval_criteria.py**: new `check_runner_context_hygiene` enforces side-effect-free simulation-only runner_context. Flags filesystem-mutating commands (mkdir, printf >, echo >, cp), bans `SETUP:` blocks, and requires a `SIMULATION MODE:` header when runner_context is non-empty. Wired into `audit()` after `check_runner_context_present` (empty runner_context is still handled upstream).
- **SKILL.md**: renumbered Phase 1 navigation map — Step 6b (Side-Effect Guard) becomes Step 7, and the cascade shifts Run eval runner (7→8), Deterministic Scoring (8→9), Spec Artifacts (9→10), Reference Validation (10→11), and Report (11→12). Updated the "Parallel subagents" rule to reference Step 8.
- **phase1-evaluation.md**: applied the same 6b→7 / 7→8 / 8→9 / 9→10 / 10→11 / 11→12 shift to section headings, gate/handoff labels, and inline prose cross-references (Step 3 routing, Step 4 audit boundary, Step 11/12 report cross-refs).
- **test_structural_audit.py**: added TestStepNumberingPillar (non-flat labels, duplicates, decimals, gaps, WARNING_ONLY score invariance, progress_gates no-fail-on-sequence) and extended TestValidateStepSequence with non-flat label and duplicate cases. Relaxed `test_minimal_skill_has_security_and_description_applicable` to tolerate the expanded applicable-pillar set.
- **test_validate_eval_criteria.py**: added TestRunnerContextHygiene covering mkdir/printf>/SETUP detection, missing SIMULATION header, empty runner_context pass-through, and clean simulation runner_context.

## 2026-04-08: Structural Audit Expansion + Consistency Fixes (v2.2)

- **structural_audit.py**: added Pillar 12 (compaction_protection) — checks for 5 compaction recovery categories (explicit section, re-read instructions, intermediate persistence, reference re-read anchors, resume instructions). Priority: complex=HIGH, standard=LOW, lightweight=skip. Advisory only (WARNING_ONLY).
- **structural_audit.py**: added Pillar 13 (spec_compliance) — checks Agent Skills spec limits: description 1-1024 chars, body < 500 lines, no root-level custom frontmatter fields. Advisory only (WARNING_ONLY).
- **structural_audit.py**: added Pillar 14 (autonomous_execution) — opt-in check; only applies when artifact advertises `--auto` or non-interactive mode. Verifies blocking calls (AskUserQuestion) are in validation gates, not mid-flow. Advisory only (WARNING_ONLY).
- **structural_audit.py**: updated docstring from "11-pillar" to "14-pillar"; fixed all pillar docstrings to use unique sequential numbers (1-14, no duplicates).
- **SKILL.md**: added Improvement Preferences #16 (context compaction protection) and #17 (constraint compilation); added Common Executor Mistake #4 (workflow-internal terms in fallback) and #5 (sequential reads for independent files).
- **phase3-reevaluation.md**: synced Common Executor Mistakes section with SKILL.md — added items #4 and #5.
- **phase1-evaluation.md**: updated pillar count text from 13 → 14; added rows 13 (spec_compliance) and 14 (autonomous_execution) to the pillar applicability table.
- **SKILL.md**: fixed `compatibility:` frontmatter field (moved from root level to under `metadata:` to comply with Agent Skills spec).
- **test_structural_audit.py**: updated docstring from "10-pillar" to "14-pillar".

## 2026-04-04: Agent Skills Spec Compliance (v2.1)

- **generate_spec_artifacts.py**: new script — converts eval runner output to Agent Skills open standard format (evals.json, grading.json, timing.json, benchmark.json)
- **structural_audit.py**: added Pillar 11 (script_quality) — checks bundled scripts for agentic design principles (no interactive prompts, --help, structured output, exit codes, self-contained deps). Advisory only (WARNING_ONLY). New `--scripts-dir` CLI arg.
- **phase1-evaluation.md**: added Step 5.6 (spec artifacts generation), timing capture around eval runner, baseline run logic (`--with-baseline` + first-eval auto-baseline)
- **phase2-improvement.md**: added Step 4.5 (description trigger testing), script-quality-checklist consultation in fresh-eyes Step 1.7, fixed stale "12 improvement preferences" → "15"
- **SKILL.md**: added `--with-baseline` and `--skip-trigger-test` flags, Improvement Preferences #14 (spec eval format) and #15 (description trigger accuracy), cross-client search paths in auto-inference, Step 5.6 in Phase 1 nav map
- **artifact-profiles.md**: expanded skill Discovery to 7-path cross-client search (Claude Code, .agents, shared, Codex, project-level, marketplace)
- **agent-skills-spec.md**: added "Script Design for Agentic Use" section (10 principles, mechanical vs LLM-judged split)
- **NEW references/script-quality-checklist.md**: 5 LLM-judged script quality checks for Phase 2
- **NEW references/description-trigger-testing.md**: methodology for testing skill description trigger accuracy
- **NEW scripts/test_generate_spec_artifacts.py**: 20 unit tests for the converter
