---
name: context-contradiction-checker
description: "Detect contradictory, inconsistent, or duplicated instructions across all active Claude Code context sources (CLAUDE.md files, .llms/rules, skills, hooks). Use when asked to check for contradictions in context, find inconsistencies across rules, or audit Claude setup for contradictory instructions. Do not use to analyze code files or non-instruction documentation. Not a linter or code reviewer."
metadata:
  user-invocable: true
  allowed-tools: "Read, Glob, Grep, Bash(echo:*, cat:*, ls:*), Edit, Write"
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

**Context compaction recovery:** If context is compacted or prior step outputs are unavailable, re-read the state file immediately. The state file is the sole source of truth for `mode`, `auto`, `files_found`, `directives`, `issues_found`, and `gates` — every input a later step needs is in it, so no step ever has to re-run an earlier one. If the file is missing: stop with "Workflow state lost — re-run /context-contradiction-checker to start fresh."

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

Project-level sources matter most: a project rule that overrides a user rule is where the interesting collisions live.

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

**Gate — Step 1 → Step 2:**

**Judge**: current session (automated — checks file and directive counts)

| Check | Severity | Pass condition |
|-------|----------|---------------|
| At least 1 file found | CRITICAL | 0 files = output "No context files found. Nothing to scan." and stop |
| At least 1 directive extracted | HIGH | 0 directives = output "No issues found. Scanned [N] files, extracted 0 directives — all consistent." and stop |

**Iteration schema**: CRITICAL stop is final — do not retry discovery. HIGH stop is final — no directives means nothing to analyze.

**State update:** Write `files_found` and the full `directives` list to the state file, then append to `gates[]`: `{"gate": "Step 1 → Step 2", "result": "PASS|HIGH_STOP|CRITICAL_STOP", "ts": "<ISO-8601>"}`.

**Handoff interface (Step 1 → Step 2):**
```json
{
  "files_found": ["string"],
  "directives": [{"text": "string", "source_file": "string", "topic": "string"}]
}
```
Required: both fields present as non-empty arrays, and both persisted to the state file under those exact keys. Missing either = CRITICAL gate failure.

## Step 2: Analyze

**Consumes (required):** `files_found` (list of paths from Step 1) and `directives` (list of {text, source_file, topic} from Step 1) — read both from the state file, which holds them whether or not Step 1 is still in context. Missing either = stop: "Step 2 cannot run — Step 1 output missing."
**Produces (required):** `issues` — list of {id, type, confidence, topic, directive_a, source_a, directive_b, source_b, resolution}. Empty list is valid (means no issues found).

For each file, extract directive lines (instructions to Claude, rules, behavioral constraints). Skip prose, documentation, and examples.

Group directives by topic: code-style, tool-usage, workflow, language-specific, communication, safety, architecture, other.

For each topic group, scan holistically for:

- **CONFLICT** (highest): Two directives directly oppose each other.
  - Example: "Always use tabs" vs "Always use spaces"
- **TENSION** (medium): Not contradictory but could cause confusion.
  - Example: "Be concise" vs "Always explain your reasoning in detail"
- **OVERLAP** (lowest): Two directives say the same thing. Wastes context window.
  - Example: Same rule stated in your user CLAUDE.md and your project CLAUDE.md

Score confidence (0-100):
- 90-100%: clearly contradictory
- 70-89%: likely contradictory
- 50-69%: possibly contradictory
- Below 50%: unlikely (exclude from output unless user asked for "all findings")

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
