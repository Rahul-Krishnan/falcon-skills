---
name: context-contradiction-checker
description: "Detect contradictory, inconsistent, or duplicated instructions across all active Claude Code context sources (CLAUDE.md files, .llms/rules, skills, hooks). Use when asked to check for contradictions in context, find inconsistencies across rules, or audit Claude setup for contradictory instructions. Do not use to analyze code files or non-instruction documentation. Not a linter or code reviewer."
metadata:
  user-invocable: true
  allowed-tools: "Read, Glob, Grep, Bash(echo:*, cat:*, ls:*, python3:*), Edit, Write"
---

# Context Contradiction Checker

Detect contradictions and inconsistencies across all context sources loaded into the current Claude Code session.

## Arguments

| Argument | Count | Mode | Error if invalid |
|----------|-------|------|-----------------|
| (none) | 0 args | Full scan -- discover all active context sources | -- |
| `{file}` | 1 arg | Single-file check -- internal contradictions only | If path does not exist: "File not found: {path}. Check the path and retry." |
| `{file_a} {file_b} [...]` | 2+ args | Targeted comparison -- compare only the specified files | If any path does not exist: "File not found: {path}. Check the path and retry." |
| `--auto` | flag | Non-interactive mode -- run Steps 1-3, output report, stop (no fix menu) | If combined with any file path argument: "`--auto` cannot be combined with file path arguments. Run `/context-contradiction-checker --auto` for a full scan, or drop the flag to compare the named files." |

**Mode detection**: count the number of file path arguments provided. Do not scan additional files beyond those specified in targeted or single-file modes. If `--auto` flag is present: record `"auto": true` in the state file (see Workflow State), run the full workflow (Steps 1-3), and stop after presenting the report — do not present the Step 4 fix menu. `--auto` is orthogonal to mode: it is a separate state field, not a fourth value of `mode`, so it must be written to state at invocation or a compacted run resumes as an interactive one and prompts a user who is not there.

**Argument validation runs before the state file is written.** Validate every supplied path first; write the state file only once the arguments are known good. A run that fails validation leaves no state file behind, so a retry always starts clean.

**Partial path failure (explicit):** In targeted or single-file mode, a missing path **aborts the run**. Do not fall back to comparing the surviving files, and do not degrade a 2-file comparison into a single-file check — the user named a specific comparison, and silently performing a different one produces a findings report that answers a question they did not ask. If several supplied paths are missing, list **all** of them in one error rather than failing on the first:

```
File not found: ~/.claude/does-not-exist.md
File not found: ./also-missing.md
Check the paths and retry.
```

This is deliberately stricter than full-scan mode, which skips missing files silently. The difference is intent: in a full scan the path list is *inferred* and absence is unremarkable, whereas in targeted mode the path list is *asserted by the user* and absence means their mental model is wrong.

## Modes

- **Full scan** (no file arguments): Discover and read all active context sources.
- **Targeted comparison** (2+ file paths): Read only the specified files and compare them.
- **Single-file check** (1 file path): Check for internal contradictions within that file only.
- **Auto mode** (`--auto`): Non-interactive. Complete Steps 1-3, output findings report, and stop. Do not present the Step 4 fix menu.

## Workflow State

At invocation start, write `/tmp/contradiction-check-<session-id>.json`, where `<session-id>` is the current Claude Code session ID (`echo $CLAUDE_CODE_SESSION_ID`):
```json
{
  "mode": "full|targeted|single-file",
  "auto": false,
  "step": 1,
  "files_found": [],
  "directives": [],
  "issues_found": [],
  "gates": []
}
```
The filename keys on the session ID, not the wall clock, so a resuming model can always reconstruct it. The session ID is stable across compaction; a timestamp is not.

Set `"auto"` to `true` when the `--auto` flag was passed. Re-read this state file at the start of each step (before reading any other files) to ensure progress survives context compaction. Update `"step"` after completing each step. After Step 1 completes, write the full directives list to `"directives"` (each entry `{text, source_file, topic}`) — persist the list itself, not a count, because Step 2 consumes it. After Step 2 completes, update `"issues_found"` with the full issues list.

## Handoff Validation (deterministic)

The typed interfaces declared at each step boundary are enforced by a bundled validator, not by self-inspection. A schema nobody runs is documentation; a schema the gate runs is a contract.

**Resolve the skill directory once, at invocation start**, and reuse `$CCC_DIR` at every call site below. This skill runs both as an installed plugin and as a local skill, and the validator lives in a different place in each. `CLAUDE_PLUGIN_ROOT` is set only for plugins, so hardcoding either path breaks the other:

```bash
CCC_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/context-contradiction-checker}"
CCC_DIR="${CCC_DIR:-$HOME/.claude/skills/context-contradiction-checker}"
```

At each gate, write the handoff payload into the state file **under a key named for the handoff** (`step1_to_step2`, `step2_to_step3`, `step3_to_step4`) alongside the flat top-level fields, then run:

```bash
python3 "$CCC_DIR/scripts/validate_handoffs.py" \
  /tmp/contradiction-check-<session-id>.json --handoff step1_to_step2 --json
```

Interpret the exit code:

| Exit | Meaning | Action |
|------|---------|--------|
| 0 | Handoff matches the schema | Proceed to the next step |
| 1 | Schema violation (missing/malformed field) | **CRITICAL_STOP.** Report the validator's error text verbatim. Do not proceed on invalid state — a later step consuming malformed handoff data produces a findings report built on garbage. |
| 2 | Usage error (bad path, unknown handoff name) | Tooling fault, not a data fault. Report it and stop; do not retry with different arguments. |

Use `--handoff workflow_state` to validate the top-level envelope right after the initial write. The schemas in `scripts/validate_handoffs.py` mirror this file's state shape exactly; if you ever change the state shape here, change the schemas there in the same edit, or the validator will start rejecting correct state.

**Context compaction recovery:** If context is compacted or prior step outputs are unavailable:

1. **Re-read this file** (`$CCC_DIR/SKILL.md`, resolved as above) first. After compaction you have no reliable memory of the workflow itself, only of having been in one — so recover the instructions before acting on them.
2. Re-read the state file at `/tmp/contradiction-check-<session-id>.json` (reconstruct the path from `echo $CLAUDE_CODE_SESSION_ID`).
3. Resume from the step named in `"step"`.

The state file is the sole source of truth for `mode`, `auto`, `files_found`, `directives`, `issues_found`, and `gates` — every input a later step needs is in it, so no step ever has to re-run an earlier one. If the file is missing: stop with "Workflow state lost — re-run /context-contradiction-checker to start fresh."

## Step 1: Discover Context Sources

**Produces:** `files` (list of found paths) and `directives` (list of {text, source_file, topic}).

**Output constraint:** Steps 1 and 2 emit no narration. Do not narrate file reads, directive extraction, state file writes, or gate evaluations to the user. The Step 1 discovery summary below is the one exception — it is a deliverable, not narration, and you always present it. Otherwise begin output only when presenting the Step 3 findings report, a stop/error message, or the Step 4 fix menu. **Exception — compaction resume:** If context was compacted and you are resuming mid-run, first announce: `Resuming from Step [N]: [step-name] — [what was already completed per state file].` Then continue.

Scan these locations (read in parallel, skip missing):

| Source Type | Paths to Check |
|-------------|---------------|
| Project CLAUDE.md | `./CLAUDE.md`, `./.claude/CLAUDE.md` |
| User CLAUDE.md | `~/.claude/CLAUDE.md` |
| LLM Rules | `~/.llms/rules/*.md`, `./.llms/rules/*.md` |
| Workspace files | `~/.claude/*.md` (any other instruction files you keep there, eg `rules.md`, `memory.md`) |
| Skills | `~/.claude/skills/*/SKILL.md`, `./.claude/skills/*/SKILL.md` |
| Hooks | `~/.claude/hooks/*`, `./.claude/hooks/*` (hooks are often `.py` or `.js`, not just `.sh`), plus any script invoked by path from a settings file |
| Settings | `~/.claude/settings.json`, `~/.claude/settings.local.json`, `./.claude/settings.json`, `./.claude/settings.local.json` — extract: hook definitions (what triggers each hook), allowed/denied tool patterns, and any behavioral flags |

Each row maps to exactly one layer in the discovery summary below: Project + User CLAUDE.md → **CLAUDE.md hierarchy**; LLM Rules → **Rules**; Workspace files → **Workspace**; Skills → **Skills**; Hooks → **Hooks**; Settings → **Settings**. Six layers, not seven — do not invent a row.

Project-level sources matter most: a project rule that overrides a user rule is where the interesting collisions live.

### What counts as a directive

Extract only text that **instructs Claude's behavior at runtime**. This scoping is what separates a useful report from a wall of noise.

**Do extract:** imperative rules ("Always use `trash`, never `rm`"), prohibitions, tool-usage constraints, formatting requirements, workflow mandates.

**Do NOT extract:**

| Not a directive | Why |
|-----------------|-----|
| A skill's frontmatter `description:` field | This is **routing metadata** — it tells the orchestrator when to *select* the skill, not how Claude should behave. Descriptions are *required* to carry "Do NOT use for X (use Y instead)" scoping language, so a naive scan reads dozens of them as prohibitions and reports every pair of adjacent skills as a CONFLICT. They are boundaries, not contradictions. |
| A skill's internal workflow steps | Instructions to *that skill's* executor while it runs, not standing rules for the session. Two skills prescribing different step orders is not a contradiction; they are different procedures. |
| Prose, rationale, examples, and sample output | Illustrative, not binding. A code block demonstrating a bad pattern is not an instruction to use it. |

The test: **would this line still bind Claude if the skill were never invoked?** If no, it is not a directive. Only a skill's session-wide overrides — the rules it declares for *all* work, not for its own execution — qualify.

### Scale (full scan only)

A real machine can hold 40+ skills; reading every `SKILL.md` body can exceed 500KB and will exhaust the context window mid-run. Bound the read, and bound it *before* reading rather than discovering the problem at 90% context:

1. Glob the paths first and count them. Report the count in the discovery summary.
2. **Read CLAUDE.md hierarchy, Rules, Workspace, Settings, and Hooks in full** — these are small and are where genuine cross-cutting conflicts live.
3. **For Skills, read only the frontmatter block and any explicit session-wide override sections** (headings matching `Memory`, `Rules`, `Overrides`, `Always`, `Never`). Skip skill bodies. Per the scoping table above, a skill body's step-by-step instructions are not directives anyway, so reading them costs context and yields nothing.
4. If the extracted directive count exceeds **300**, stop extracting and report: `Directive budget reached (300). Scanned [N] of [M] files — narrow the scan with explicit file paths for a complete analysis.` A truncated scan that says so is honest; a truncated scan that stays quiet reports "all consistent" about files it never opened.

Reads remain parallel — this bounds *what* is read, not how many reads run at once.

**Deduplicate the path list before reading.** The rows above overlap by design — `~/.claude/CLAUDE.md` matches both the User CLAUDE.md row and the workspace glob. Resolve every path (expand `~`, follow symlinks) and read each unique file exactly once. A file read twice yields every one of its directives twice, and Step 2 then reports a perfect OVERLAP between the file and itself.

**Error handling:** If a file exists but cannot be read (permission error, binary, etc.), skip it and note: `[skipped: {path} — unreadable]`. If a settings file is malformed JSON, skip that file's settings extraction and note `[skipped: {path} — invalid JSON]`.

Read all found files. Present discovery summary (this is the one output Steps 1-2 produce):

```
Scanned [N] files across [M] layers. Extracted [X] candidate directives.

| Layer | Files | Candidates |
|-------|-------|------------|
| CLAUDE.md hierarchy | N | N |
| Rules | N | N |
| Workspace | N | N |
| Skills | N | N |
| Hooks | N | N |
| Settings | N | N |
```
Omit any layer row where no file was found.

**Before presenting, check the table against itself:** the `Files` column must sum to `[N]` and the `Candidates` column must sum to `[X]`. If they disagree, the arithmetic is wrong — fix it, do not present it. A contradiction-detection tool that ships an internally inconsistent summary table has failed at its own premise.

**Targeted and single-file modes** use the same table with a single row, labeled by the layer the named files belong to (`CLAUDE.md hierarchy` for a `CLAUDE.md`, `Workspace` for `~/.claude/rules.md`, etc.). Do not invent a "Targeted comparison" layer — the layer names are fixed by the mapping above.

**Gate — Step 1 → Step 2:**

**Judge**: current session (automated — checks file and directive counts)

| Check | Severity | Pass condition |
|-------|----------|---------------|
| At least 1 file found | CRITICAL | 0 files = output "No context files found. Nothing to scan." and stop |
| At least 1 directive extracted | HIGH | 0 directives = output "No issues found. Scanned [N] files, extracted 0 directives — all consistent." and stop |

**Iteration schema**: CRITICAL stop is final — do not retry discovery. HIGH stop is final — no directives means nothing to analyze.

**State update:** Write `files_found` and the full `directives` list to the state file (both at top level and under a `step1_to_step2` key), then append to `gates[]`: `{"gate": "Step 1 → Step 2", "result": "PASS|HIGH_STOP|CRITICAL_STOP", "ts": "<ISO-8601>"}`.

**Handoff interface (Step 1 → Step 2):**
```json
{
  "files_found": ["string"],
  "directives": [{"text": "string", "source_file": "string", "topic": "string"}]
}
```
Required: both fields present as non-empty arrays, and both persisted to the state file under those exact keys. Missing either = CRITICAL gate failure.

**Validate before proceeding** (see [Handoff Validation](#handoff-validation-deterministic)):
```bash
python3 "$CCC_DIR/scripts/validate_handoffs.py" \
  /tmp/contradiction-check-<session-id>.json --handoff step1_to_step2 --json
```
Exit 1 = CRITICAL_STOP. Do not enter Step 2 on invalid handoff data.

## Step 2: Analyze

**Consumes (required):** `files_found` (list of paths from Step 1) and `directives` (list of {text, source_file, topic} from Step 1) — read both from the state file, which holds them whether or not Step 1 is still in context. Missing either = stop: "Step 2 cannot run — Step 1 output missing."
**Produces (required):** `issues` — list of {id, type, confidence, topic, directive_a, source_a, directive_b, source_b, resolution}. Empty list is valid (means no issues found).

Work from the `directives` list in state (Step 1 already extracted and scoped it — do not re-extract from raw files). Group directives by topic: code-style, tool-usage, workflow, language-specific, communication, safety, architecture, other.

Compare directives **pairwise within each topic group**. Two directives on different topics cannot conflict, so cross-topic pairs are not candidates.

### Classifying a pair

Apply these tests in order. The first one that matches decides the type.

1. **Do the two directives command the same action on the same subject?** → **OVERLAP**. The rule is stated twice, wasting context. (Example: "Use `trash` instead of `rm`" in your user CLAUDE.md and "Always use `trash <path>` instead of `rm`" in your rules file.)
2. **Do they command opposed actions on the same subject, with neither scoped by a condition?** → **CONFLICT**. Both claim to govern the same situation and cannot both be obeyed. (Example: "Always use tabs" vs "Always use spaces".)
3. **Do they command opposed actions, but one is scoped to a subset of cases the other governs?** → **TENSION** by default. An exception carved out of a general rule is how policy normally works; it is confusing, not incoherent. (Example: "Always run tests before committing" vs "Skip tests for documentation-only changes" — the second is a carve-out, not a rebuttal.)
   - **Escalate to CONFLICT** if the general rule explicitly forecloses exceptions — "No exceptions", "under any circumstances", "never, ever". An absolute that admits no carve-out plus a carve-out is a genuine contradiction, and the emphatic wording is what makes it one.
4. **Do they pull in opposed directions without commanding opposed actions?** → **TENSION**. (Example: "Be concise" vs "Always explain your reasoning in detail".)

If none match, it is not an issue. Do not report it.

### Scoring confidence

Confidence measures how sure you are that the pair is *really* the type you assigned — not how strongly you feel about it. Start from the band and adjust:

| Band | Condition |
|------|-----------|
| 90-100% | Same subject, unambiguous wording, and the two directives cannot both be satisfied by any single action. |
| 70-89% | Same subject and clearly opposed, but one is conditional, scoped, or its wording admits more than one reading. |
| 50-69% | The subjects may not be the same, or resolving the pair depends on context the files do not supply. |
| Below 50% | Speculative. **Exclude from the report** unless the user explicitly asked for all findings. |

Two directives that merely *sound* similar are not an OVERLAP; two that address adjacent-but-distinct subjects are not a CONFLICT. When you cannot decide between two bands, take the lower one — a false positive costs the user more than a missed low-confidence finding, because it teaches them to distrust the whole report.

**Gate — Step 2 → Step 3:**

**Judge**: current session (automated — checks issues list and confidence scores)

| Check | Severity | Pass condition |
|-------|----------|---------------|
| `issues` field populated (list produced) | CRITICAL | missing = stop: "Step 2 produced no output — analysis failed" |
| At least 1 issue with confidence >= 70% | HIGH | 0 qualifying issues: update state (`step=2`, `issues_found=[]`), output zero-findings message, stop immediately. Do NOT proceed to Step 3 or Step 4. Do NOT present the fix menu. |

**Iteration schema**: CRITICAL failure = stop. HIGH stop is final — zero findings is a valid result, not an error.

**State update:** Append to `gates[]`: `{"gate": "Step 2 → Step 3", "result": "PASS|HIGH_STOP|CRITICAL_STOP", "ts": "<ISO-8601>"}`. On HIGH stop, also write `issues_found: []` to state.

**Handoff interface (Step 2 → Step 3):**
```json
{
  "issues": [{"id": "string", "type": "CONFLICT|TENSION|OVERLAP", "confidence": "number (0-100)", "topic": "string", "directive_a": "string", "source_a": "string", "directive_b": "string", "source_b": "string", "resolution": "string"}]
}
```
Required: `issues` array (empty array valid — handled by HIGH gate above).

**Validate before proceeding.** Write the payload under a `step2_to_step3` key in state, then:
```bash
python3 "$CCC_DIR/scripts/validate_handoffs.py" \
  /tmp/contradiction-check-<session-id>.json --handoff step2_to_step3 --json
```
Exit 1 = CRITICAL_STOP. The schema enforces `type` ∈ {CONFLICT, TENSION, OVERLAP} and `confidence` ∈ [0, 100], so a malformed classification is caught here rather than surfacing in the report.

## Step 3: Report

**Consumes (required):** `issues` (list from Step 2, filtered to confidence >= 70%). Missing = stop: "Step 3 cannot run -- issues list missing."
**Produces (required):** Formatted findings report shown to user.

If `issues` list is empty at this point (defensive check — gate should have caught this): output zero-findings message and stop.

Present findings filtered to confidence >= 70%:

```
Found [N] issues across [M] files:

  #1 [CONFLICT] (95%) code-style
     File A: "Use tabs for indentation"
     File B: "Use 2-space indentation"
     → Resolution: Pick one, remove the other

  #2 [TENSION] (75%) workflow
     File A: "Always run tests before committing"
     File B: "Skip tests for documentation-only changes"
     → Resolution: Add exception clause to File A

  #3 [OVERLAP] (80%) tool-usage
     File A: "Use Grep for searching"
     File B: "Always use Grep instead of bash grep"
     → Resolution: Keep the more specific one, remove the other
```

**Zero findings:** If no issues meet the confidence threshold (>= 70%), output:

```
No issues found. Scanned [N] files, extracted [X] directives — all consistent.
```

**Gate -- Step 3 -> Step 4:**

**Judge**: current session (automated -- verifies report was presented and issues are non-empty)

| Check | Severity | Pass condition |
|-------|----------|---------------|
| Findings report presented to user | CRITICAL | not presented = stop |
| `issues` list non-empty | HIGH | empty = go directly to "Done" without prompting for menu |

**Iteration schema**: CRITICAL failure = stop. HIGH: skip menu and exit.

**State update:** Append to `gates[]`: `{"gate": "Step 3 → Step 4", "result": "PASS|HIGH_STOP|CRITICAL_STOP", "ts": "<ISO-8601>"}`. On HIGH (empty issues): stop without presenting fix menu.

**Handoff interface (Step 3 → Step 4):**
```json
{
  "issues": [{"id": "string", "type": "string", "confidence": "number"}],
  "report_presented": true
}
```
Required: `report_presented` must be `true`. Empty `issues` = HIGH gate stop (no menu).

**Validate before proceeding.** Write the payload under a `step3_to_step4` key in state (including `report_presented: true`), then:
```bash
python3 "$CCC_DIR/scripts/validate_handoffs.py" \
  /tmp/contradiction-check-<session-id>.json --handoff step3_to_step4 --json
```
Exit 1 = CRITICAL_STOP. Skip this validation when `auto` is `true` — there is no Step 4 to hand off to.

## Step 4: Fix (Interactive)

**Consumes (required):** `issues` list from Step 3. **Consumes (optional):** user menu selection (1-4). If user provides no input or invalid input (not 1-4), re-prompt once: "Please enter 1, 2, 3, or 4." If still invalid, default to option 3 (Report only -- no changes) and note: "Defaulting to report-only mode."
**Produces (required):** File edits applied and confirmed (or "no changes" if user chose option 3/4).

**Auto mode:** If `"auto"` is `true` in the state file, skip this step entirely. The Step 3 report is the final output. Read the flag from state, not from memory of the invocation — after a compaction, state is the only place it survives.

After presenting findings, ask the user:

```
How would you like to proceed?
  1. Fix all overlaps — remove duplicates, keep the more specific version
  2. Review conflicts one by one — present each with resolution options
  3. Report only — no changes
  4. Done
```

Wait for the user's reply before taking any action.

**Option 2 — Review conflicts one by one:** For each conflict/tension, present:

```
Conflict #[N] of [total] — [TYPE] ([confidence]%) [topic]

  [file_a]: "[directive_a]"
  [file_b]: "[directive_b]"

  How would you like to resolve this?
    A. Keep [file_a] rule — remove [file_b] rule
    B. Keep [file_b] rule — remove [file_a] rule
    C. Keep both — mark as intentional
    D. Skip this conflict
```

Wait for user reply before proceeding to the next conflict.

For each fix:
1. Read the target file (required before Edit).
2. Edit the file using the Edit tool.
3. Re-read to confirm persistence.
4. Show the before/after diff inline.

**Error handling:** If an Edit fails (e.g., string not found, file changed), report: `[Edit failed for {file}: {reason}. Skipping this fix — please edit manually.]` and continue to the next issue.
