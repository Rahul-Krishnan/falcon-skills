---
name: hindsight
description: "Cross-session retrospective analysis. Finds recurring friction, mistakes, and improvement opportunities across multiple Claude Code sessions. Use when the user wants to review patterns across past sessions, asks what keeps going wrong, or invokes /hindsight. Do NOT trigger for single-session questions like 'what did I do last session' or 'what happened in my last session' — those are session-history lookups, not cross-session retrospectives. Answer single-session questions directly from that session's transcript (via the session-history skill if it is installed, otherwise by reading the transcript) — do not invoke the hindsight pipeline."
metadata:
  user-invocable: true
  argument-hint: "[window=7d] [--hype|--roast] [--human|--ai] [--viz] [--auto]"
  allowed-tools: "Task, Read, Glob, Grep, Bash(mkdir:*, cat:*, ls:*, touch:*, echo:*, python3:*, wait:*, which:*, cp:*, rm:*, tail:*, wc:*), Write, TodoWrite, AskUserQuestion"
---

```
██╗  ██╗██╗███╗   ██╗██████╗ ███████╗██╗ ██████╗ ██╗  ██╗████████╗
██║  ██║██║████╗  ██║██╔══██╗██╔════╝██║██╔════╝ ██║  ██║╚══██╔══╝
███████║██║██╔██╗ ██║██║  ██║███████╗██║██║  ███╗███████║   ██║
██╔══██║██║██║╚██╗██║██║  ██║╚════██║██║██║   ██║██╔══██║   ██║
██║  ██║██║██║ ╚████║██████╔╝███████║██║╚██████╔╝██║  ██║   ██║
╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═════╝ ╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
It's not nagging if it's data-driven.
```

# /hindsight — Cross-Session Retrospective

> Three parallel agents scan your transcripts, memory, and workspace to surface recurring friction, stale rules, and missed automation opportunities.

Analyzes patterns across multiple Claude Code sessions to find recurring friction, mistakes, workflow inefficiencies, and improvement opportunities for both the AI and the human user.

**PRIVACY:** See [Privacy Rules](#privacy-rules) at the end of this file -- they apply to ALL phases.

## Trigger

Invoked via `/hindsight`. Optional arguments:
- `window=<duration>` — eg `window=7d`, `window=3sessions` (default: max(since last hindsight, last 14 days), or last 14 days if no prior hindsight)
- `--viz` — after wrap-up, generate a visual HTML report (Organic Earth style) saved to your local reports directory. Off by default. When active, print at Phase 0: "Visual report will be saved locally at the end." The `--viz` flag does NOT affect Phases 0-3e; it only triggers Phase 3f after everything else is done.
- `--auto` — non-interactive automation mode. Skips Phase 3c (AskUserQuestion) and Phase 3d (user-driven finding review). Instead: auto-applies all LOW/MED NEEDS_APPROVAL findings without prompting; logs HIGH/CRITICAL NEEDS_APPROVAL findings to `~/.claude/state/overnight-flags.md` for human review; writes the full report automatically; exits. Use when invoked from rem-sleep or other automated pipelines where no user is present.

### Tone Modes

Control how findings are presented. Affects Phase 3 output style only — collection and synthesis are always analytical.

- **(default)** — Neutral. Analytical, evidence-first, no editorializing. "Pattern: AI sent external message without approval. 4 incidents across 3 sessions."
- **`--roast`** — Brutal. Calls it like it is. "You did this AGAIN. Third time this week, and you literally have a rule about it. The rule you wrote. After the last time you did this."
- **`--hype`** — Celebratory. Highlights wins and frames issues as easy fixes. "Look at this growth! You caught 3 more patterns than last retro. One small thing to tighten up and you're golden."

### Focus Modes

Control what categories of findings to prioritize. Affects which taxonomy categories are surfaced prominently vs deprioritized.

- **(default)** — Both human and AI patterns, weighted equally.
- **`--human`** — Prioritize human-side patterns: scope creep, skipping planning, not reading AI output, overriding good suggestions, context-switching too fast, not confirming before posting.
- **`--ai`** — Prioritize AI-side patterns: constraint amnesia, search cascades, unauthorized actions, shallow investigation, task substitution, context compaction losses.

When tone and focus modes are combined (eg `--roast --human`), apply the tone mode to all findings regardless of focus. Focus determines sort order and weighting; tone determines presentation style.

Focus modes don't hide findings — they sort and weight. A `--human` run still shows AI issues, just after the human ones. The Phase 3 header shows the active mode:

```
  Mode: --roast --human
```

## Argument Parsing (do this FIRST before any phase)

Parse the invocation string into structured flags. This is a prerequisite for all phases.

```
Input: "/hindsight [args...]"
Parse into:
  window      = extract "window=<value>" if present, else null
  tone        = "--roast" | "--hype" | null (default: neutral)
  focus       = "--human" | "--ai" | null (default: both)
  viz         = true if "--viz" present, else false
  auto        = true if "--auto" present, else false

Examples:
  "/hindsight"                       → window=null, tone=null, focus=null, viz=false, auto=false
  "/hindsight window=7d"             → window="7d", tone=null, focus=null, viz=false, auto=false
  "/hindsight --roast --human --viz" → window=null, tone="roast", focus="human", viz=true, auto=false
  "/hindsight window=3sessions --ai" → window="3sessions", tone=null, focus="ai", viz=false, auto=false
  "/hindsight --auto"                → window=null, tone=null, focus=null, viz=false, auto=true
```

When `viz` is true, print at Phase 0: "Visual report will be saved locally at the end."

When combining tone + focus: tone affects Phase 3 presentation style only; focus affects sort order of findings in Phase 3. Collection and synthesis (Phases 1-2) are always analytical regardless of tone.

## Workflow State

**Write this file as your FIRST action — before Phase 0, before reading any other file, before any other tool call.** This is unconditional. It applies no matter which flag was passed and no matter which mechanic (carry-forward, roast mode, a specific window) the invocation is focused on. An executor that jumps straight to Phase 0 because the interesting part of the request lives later in the pipeline has already broken the resume path.

**Run ID (compute once, at start):**
```bash
RUN_ID="hindsight-$(date +%Y%m%d-%H%M)"   # eg hindsight-20260713-0022
```
The state file is keyed per RUN, not per session, so two hindsight runs in one session (or a hindsight dispatched from `/rem-sleep` alongside other skills) never share a file.

**Recovering RUN_ID after compaction:** the timestamp is not reconstructible from memory. Glob for the most recent run instead:
```bash
STATE_FILE=$(ls -t /tmp/workflow-hindsight-*.json 2>/dev/null | head -1)
```
If that returns nothing, no run is in progress and you are starting fresh.

Write state to `/tmp/workflow-${RUN_ID}.json` at start:
```json
{"workflow": "hindsight", "steps": {"phase0": "pending", "phase0_5": "pending", "phase1": "pending", "phase1_5": "pending", "phase2": "pending", "phase3": "pending"}, "gates": [], "open_questions": []}
```
Update each step to `"in_progress"` then `"done"` as you go. Track open questions. Before any exit, re-read: if `open_questions` has items answerable with another search/read/tool call, keep going. On re-entry, skip done steps.

**Gate events:** At each phase transition, append a gate event to `gates[]`:
```json
{"step": "<from>_to_<to>", "judge": "self-check", "result": "pass", "ts": "<ISO 8601>"}
```
Required transitions: phase0→phase0_5, phase0_5→phase1, phase1→phase1_5, phase1_5→phase2, phase2→phase3. Use `"result": "fail"` only when the transition should not proceed (eg zero sessions at phase0_5).

**Mechanical exit gate (MANDATORY before any exit):** Before stopping for any reason (wrap-up, early exit, error), re-read the state file and verify: (1) all steps are `"done"` or `"skipped"`, (2) no step is `"pending"` or `"in_progress"`, (3) `open_questions` is empty or all items are unanswerable. If any step is non-done with no error: resume from that step, do not exit.

## Quick Start

When invoked, execute this pipeline:

### Execution Path Decision Tree

Before reading the full pipeline, determine which path applies:

```
/hindsight invoked
  |
  +--> Write workflow state file (ALWAYS — first action, before anything else)
  +--> Parse arguments (always)
  +--> Phase 0: Setup (always)
  +--> Phase 0.5: Pre-flight session check
  |      |
  |      +--> 0 sessions AND 0 fingerprints? --> EXIT EARLY (no data)
  |      +--> Otherwise, continue
  |
  +--> Is Task tool available?
  |      |
  |      +--> YES: Phase 1 via 3 parallel subagents (Subagents 1/2/3)
  |      +--> NO:  Phase 1 via collect-then-analyze fallback
  |                (Step A: Bash backgrounding, Step B: single-pass analysis)
  |
  +--> Phase 1.5: Validate outputs
  +--> Phase 2: Synthesis
  +--> --auto flag set?
  |      |
  |      +--> YES: Phase 3 (auto mode): 3a header + 3b findings summary (printed),
  |      |         then auto-apply all LOW/MED NEEDS_APPROVAL findings without prompting,
  |      |         log HIGH/CRITICAL NEEDS_APPROVAL to ~/.claude/state/overnight-flags.md,
  |      |         write full report, proceed to 3e wrap-up. Skip 3c (AskUserQuestion)
  |      |         and 3d (finding detail flow).
  |      +--> NO:  Phase 3: Interactive report + actions (3a-3e)
  |
  +--> --viz flag set? --> Phase 3f: Generate HTML visual report
```

**Phase narration (mandatory):** As you progress through each phase, explicitly label it in your output (e.g., "**Phase 0: Setup**", "**Phase 1: Collection**", "**Phase 2: Synthesis**"). This helps the user track progress. After Phase 0 completes, output one summary line: "Setup complete — window: `<start>` → `<end>`, parser: `<mode>`, taxonomy: `<N>` categories."

### Phase 0: Setup

**Read all setup files in parallel** (steps 1, 2, and 4 are independent reads — issue them in a single message with multiple Read tool calls):

1. Read `~/.claude/hindsight/last-retro.json` to determine the analysis window and load carry-forward state. If missing, default to last 14 days. If present, use `max(since last hindsight, last 14 days)` as the window — this guarantees a minimum 14-day floor so slow-burn patterns aren't missed by frequent runs. Also extract `unresolved_findings` if present — these are findings from the previous hindsight that were skipped. They will be re-checked against the current window and re-surfaced if the pattern continues (with bumped severity). If the pattern is not found in the new window, age it out silently.
1a. **Resolve session-count windows.** If `window` matches `N sessions` (eg `window=3sessions`), do NOT convert it to a calendar duration. Instead: enumerate all sessions across all projects (live transcripts plus fingerprints, deduplicated), sort by end timestamp descending, take the N most recent, and set the effective window to `[end timestamp of the Nth session, now]`. Pass this resolved date range — not the raw session count — into `setup_context.window`, so every downstream subagent filters on the same concrete boundary. If fewer than N sessions exist, use all available ones and say so in the Phase 0 summary line. Without this resolution step two runs of the same `window=3sessions` command can select different session sets.

2. Load finding categories from taxonomy. Check in order: (a) `~/.claude/hindsight/taxonomy.json`, (b) `references/taxonomy.json` relative to this skill file's directory. If neither exists, use these hardcoded categories:
   ```
   unauthorized_actions, scope_creep, search_cascades, planning_incompleteness,
   constraint_amnesia, output_neglect, memory_staleness, context_bloat,
   rule_contradiction, redundancy, hypomania_signals, zombie_file
   ```
3. Parse the window argument if provided (override default).
4. Check for `~/.claude/skills/session-history/scripts/cclog.py`. Set parser mode to `robust` if present, `fallback` if not. If fallback, tell the user: "For better results, install the session-history skill (provides a robust transcript parser)."
5. Check for fingerprint files in `~/.claude/hindsight/fingerprints/*.json`. These are lightweight session summaries persisted by the hindsight plugin's Stop hook. They persist across machines when you sync `~/.claude`, providing data even when raw transcripts are gone. Count how many fingerprints fall within the analysis window.

6. Run `mkdir -p ~/.claude/hindsight/{logs,reports,fingerprints}` to ensure output directories exist.

**Do NOT read these files sequentially.** Steps 1, 2, and 4 have no dependencies on each other and MUST be issued as parallel Read calls in a single message. Step 5 can run in parallel with them.

### Phase 0.5: Pre-flight Session Check

**CRITICAL: Do this check BEFORE launching any subagents.** If there is no data, exit early and save tokens.

Before launching subagents, do a quick session count check to avoid wasting tokens on empty data:
- Count **live transcripts** (current machine):
  - If `robust` mode: Run `~/.claude/skills/session-history/scripts/cclog.py --format=json projects 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(p['sessions'] for p in d.get('projects',[])))"` and check the total session count across all projects.
  - If `fallback` mode: Run `ls ~/.claude/projects/*/*.jsonl 2>/dev/null | wc -l` to count session files.
- Count **fingerprints** (persisted across machines): Run `ls ~/.claude/hindsight/fingerprints/*.json 2>/dev/null | wc -l`.

**Zero-session exit flow (MANDATORY):**
- If 0 live transcripts AND 0 fingerprints:
  1. Print: "No sessions found in the last `<window>`. Try widening: `/hindsight window=30d`"
  2. If no fingerprints directory exists, that's expected — fingerprints are an optional enhancement and the skill works on live transcripts alone.
  3. Do NOT proceed to Phase 1, Phase 2, or Phase 3
  4. Do NOT write `last-retro.json`
  5. Update the workflow state file before exiting: set `phase0_5` to `"done"`, set every remaining step (`phase1`, `phase1_5`, `phase2`, `phase3`) to `"skipped"`, and append a gate event with `"result": "fail"` and a reason of `"zero sessions"`. The Mechanical Exit Gate forbids exiting with any step left `pending`; skipping this leaves stale `pending` steps on disk that the next run's compaction-resume logic will try to resume from.
  6. Exit cleanly — the pipeline ends here
- If 0 live transcripts but fingerprints exist, proceed — the transcript scanner will use fingerprints as its data source.

### Phase 1: Collection (3 parallel subagents)

**Handoff interface (Phase 0.5 → Phase 1):**
```
setup_context: {
  window: {start: string, end: string},
  parser_mode: "robust" | "fallback",
  taxonomy_categories: string[],
  last_retro: object | null,
  unresolved_findings: object[] | null,
  fingerprint_count: number,
  session_count: number,
  auto_mode: boolean,
  viz_mode: boolean,
  tone: "neutral" | "roast" | "hype",
  focus: "both" | "human" | "ai"
}
```

Launch 3 subagents in parallel using the Task tool. Each outputs structured JSON findings.

**Important:** Use `subagent_type: "general-purpose"`, `model: "sonnet"`, and `max_turns: 20` for all three. These subagents do pattern recognition, taxonomy classification, and severity assessment on top of I/O work — sonnet provides meaningfully better analytical quality than haiku for these judgment-heavy tasks while remaining fast. The `max_turns` cap prevents runaway subagents from burning tokens on edge cases. Before launching, substitute the Phase 0 values (analysis window, parser mode, taxonomy categories) into each prompt template's placeholders. Launch all 3 in a single message with `run_in_background: true` for maximum parallelism.

**Pass `unresolved_findings` to the Transcript Scanner.** The subagents are the only actors that actually read this window's transcripts, so they are the only ones who can confirm whether a prior finding recurred. Substitute the `unresolved_findings` array from `setup_context` into Subagent 1's prompt and instruct it to actively look for those patterns, reporting any it finds as normal findings under the same `category` key with fresh evidence. If this array never reaches a subagent, Phase 2 step 6a has nothing to match against and carry-forward is dead on arrival.

**Parallelism fallback and subagent prompts:** See [references/subagent-prompts.md](references/subagent-prompts.md) for the full fallback collection pattern and all 3 subagent prompt templates (Transcript Scanner, Memory Auditor, Workspace Scanner).

### Phase 1.5: Validate Subagent Outputs

Before synthesis, verify each subagent returned valid JSON with the expected top-level keys. If a subagent returned an error or malformed output, log it and proceed with the remaining outputs. At least 1 subagent must succeed to continue.

**Handoff interface (Phase 1.5 → Phase 2):**
```
collection_results: {
  transcript_findings: object[],
  memory_findings: object[],
  workspace_findings: object[],
  sessions_analyzed: number,
  sources_available: {transcripts: boolean, fingerprints: boolean, memory: boolean, workspace: boolean},
  subagent_errors: string[]
}
```

### Phase 2: Synthesis

After all subagents return and pass validation, synthesize their outputs:

1. **Merge findings** from all 3 subagents into a single list.
2. **Deduplicate**: if two findings describe the same pattern from different sources, merge them (combine evidence, keep the higher severity).
3. **Classify** each finding into a taxonomy category. If a finding doesn't fit, add to `uncategorized` and flag for taxonomy review.
4. **Score severity** using:
   - CRITICAL: causes data loss, sends wrong messages externally, or violates safety rules
   - HIGH: wastes significant time (10+ min), causes incorrect output, or frustrates the user visibly
   - MEDIUM: minor time waste (2-10 min), suboptimal behavior, or recurring annoyance
   - LOW: cosmetic, minor inefficiency, or isolated incident
5. **Score evidence quality**:
   - STRONG: 3+ instances across 2+ sessions with clear pattern
   - MODERATE: 2 instances or strong single instance with indirect corroboration
   - WEAK: 1 instance, inferred pattern, or low-confidence match
6. **Group related findings** that share a root cause.
6a. **Match carry-forward findings.** For each entry in `setup_context.unresolved_findings` (loaded in Phase 0), try to match it against the merged findings list from step 1. A match requires BOTH: (i) the `category` taxonomy key is identical, and (ii) the new finding describes the same underlying behavior — judge by root cause, not string equality, because `pattern` text is regenerated fresh each run and will rarely match byte-for-byte. On a match, mark the *new* finding as carried forward: set `times_carried = unresolved.times_carried + 1` and bump `current_severity` one level per carry (LOW→MEDIUM→HIGH), capping at HIGH. Add it to `carry_forward_findings`. If several unresolved entries and several new findings share a category, pair the highest-severity ones first. If an unresolved entry has no match in this window, drop it silently — do not re-emit it as a finding. **Without this step `carry_forward_findings` is never populated and the `[HIGH↑]` display in 3b, the severity-bump mechanic, and the entire "is this getting worse" signal quietly stop working while everything still looks like it ran.**
7. **Compute per-session friction scores** for the header sparkline: for each session, count `corrections + errors + retries` from transcript scanner data and cap at 10. The denominator is always 10 (not the max score observed). Store as an ordered array (oldest to newest). Compute the mean. Map each score to a block character: 0=▁, 1-2=▂, 3-4=▃, 5-6=▅, 7-8=▆, 9-10=█. Display as `avg <score>/10` (always /10, never /5 or any other denominator).
8. **Compute trend delta** (if `last-retro.json` was loaded in Phase 0): compare `findings_count`, severity distribution, and which categories are new vs resolved. Store as a structured delta for the header.
9. **Generate proposed actions** for each finding:
   - What to change (specific file, rule, memory entry, or behavior)
   - Auto-apply tier: AUTO_APPLY (internal skill state only), NEEDS_APPROVAL (any user file/memory), DISCUSS (ambiguous)
   - Alternatives with pros/cons
10. **Promote recurring corrections (3+ threshold):** For any Memory Auditor finding tagged as a promotion candidate (3+ occurrences of the same correction topic), generate a concrete promotion action:
   - Draft the rule text for the target file (your CLAUDE.md at user or project scope, or a specific skill/command)
   - Set auto-apply tier to NEEDS_APPROVAL (writes to user files)
   - Include the memory IDs to deduplicate/delete after promotion (the individual corrections become redundant once the pattern is codified)
   - Present in Phase 3 with a distinct `[PROMOTE]` tag so promotions are visually distinct from regular findings
11. **Execute AUTO_APPLY** actions (internal state only: rotate old log files, clean temp data from `/tmp/hindsight_*` if present from fallback mode, delete fingerprint files older than 90 days from `~/.claude/hindsight/fingerprints/`).

**Handoff interface (Phase 2 → Phase 3):**
```
synthesis_results: {
  findings: [{category: string, pattern: string, severity: string, evidence_quality: string, evidence: object[], impact: string, proposed_action: string, auto_apply_tier: string}],
  friction_scores: number[],
  friction_mean: number,
  trend_delta: {findings_delta: number, new_categories: string[], resolved_categories: string[]} | null,
  carry_forward_findings: object[]
}
```

### Phase 3: Report + Action (Interactive)

#### 3a. Logo + Stats Header

Open the report with this branded header. **All fields are mandatory** — fill every slot from the parsed arguments and Phase 2 synthesis data:

```
██╗  ██╗██╗███╗   ██╗██████╗ ███████╗██╗ ██████╗ ██╗  ██╗████████╗
██║  ██║██║████╗  ██║██╔══██╗██╔════╝██║██╔════╝ ██║  ██║╚══██╔══╝
███████║██║██╔██╗ ██║██║  ██║███████╗██║██║  ███╗███████║   ██║
██╔══██║██║██║╚██╗██║██║  ██║╚════██║██║██║   ██║██╔══██║   ██║
██║  ██║██║██║ ╚████║██████╔╝███████║██║╚██████╔╝██║  ██║   ██║
╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═════╝ ╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
        Cross-Session Retrospective

  Mode:     <neutral|--roast|--hype> <--human|--ai|both>
  Window:   <start> → <end>
  Sessions: <N> analyzed (<T> transcripts, <F> fingerprints), <M> skipped
  Sources:  Transcripts <✓|✗>  Fingerprints <✓|✗>  Memory <✓|✗>  Workspace <✓|✗>
  Findings: <N> total (<C> critical, <H> high, <M> med, <L> low)
  Friction: <sparkline> avg <score>/10 across sessions
```

**Trend comparison (when `last-retro.json` exists):** After the stats, show a one-line delta vs the previous run:
```
  vs last:  ▼ 2 fewer findings | severity ▼ (was 1 CRIT, now 0) | 3 resolved, 1 new
```
If this is the first run, show `  vs last:  (first run)` instead.

**Friction score:** Compute a per-session friction score (0-10) during Phase 2 synthesis: `min(10, corrections + errors + retries)` for each session. Show in the header as a sparkline using block characters (eg `▁▂▁▅▁▂▁`) and the mean. This answers "am I getting better?" at a glance without reading individual findings.

#### 3b. Findings Summary

Present all findings sorted by severity (CRITICAL first) using this EXACT format — 3 lines per finding, plain text, no box-drawing characters.

**Privacy reminder:** Apply the [Privacy Rules](#privacy-rules) to all finding descriptions.

**Category display rule:** `category` is ALWAYS stored as the taxonomy key (snake_case, one of the categories loaded in Phase 0 step 2). Never invent a key at storage time — a genuinely novel pattern goes to `uncategorized` with a suggested key. For display only, Title-Case the key with spaces: `unauthorized_actions` → `Unauthorized Actions`, `memory_staleness` → `Memory Staleness`. The display string is never written back to `last-retro.json`. If a freeform label reaches storage, the next run's carry-forward matching (Phase 2 step 6a, which compares taxonomy keys) silently fails to match it and the trend delta treats it as a brand-new category forever.

**Tone applies to every finding.** When a focus mode is active (`--human` or `--ai`), focus changes only the sort order — it does not soften the tone on deprioritized findings. A `--roast --human` run roasts the AI-side findings just as hard as the human-side ones; they simply appear further down.

```
  #1 [CRIT] Unauthorized Actions                   (4 incidents)
     AI posts externally without approval despite draft-confirm rules
     → Discuss: Verify rules are being loaded correctly

  #2 [HIGH↑] Memory Staleness                      (7 incidents)
     AI uses cached data instead of live checks; config files stale
     ↑ Carried from last retro (was MED, bumped after 2 runs)
     → Apply: Update stale config entries, move completed items to archive

  #3 [MED]  Rule Contradiction                     (5 incidents)
     AI works on wrong target when user references are ambiguous
     → Apply: Add clarification rule to workspace config

  #4 [LOW]  Output Neglect                         (3 incidents)
     Plans/docs too long or formal for target audience
     → Apply: Add audience-awareness check before generating docs
```

Format rules:
- **Line 1:** `#N [SEVERITY] Category Name` left-aligned, `(N incidents)` right-aligned. The category name is the Title-Cased taxonomy key — every label above maps to a real key.
- **Line 2:** One-sentence pattern description, indented
- **Line 3:** `→ Apply/Discuss/Skip: <action summary>`, indented
- Blank line between findings
- Mark AUTO_APPLY items with `✓ Auto-applied` on line 3
- **Carry-forward findings:** Append `↑` to severity tag (eg `[HIGH↑]`) and add an extra line: `↑ Carried from last retro (was <original>, bumped after <N> runs)`. This signals the pattern is persistent and getting worse.

#### 3c. Navigation (Interactive) or Auto-Apply (Auto Mode)

**If `auto=true` (--auto flag set):** Skip AskUserQuestion and the finding detail flow entirely. Execute the following instead:

1. Auto-apply all LOW and MEDIUM severity NEEDS_APPROVAL findings without prompting. For each applied finding, log: `[AUTO-APPLIED] #N [SEV] <category>: <action taken>`.
2. Collect all HIGH and CRITICAL severity NEEDS_APPROVAL findings. Append them to `~/.claude/state/overnight-flags.md` under a `## Hindsight` section (create the section if not present; do not overwrite existing content in the file — append only):
   ```markdown
   ## Hindsight
   - [ ] [HIGH] <category>: <pattern> — <proposed_action>
   - [ ] [CRIT] <category>: <pattern> — <proposed_action>
   ```
3. Collect all DISCUSS-tier findings, at any severity, and append them to the same `## Hindsight` section as `- [ ] [DISCUSS] <category>: <pattern> — <proposed_action>`. Set their `disposition` to `"discussed"`. DISCUSS exists precisely because a finding is ambiguous and needs a human; `--auto` is the one mode where no human is watching, so a DISCUSS finding that is neither applied nor flagged here vanishes silently.
4. Write the full report to `~/.claude/hindsight/reports/YYYY-MM-DD-hindsight.md` (same as the Level 3 "Full report" path in 3e). Do not wait for user input.
5. Proceed directly to 3e wrap-up with `applied = <count of auto-applied>`, `skipped = 0`, `discussed = <count of DISCUSS-tier plus HIGH/CRIT findings flagged to overnight-flags.md>`.

**If `auto=false` (interactive mode, default):** Follow the full interactive flow below.

**You MUST call the AskUserQuestion tool after presenting findings — unless it is genuinely unavailable in this tool context, which is the only exception (see Fallback below).** Do not print options as text because it is faster. Do not present a numbered list out of convenience. Do not skip this step. Do not proceed to detail views without calling AskUserQuestion first. If you find yourself typing "1.", "2.", "3." as navigation options while the tool IS available, STOP and use AskUserQuestion instead. This is the primary interactive mechanism and is required for the pipeline to function correctly.

After presenting the header and findings summary, use AskUserQuestion to let the user navigate interactively. AskUserQuestion provides clickable options which is a better UX than plain text.

```
Question: "What would you like to do?"
Header: "Hindsight"
Options:
  - "Review top finding" / "Drill into the highest-severity unreviewed finding"
  - "Apply all safe" / "Preview and apply all NEEDS_APPROVAL items at LOW/MED severity (shows summary of planned changes before executing)"
  - "Full report" / "Write complete report to ~/.claude/hindsight/reports/"
  - "Done" / "Finish hindsight, save state, skip remaining"
```

**Fallback:** If AskUserQuestion is unavailable (eg running inside a subagent or restricted tool context), present the same options as a numbered text list and ask the user to type their choice. The interactive tool is preferred but not required for the pipeline to function.

#### 3d. Finding Detail Flow (interactive mode only — skip if `auto=true`)

When the user selects a finding to review, show:

```
  ┌─────────────────────────────────────┐
  │ #N [SEVERITY] Category              │
  └─────────────────────────────────────┘
  Pattern: <description>
  Evidence: <N> incidents across <M> sessions

  • <date> — <paraphrased description>
  • <date> — <paraphrased description>
  • <date> — <paraphrased description>

  Impact: <what this costs>
  Related: #X, #Y (shared root cause)
```

Then use AskUserQuestion for the action (or text fallback if the tool is unavailable):

```
Question: "What should we do about this?"
Header: "Action"
Options:
  - "Apply fix" / "<specific action description>"
  - "Skip" / "Acknowledge but take no action"
  - "Discuss" / "I have questions or a different idea"
  - "Next finding" / "Move to next unreviewed finding"
```

After the user selects an action, automatically present the next unreviewed finding (don't make the user navigate back to the menu).

#### 3e. Wrap-up

When the user selects "Done" or all findings are processed:

1. Show a final summary:
```
  ◀ Hindsight Complete ▶
  Applied: <N> changes
  Skipped: <M> findings
  Discussed: <K> items
```

2. Write both output files in parallel (issue both Write calls in a single message):
   - `~/.claude/hindsight/last-retro.json`:
```json
{
  "timestamp": "<ISO 8601>",
  "window": {"start": "<date>", "end": "<date>"},
  "sessions_analyzed": <count>,
  "sessions_from_transcripts": <count>,
  "sessions_from_fingerprints": <count>,
  "findings_count": <count>,
  "actions_applied": <count>,
  "actions_skipped": <count>,
  "friction_scores": [<per-session scores, oldest to newest>],
  "friction_mean": <average friction score>,
  "mode": "<neutral|--roast|--hype> <--human|--ai|both>",
  "all_findings": [
    {
      "category": "<taxonomy category key>",
      "pattern": "<plain-language description>",
      "severity": "LOW|MEDIUM|HIGH|CRITICAL",
      "evidence_count": <number of incidents>,
      "evidence_sessions": <number of distinct sessions>,
      "evidence_items": [
        {"date": "<YYYY-MM-DD>", "description": "<paraphrased, no filesystem paths>"}
      ],
      "impact": "<what this costs>",
      "disposition": "applied|skipped|discussed",
      "related_findings": [<indices of related findings>]
    }
  ],
  "unresolved_findings": [
    {
      "category": "<category>",
      "pattern": "<description>",
      "original_severity": "<severity when first found>",
      "current_severity": "<severity after carry-forward bumps, computed as original + times_carried levels, capping at HIGH>",
      "first_seen": "<ISO 8601 of hindsight that first found it>",
      "times_carried": <number of hindsight runs this has been carried forward>
    }
  ]
}
```

The `all_findings` array captures every finding with its final disposition, evidence (capped at 3 items per finding), and impact. Evidence items use dates only (no filesystem paths). This array is the data source for Phase 3f visual reports.
```
   - `~/.claude/hindsight/logs/YYYY-MM-DD-changes.md` (changelog)

The `unresolved_findings` array contains findings the user skipped. On the next hindsight, these are loaded in Phase 0 step 1 and re-checked. If the pattern recurs, severity bumps one level (LOW→MEDIUM, MEDIUM→HIGH), capping at HIGH — carry-forward findings never become CRITICAL, since CRITICAL is reserved for genuine safety/data-loss issues, not persistence. If not found in the new window, the finding is dropped silently.

3. Verify both files were written by reading the first line of each (issue both Read calls in a single message).

**Level 3 — Full report (on user request):**

Write `~/.claude/hindsight/reports/YYYY-MM-DD-hindsight.md` containing:
- Executive summary with the branded header
- All findings with full evidence
- Taxonomy maintenance section (proposed new categories, merge suggestions)
- Trend comparison with previous hindsight (if exists)

#### 3f. Visual Report (only if --viz flag is set)

**STOP. You MUST read [references/visual-report.md](references/visual-report.md) before executing this phase.** It contains the full HTML generation spec, inline CSS, local save flow, and validation steps.

## Error Handling

- **No cclog.py**: Use fallback parser mode. Tell user: "Install session-history skill for robust parsing."
- **0 sessions**: Detect this in Phase 0.5 (before launching subagents). Show: "No sessions found in the last `<window>`. Try widening: `/hindsight window=30d`". Do NOT proceed to Phase 2/3 with empty data — exit cleanly. Do NOT write `last-retro.json` (nothing to persist).
- **No memory DB**: Skip Memory Auditor. Note in output.
- **No workspace files**: Skip Workspace Scanner. Note in output.
- **Subagent failure**: Proceed with remaining subagent outputs. Note which source was unavailable.

## Context Compaction Protection

This workflow runs 15+ minutes with subagents. After context compaction:
1. Re-invoke the hindsight skill to reload this file (works for both plugin and local installs; the SKILL.md location is resolved by the skill loader, not a hardcoded path)
2. Re-read the workflow state file (`/tmp/workflow-${RUN_ID}.json`) to determine current step
3. Re-read persisted intermediate results from `/tmp/hindsight_*.json` (fingerprints, sessions, memory, workspace manifest)
4. Re-read `~/.claude/hindsight/last-retro.json` from disk. This is unconditional — do not reuse values you believe you already know. After compaction you have no memory of prior turns, so anything that "feels" already loaded is a reconstruction, not data.
5. Resume from the first non-done step in the state file — do not restart from Phase 0

**STOP. You MUST re-read this file after any context compaction.** The workflow state file tracks which phases completed. Skip done phases and resume from the first pending or in-progress step.

## Privacy Rules

- Read everything including natural language frustration
- Report behavioral patterns, not conversation topics
- Never externalize reports to external services or shared storage. Visual reports (`--viz`) are written to the local filesystem only.
- No verbatim content quoting in terminal output (findings summary or detail views). Full reports written to file (`~/.claude/hindsight/reports/`) may include brief paraphrases.
- Skip content about personal matters, HR, health, or private discussions entirely
- **`--viz` reports are local only:** HTML reports generated by `--viz` are written to your local reports directory and never uploaded anywhere. They contain only behavioral pattern summaries (paraphrased) and aggregate statistics, never verbatim conversation content, filesystem paths, or personal/health content. Evidence items use dates only.
