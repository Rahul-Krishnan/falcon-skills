# Script Quality Checklist

Reference for hone structural audit pillar 11: evaluating bundled scripts for agentic design principles.

## Mechanical Checks (5, run by structural_audit.py)

These are regex-based pattern checks run during the structural audit. Each produces a binary pass/fail.

| # | Check | Pattern | Pass | Fail |
|---|-------|---------|------|------|
| 1 | No interactive prompts | Scan for `input(`, `read -p`, `select`, `$REPLY` | No matches | Match found |
| 2 | Has help/usage | Scan for `--help`, `-h`, `usage`, `argparse` | Match found | No matches |
| 3 | Structured output | Scan for `json.dumps`, `--json`, `JSON`, `print(json` | Match found | No matches |
| 4 | Exit codes | Scan for `sys.exit`, `exit(`, `exit ` with numeric arg | Match found | No matches |
| 5 | Self-contained deps | Scan for `import` statements; check all are stdlib or in-repo | All stdlib/local | External dep without venv |

## LLM-Judged Criteria (5, evaluated during Phase 2 fresh-eyes analysis)

These require holistic judgment and are assessed by the fresh-eyes subagent during fresh-eyes analysis.

### 1. Idempotency

**Question:** Can this script be run twice with the same input and produce the same output without side effects?

**Pass criteria:**
- No append-only file writes without truncation
- No global state mutations (environment variables, config files)
- Deterministic output for deterministic input

**Fail signals:**
- Appends to files without checking existing content
- Creates resources without checking if they already exist
- Modifies global state (env vars, config files) without restoring

### 2. Dry-run Safety

**Question:** Does this script support a way to preview what it would do without actually doing it?

**Pass criteria:**
- Has `--dry-run` or `--preview` flag, OR
- Is read-only by nature (analysis scripts, validators), OR
- Outputs a plan before executing destructive operations

**Fail signals:**
- Immediately performs destructive operations (file deletes, API calls) with no preview
- No way to see what would happen before it happens

### 3. Predictable Output Size

**Question:** Is the output size bounded and predictable regardless of input size?

**Pass criteria:**
- Output is O(input) or better (summarization, filtering)
- Large outputs are paginated, truncated, or written to file
- Summary/count modes available for large result sets

**Fail signals:**
- Dumps entire file contents without truncation
- Output grows unboundedly with input size
- No summary mode for large datasets

### 4. Helpful Error Messages

**Question:** When the script fails, does it tell the user what went wrong and how to fix it?

**Pass criteria:**
- Error messages include: what failed, why, and suggested fix
- Non-zero exit code on error
- Errors go to stderr, not stdout

**Fail signals:**
- Raw stack traces as the only error output
- Silent failures (exit 0 on error)
- Generic "Error occurred" without specifics

### 5. Safe Defaults

**Question:** Does the script's default behavior minimize blast radius?

**Pass criteria:**
- Defaults to read-only or dry-run when destructive
- Requires explicit flags for destructive operations
- Validates inputs before acting

**Fail signals:**
- Defaults to destructive operations (overwrite, delete)
- No input validation before acting
- Requires explicit flags to be safe (inverted safety)

## How Hone Uses This

1. **Structural audit (pillar 11):** Runs the 5 mechanical checks. Results are `WARNING_ONLY` — they inform but don't block.
2. **Fresh-eyes analysis:** When the artifact has a `scripts/` directory, the fresh-eyes subagent reads this checklist and evaluates each bundled script against the 5 LLM-judged criteria.
3. **Improvement plan:** Script quality findings appear as `fix_type: "script"` entries. They are lower priority than structural and content fixes unless a script has critical safety issues (no exit codes + destructive defaults).

## When NOT to Use

- Artifacts without a `scripts/` directory
- Test files (`test_*.py`) — these are evaluated by different criteria
- Scripts that are explicitly documented as one-shot or interactive by design
