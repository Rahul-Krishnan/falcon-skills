---
name: unslop-code
description: "Detect and roast AI code slop - redundant, unreadable, or unnecessarily complex code patterns. Focuses on stupid comments, sloppy tests, over-abstraction, and repetitive code that makes codebases painful to maintain. Do NOT use for security audits, performance profiling, architectural review, or linting — this skill only finds surface-level AI code quality issues."
metadata:
  user-invocable: true
  argument-hint: "[files-or-dirs] [--auto|--auto-fix] [--review]"
  allowed-tools: "Read, Glob, Grep, Bash(git:*, npm:*, pnpm:*, yarn:*, pytest:*, go:*, cargo:*, tsc:*, npx:*, ruff:*, mypy:*, rm:*, cat:*, ls:*, echo:*), Edit, Write, AskUserQuestion"
---

# Unslop Code

Scan code for AI-generated slop and roast it accordingly. No mercy for tutorial comments, vacuous tests, or "enterprise" abstractions for 3-line scripts.

## Auto Mode

**`--auto`**: Run fully non-interactive. Skip the fix menu (Step 4) and default to option 4 (Report only). In Step 1, if the git command fails or returns empty and no specific target was given, output "No files to scan — exiting." and stop (do NOT call AskUserQuestion). Produce the full report without applying fixes or waiting for user input.

**`--auto-fix`**: Non-interactive AND applies fixes. Skip the Step 4 menu and behave as option 1 (Fix all), then run the Step 5 behavior gate. Use this when an automated caller needs slop actually removed, not just reported; plain `--auto` is report-only and applies nothing. Gate-exempt edits (plain-comment deletions — see the exemption rules in Step 5) apply directly; everything else — code removals, docstring or directive edits, runtime-string rewrites — is gated by Step 5 and reverted if the behavior check fails. Same empty/failed-diff exits as `--auto`. Report `fixes_applied` truthfully in the output.

## Review Mode

**`--review`**: Reviewer-only pass over cleanup that was already drafted. The same pass must never both apply slop fixes and approve them; `--review` is the separate check.

When `--review` is set:
1. **Do NOT edit any file.** This mode reads only.
2. Take the target as a cleanup to audit (a diff, a branch, or the listed changed files). Inspect what the cleanup removed or rewrote and check specifically for:
   - **Over-removal:** deleted code that was actually used, not dead (verify before trusting a "dead code" deletion).
   - **Behavior drift:** a "simplification" that changed what the code does.
   - **Leftover slop:** tells the cleanup missed (run Step 2 scan over the result).
   - **Weak verification:** preserved behavior with no test covering it.
3. Produce a **verdict** (`APPROVE` / `CHANGES NEEDED`) with the required follow-ups listed.
4. Hand needed changes back to a normal writer pass. Do not fix-and-approve in one step.

`--review` and `--auto` compose: `--review --auto` runs the reviewer verdict non-interactively.

## Workflow State

At start, write `${TMPDIR:-/tmp}/unslop-{YYYYMMDD-HHmmss}-{4-char random suffix}.json` (unique per run — the file later holds full pre-fix file contents, the only revert source, so a name collision between concurrent runs is data loss):
```json
{"status": "scanning", "source": "", "findings": [], "files_scanned": []}
```
Update `findings` after Step 2, update `status` to `"roasting"` before Step 3, `"awaiting_fix_choice"` before Step 4, and `"verifying"` before Step 5 (behavior gate). Record `modified_files` and `pre_fix_content` in state once fixes are applied, so a failed gate or a compaction mid-fix can still revert cleanly. Delete the state file once the run completes (final report delivered, or report-only exit) — it contains source code and should not linger in a shared temp dir.

## Step Interfaces

**Step 1 → Step 2 produces** (required):
- `source`: string — description of where code came from
- `files`: array of {path, content} — all files to scan

**Step 2 → Step 3 produces** (required):
- `findings`: array of {id, pattern, location, severity, why_slop, snippet, fix}
- `severity` values: `CRITICAL` | `HIGH` | `MEDIUM` | `LOW`

**Step 3 → Step 4 produces**: rendered report (required); gate: wait for user numeric input before proceeding

**Step 4 → Step 5 produces** (when any fix was applied):
- `modified_files`: array of paths edited
- `pre_fix_content`: map of {path → original content}, retained so a failed gate can revert
- gate: Step 5 must run before reporting completion; a failing behavior check forces a revert of the applied fixes

**Review mode interface** (`--review`): consumes a drafted cleanup (diff / changed files), produces `{verdict: APPROVE | CHANGES NEEDED, followups: [...]}`; gate: no file writes occur in this mode

## Workflow

**Step 1: Get the code**

Default to uncommitted changes. Only deviate if user specifies otherwise.

- **Default**: Use Bash to run `git status --porcelain` and take every modified or untracked path (untracked files are where fresh slop usually lives; `git diff` alone misses them). Skip deleted paths and anything no longer on disk. Then read each remaining file.
- **User-specified**: scan the files/directories or pasted code the user provided

If the git command fails or returns an error: in `--auto` mode, output "Git status failed: [error]. No files to scan — exiting." and stop. Otherwise, call `AskUserQuestion` with the question "Could not read git status: [error]. Which files should I scan?"

If no uncommitted changes are found and no specific target was given: in `--auto` mode, output "No uncommitted changes found. No files to scan — exiting." and stop. Otherwise, call `AskUserQuestion` with the question "No uncommitted changes found. Which files should I scan?"

**Step 2: Scan for slop**
Flag each instance with: Pattern name, Location, Severity (`CRITICAL` / `HIGH` / `MEDIUM` / `LOW`), Why it's slop, Code snippet, Proposed fix.
Assign severity based on impact: CRITICAL = chatbot bleed or tautological assertions; HIGH = comment narrating obvious code, vacuous tests; MEDIUM = abstraction inflation, phantom parameters, defensive over-engineering, jargon; LOW = minor style issues.
Update state file `findings` array with each instance as you identify them.

**Step 3: Present the roast**
Summary stats + detailed findings with code snippets

**Step 4: Let user pick fixes**

After the report, present options as a numbered list and **wait for the user's numeric reply before proceeding**. If the user replies with anything other than 1, 2, 3, or 4, respond: "Please reply with 1, 2, 3, or 4."

```
How would you like to proceed?
  1. Fix all — auto-apply all suggested fixes
  2. Fix by severity — choose which severity levels to fix
  3. Interactive — walk through each finding one by one
  4. Report only — no changes, just the roast
```

**For "Fix by severity" (option 2)**: ask "Fix CRITICAL, HIGH, MEDIUM, or LOW? (list all that apply, e.g. 'CRITICAL HIGH')" and wait for reply before applying.

**For "Interactive" mode (option 3)**:
- Present each finding one at a time: show Pattern, Location, snippet, and proposed fix
- Ask "Fix this? (y/n)" and wait for reply before moving to the next finding
  - `y` = apply the fix to the file now
  - `n` = skip this finding, do not apply
- After the last finding, output a summary: "Applied X fixes, skipped Y. Files modified: [list]"

**Before applying any gated fixes (options 1, 2, or 3, and `--auto-fix`)**: find the Step 5 check(s) for the touched files and run them once as a **baseline**. Record each check's command and pass/fail as `baseline_status` in the state file. This is what makes the gate's blame honest — without it, a repo that was already red gets its cleanup wrongly reverted.

**After applying any fixes (options 1, 2, or 3)**: re-read each modified file with Read to confirm the edits persisted, then run **Step 5 (behavior gate)** before reporting completion.

**Step 5: Behavior gate (mandatory after any fix is applied)**

Removing dead code, collapsing abstractions, or rewriting tests can change behavior, so confirm behavior held instead of trusting that the edit "looks right".

1. **Find the narrowest check per ecosystem.** For each language present in the touched files, pick one check in this order: an obvious project runner (`npm test`/`pnpm test`/`pytest`/`go test`/`cargo test`), then a typecheck/lint (`tsc --noEmit`, `ruff`, `mypy`). A diff touching both TS and Python gates on both — never report "verified" while one ecosystem shipped unchecked. Prefer a scoped run over the touched paths over a full suite.
2. **Run every selected check** (the same ones you baselined in Step 4). Compare each result with its baseline.
   - **Pass:** report completion with the command and its result as evidence.
   - **Fail, baseline was green (or the failure is new):** the cleanup likely changed behavior. Revert the fixes you just applied (restore the pre-fix content of each modified file), then report which findings were backed out and the failing output. Before restoring a file from `pre_fix_content`, re-read it: if its current content no longer matches what you wrote in Step 4, something else edited it while the check ran — stop and warn instead of overwriting. Never leave a failing tree and never "fix forward" past a red gate inside this skill.
   - **Fail, baseline was already red with the same failures:** the cleanup is not to blame. Keep the fixes, but report "unverified: baseline was already failing" with both outputs so the user can judge.
3. **If no check can be found:** do not silently skip. State plainly that behavior was not verified, list exactly which files changed, and recommend the user run their own tests. In `--auto` mode, prefer reverting non-exempt fixes over leaving them unverified; gate-exempt comment deletions are safe to keep.

**Gate-exempt edits** are only deletions or condensations of plain comments (patterns 1 and 5) — comments that are not docstrings and not directives. Everything else is gated:
- **Docstrings are code**, not comments: they are runtime objects (`__doc__`, doctest, argparse/click help). Deleting or condensing one goes through the gate.
- **Directive comments are code**: `# noqa`, `# type: ignore`, `// @ts-ignore`, `// eslint-disable-next-line`, `//go:build`, `//go:embed`, shebang lines. Never delete these as slop; if one looks wrong, flag it in the report instead.
- **Runtime strings are code**: any pattern-5 rewrite of an error message, log line, or exception text is behavior-visible (tests match on message text, log parsers key on it) and always goes through the gate — including under `--auto-fix`.

Gate-exemption is independent of severity: an exempt CRITICAL chatbot-bleed comment deletion is still exempt — severity reflects maintainability impact, not behavioral risk. Still report everything removed.

## Slop Patterns

### 1. COMMENT SLOP (MAXIMUM PRIORITY)

99% of AI-generated comments are garbage. Delete them all.

**The Rule:** If the comment just restates what the code does, DELETE IT.

Crimes:
- **The Narrator**: `# Retrieve all users from the repository` above `all_users = user_repository.get_all_users()`
- **The Paraphraser**: `// Calculate the total price` above `const total = price * quantity`
- **The Step Counter**: `# Step 1: Initialize`, `# Step 2: Execute`, `# Step 3: Process`
- **The Over-Documenter**: 8-line docstring for 4-line self-explanatory function
- **The TODO Graveyard**: 6 TODOs, zero actual improvements
- **The Type Announcer**: `// String variable to store user name` above `const userName: string`
- **The Import Explainer**: `# Import the os module for operating system operations`
- **The Windbag**: a legitimate why/gotcha/spec comment that earns its place but is padded to multiple sentences or a paragraph when one terse line carries the same information. Do NOT delete this one, the intent is worth keeping. CONDENSE it to the shortest form that preserves the "why". Example: a 4-line explanation of why a lock is taken before the retry becomes `// lock before retry: the upstream client is not reentrant`. Severity LOW/MEDIUM (maintainability, not correctness).

**Keep only, and keep terse**: Why a non-obvious approach was chosen, business logic not clear from code, warnings about gotchas, links to specs/tickets. A surviving comment should be one line wherever one line suffices; condense the verbose ones rather than leaving them bloated. Condensing plain comment text is gate-exempt, same as deletion — but docstrings and directive comments (`# noqa`, `# type: ignore`, `//go:build`, shebangs) are NOT plain comments: trimming an Over-Documenter docstring is a gated edit, and directives are never deleted as slop (see the gate-exemption rules in Step 5).

### 2. VACUOUS TESTS

Tests that verify nothing meaningful:
- `EXPECT_TRUE(true)` or `assert True`
- Testing boolean tautologies: `result == OK || result != OK`
- No assertions, just calling functions and hoping
- Testing setter/getter roundtrips with no logic
- Coverage padding: same "test" 15 times with different params and zero assertions
- Mock-only assertions: asserting `mock.method.assert_called_once()` or `call_count` while never checking return values, side effects, or state — the assertion verifies the test's own setup, not the code under test

### 3. ABSTRACTION INFLATION

Creating "enterprise frameworks" for simple scripts:
- Interfaces with one implementation
- Factory for objects with one variant
- Builder for 2-3 field objects
- DI framework for a 50-line script

### 4. CONTEXT-BLIND REINVENTION

Rewriting existing utils instead of using them. AI generates duplicate `send_email()` when one already exists.

### 5. CHATBOT BLEED

Conversational language in code: "I hope this helps!", "Certainly!", "Let me know if you need anything else"

Also AI prose that leaked into runtime strings — error messages, log lines, or exception text written as hedged chat replies: `raise ValueError("It seems like the user_id might be None — you may want to double-check and try again!")`, `logger.error("Unfortunately, we were unable to process your request at this time.")`.

### 6. CORPORATE JARGON

`leverage_caching_mechanism_to_enhance_performance()` — "leverage" means "use", "facilitate" means "enable".

### 7. DUPLICATION DRIFT

Same types/functions defined multiple times across files.

### 8. INCONSISTENT PARADIGM MASH

Mixing async/await with callbacks, ORM with raw SQL, in the same file.

### 9. SPEC BLEED

Prompt vocabulary in code names: `implement_business_requirement_3_2()`, `UltimateTierFeatureFlagV2AsRequested`

### 10. HARDCODED SLEEP WAITS

Fixed `sleep(5)` instead of proper `waitForCondition()` in tests. Also production sleeps standing in for real backoff: `time.sleep(2)` inside a pagination loop instead of honoring `Retry-After` or exponential backoff.

### 11. PHANTOM PARAMETERS

Function signatures accepting arguments never referenced in the body — copied from a similar function or lifted straight from the prompt. `def calculate_discount(price, user_id, region, tier): return price * 0.9` takes four params and uses one.

### 12. DEFENSIVE OVER-ENGINEERING

Needless `try/except` that swallows or blindly re-raises, wrapping code that can't realistically fail or where the handler adds nothing: `try: return x + y\nexcept Exception as e: raise e`, or a broad `except Exception: pass` that hides real errors. AI reaches for defensive handling it doesn't understand the need for.

## Output Format

```
═══════════════════════════════════════
  AI SLOP DETECTION REPORT
═══════════════════════════════════════

Source: [where the code came from]
Total slop found: [X] patterns
Signal: [MAXIMUM / STRONG / MODERATE / WEAK]

Slop Breakdown:
  Comment Slop:           [count]
  Vacuous Tests:          [count]
  Abstraction Inflation:  [count]
  Duplication:            [count]
  Other Slop:             [count]

Verdict: [one-line summary]
```

Then detailed findings with location, code snippet, roast, and fix.

**If zero slop is found**, output:

```
═══════════════════════════════════════
  AI SLOP DETECTION REPORT
═══════════════════════════════════════

Source: [where the code came from]
Total slop found: 0 patterns
Signal: CLEAN

Verdict: No slop detected. Code reads like it was written by a human who was
         paying attention. Either this is genuinely good code, or it's too
         short to have accumulated cruft yet.
```

## Roast Tone

The roast should be direct, dry, and specific — not generic. Each finding's roast should:
- Name exactly what's wrong in one punchy sentence
- Reference the actual code (don't be vague)
- Avoid corporate hedging ("this could potentially be improved")

Good: `"'# Initialize the counter variable' — yes, we can see it's a counter variable. The word 'counter' is right there. Delete this."`
Bad: `"This comment may not add significant value to the codebase."`

## Detection Priority

1. Comments narrating obvious code (DELETE THESE FIRST)
2. Chatbot bleed ("I hope this helps!")
3. Tautological test assertions
4. Abstraction for simple scripts
5. Reinventing existing utils
6. Corporate jargon
7. Spec bleed in names
