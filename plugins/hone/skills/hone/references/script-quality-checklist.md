# Script Quality Checklist

Checklist for bundled scripts, used by structural audit pillar 11.

## Mechanical Checks (5, run by structural_audit.py)

Each regex check returns pass/fail.

| # | Check | Pattern | Pass | Fail |
|---|-------|---------|------|------|
| 1 | No interactive prompts | Scan for `input(`, `read -p`, `select`, `$REPLY` | No matches | Match found |
| 2 | Has help/usage | Scan for `--help`, `-h`, `usage`, `argparse` | Match found | No matches |
| 3 | Structured output | Scan for `json.dumps`, `--json`, `JSON`, `print(json` | Match found | No matches |
| 4 | Exit codes | Scan for `sys.exit`, `exit(`, `exit ` with numeric arg | Match found | No matches |
| 5 | Self-contained deps | Scan for `import` statements; check all are stdlib or in-repo | All stdlib/local | External dep without venv |

## LLM-Judged Criteria (5, evaluated during Phase 2 fresh-eyes analysis)

The fresh-eyes subagent judges these criteria.

### 1. Idempotency

Can repeated runs with identical input produce the same output without side effects?

**Pass criteria:**
- No append-only file writes without truncation
- No global state mutations (environment variables, config files)
- Deterministic output for deterministic input

**Fail signals:**
- Appends to files without checking existing content
- Creates resources without checking if they already exist
- Modifies global state (env vars, config files) without restoring

### 2. Dry-run Safety

Can the user preview the script's effects?

**Pass criteria:**
- Has `--dry-run` or `--preview` flag, OR
- Is read-only by nature (analysis scripts, validators), OR
- Outputs a plan before executing destructive operations

**Fail signals:**
- Immediately performs destructive operations (file deletes, API calls) with no preview
- No preview of effects

### 3. Predictable Output Size

Is output size bounded and predictable?

**Pass criteria:**
- Output is O(input) or better (summarization, filtering)
- Large outputs are paginated, truncated, or written to file
- Summary/count modes available for large result sets

**Fail signals:**
- Dumps entire file contents without truncation
- Output grows unboundedly with input size
- No summary mode for large datasets

### 4. Helpful Error Messages

Do errors explain the failure and how to fix it?

**Pass criteria:**
- Error messages include: what failed, why, and suggested fix
- Non-zero exit code on error
- Errors go to stderr, not stdout

**Fail signals:**
- Raw stack traces as the only error output
- Silent failures (exit 0 on error)
- Generic "Error occurred" without specifics

### 5. Safe Defaults

Do defaults minimize the scope of damage?

**Pass criteria:**
- Defaults to read-only or dry-run when destructive
- Requires explicit flags for destructive operations
- Validates inputs before acting

**Fail signals:**
- Defaults to destructive operations (overwrite, delete)
- No input validation before acting
- Requires explicit flags to be safe (inverted safety)

## How Hone Uses This

1. **Structural audit (pillar 11):** Runs the 5 mechanical checks as non-blocking `WARNING_ONLY` findings.
2. **Fresh-eyes analysis:** For artifacts with `scripts/`, the subagent reads this checklist and judges each script against the 5 criteria.
3. **Improvement plan:** Record findings as `fix_type: "script"`, below structural and content fixes unless safety is critical (no exit codes + destructive defaults).

## When NOT to Use

- Artifacts without a `scripts/` directory
- Test files (`test_*.py`), which use different criteria
- Scripts that are explicitly documented as one-shot or interactive by design
