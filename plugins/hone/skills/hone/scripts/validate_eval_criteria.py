#!/usr/bin/python3
"""Validate or audit eval_criteria.json files before launching eval runner.

Usage:
  python3 validate_eval_criteria.py <path_to_eval_criteria.json>         # validate only
  python3 validate_eval_criteria.py --audit <path> [--artifact-path P]   # audit with JSON findings

Validate mode (default):
  - required_present values that look like exact section headers (uppercase, spaces)
  - required_absent values that are common English words ("error", "warning", etc.)
  - Missing checks
  - Empty test cases
  - Unsimulatable pipeline tests
  - Tool-call string matching in required_present

Audit mode (--audit):
  All validation checks PLUS:
  - Missing runner_context (warning, not auto-generated)
  - Missing or incomplete allowed_tools
  - Missing target_skills
  - Keyword-only semantic checks (heuristic detection)
  - Minimum test count (3+, 2 for lightweight-tier artifacts)
  Outputs JSON to stdout. Human-readable messages go to stderr.

Exit codes (validate mode):
  0 = valid; warnings may be present (references/phase1-evaluation.md's
      Step 5 -> Step 6 gate accepts warnings, and the handoff field
      validation_passed is defined as exit 0 — warnings must not flip it)
  2 = errors (schema-invalid, unreadable, empty, or empty prompts)
Audit mode exits 1 on a hard error (unreadable/empty file), else 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Shared filesystem-mutation regexes, sandbox header, and slash-invocation
# detector (also used by side_effect_guard.py for sandboxing); edits to any
# of them belong in hone_common, not here.
from hone_common import (
    FS_MUTATING_BASH_PATTERNS,
    SANDBOX_HEADER,
    find_slash_invocations,
    get,
)

# Canonical pipeline-command list shared with side_effect_guard.py; add names there.
from pipeline_skills import PIPELINE_COMMANDS
from validate_criteria_schema import validate_criteria as validate_schema

# Words that legitimately appear in technical output and should NOT be in required_absent
DANGEROUS_ABSENT_WORDS = {
    "error",
    "warning",
    "fail",
    "failed",
    "bug",
    "issue",
    "problem",
    "debug",
    "exception",
    "trace",
    "stack",
    "crash",
    "retry",
}

# Words in semantic checks that suggest the test expects full pipeline execution
PIPELINE_RESULT_PATTERNS = re.compile(
    r"\b(output|result|completion|draft diff|landed|submitted|published|"
    r"diff created|diff submitted|rebase|CI signals|CI status)\b",
    re.IGNORECASE,
)

# Patterns that look like tool-call artifacts (file paths with extensions,
# script names) rather than prose the agent would write in a response
TOOL_CALL_ARTIFACT_RE = re.compile(
    r"[a-zA-Z0-9_-]+\.(py|sh|js|ts|json|yaml|yml|md|txt|cfg|conf|toml|ini|log)$"
)

# Behavioral verbs that indicate a semantic check measures actual skill behavior
BEHAVIORAL_VERBS = re.compile(
    r"\b(follow|execute|produce|invoke|route|validate|activate|begin|run|spawn|"
    r"dispatch|create|write|read|check|scan|parse|classify|display|present|"
    r"generate|build|complete|implement|attempt|start|trigger|fire|return|"
    r"call|use|apply|perform|handle|process|detect|identify|analyze)\b",
    re.IGNORECASE,
)

# Substring-matching language that indicates a keyword-only check
KEYWORD_LANGUAGE = re.compile(
    r"\b(contains?|includes?|mentions?|has the word|presence of|appears?|"
    r"shows? the string|lists?|says?|states?|prints?)\b",
    re.IGNORECASE,
)

# Default allowed_tools for skills/commands
DEFAULT_SKILL_TOOLS = [
    "Read",
    "Bash",
    "Grep",
    "Glob",
    "Skill",
    "Agent",
    "Write",
    "Edit",
]


# Typed null-safe getters tolerate malformed audit inputs. String-list getters
# also discard non-string items before regex checks and suggested repairs.


def _string_items(values: list) -> list[str]:
    """Only the string items of a possibly mixed-type list."""
    return [value for value in values if isinstance(value, str)]


def _get_required_present(tc: dict) -> list[str]:
    """Get required_present from test case."""
    return _string_items(get(tc, "required_present", [], expected=list))


def _get_required_absent(tc: dict) -> list[str]:
    """Get required_absent from test case."""
    return _string_items(get(tc, "required_absent", [], expected=list))


def _get_checks(tc: dict) -> list:
    """Get checks from test case."""
    return get(tc, "checks", [], expected=list)


def _get_runner_context(tc: dict) -> str:
    """Get runner_context from test case."""
    return get(tc, "runner_context", "", expected=str).strip()


def _get_allowed_tools(tc: dict) -> list[str]:
    """Return string allowed_tools entries only, so suggested repairs remain valid."""
    return _string_items(get(tc, "allowed_tools", [], expected=list))


def _get_target_skills(tc: dict) -> list:
    """Get target_skills from test case."""
    return get(tc, "target_skills", [], expected=list)


def _get_prompt(tc: dict) -> str:
    """Get prompt from test case."""
    return get(tc, "prompt", "", expected=str)


def _get_id(tc: dict) -> str:
    """Return a string test ID, defaulting to unknown for missing or invalid values.
    IDs must be hashable and sortable for audit findings."""
    return get(tc, "id", "unknown", expected=str)


def check_unsimulatable_pipeline(tc: dict) -> str | None:
    """Check if a test case invokes a pipeline command and expects full execution."""
    tc_id = _get_id(tc)
    prompt = _get_prompt(tc)

    invoked_command = None
    for cmd in PIPELINE_COMMANDS:
        # Slash form only. PIPELINE_COMMANDS includes ordinary English verbs
        # ("present", "ship"); bare-word matching flagged benign prompts like
        # "present its audit results" as pipeline invocations.
        if re.search(rf"/{re.escape(cmd)}\b", prompt):
            invoked_command = cmd
            break

    if not invoked_command:
        return None

    checks = _get_checks(tc)
    for check in checks:
        description = ""
        if isinstance(check, dict):
            description = get(check, "description", "", expected=str)
        elif isinstance(check, str):
            description = check
        if PIPELINE_RESULT_PATTERNS.search(description):
            return (
                f"  {tc_id} tests full pipeline execution for a slash command. "
                "Pipeline commands need real state (plans, changed files) that "
                "can't exist in eval runner simulation. Consider testing argument "
                "parsing, instruction quality, or flag handling instead."
            )

    return None


def check_tool_call_string_matching(tc: dict) -> list[str]:
    """Check if required_present contains tool-call artifacts."""
    tc_id = _get_id(tc)
    results = []

    for val in _get_required_present(tc):
        if TOOL_CALL_ARTIFACT_RE.search(val):
            results.append(
                f"  {tc_id}: required_present contains '{val}' which appears "
                "to be a tool call artifact, not a prose response string. "
                "With short_circuit, semantic checks won't run. Remove or "
                "replace with response-level strings."
            )

    return results


def is_brittle_present(value: str) -> tuple[bool, str]:
    """Check if a required_present value is too brittle."""
    if value.startswith("#"):
        # Markdown headers like "## Summary" are fine: they are structural
        # output the artifact emits verbatim, casing included. This exemption
        # must precede the uppercase check or it is unreachable.
        return False, ""
    if value != value.lower() and len(value) > 3:
        return (
            True,
            "contains uppercase (case-sensitive match will fail if agent uses different casing)",
        )
    if "  " in value:
        return True, "contains double spaces (fragile whitespace match)"
    if len(value) > 30:
        return (
            True,
            f"very long substring ({len(value)} chars) — unlikely to match exactly",
        )
    words = value.split()
    if len(words) >= 3 and any(w[0].isupper() for w in words):
        return True, "looks like a section header (multi-word with capitals)"
    return False, ""


# --- Audit-specific checks ---


def check_runner_context_present(tc: dict) -> dict | None:
    """Flag test cases missing runner_context."""
    tc_id = _get_id(tc)
    if not _get_runner_context(tc):
        return {
            "test_id": tc_id,
            "issue": "missing_runner_context",
            "severity": "warning",
            "message": (
                "No runner_context. eval runner executor won't know how to "
                "simulate this skill. Add test-case-specific simulation "
                "instructions."
            ),
        }
    return None


# Reject filesystem mutations and SETUP blocks in runner_context to preserve
# eval isolation. Share mutation patterns with side_effect_guard via hone_common;
# the SETUP pattern is specific to this validator.
_FS_MUTATING_PATTERNS = [
    (re.compile(pattern), label) for pattern, label in FS_MUTATING_BASH_PATTERNS
] + [
    (re.compile(r"^\s*SETUP:", re.MULTILINE), "SETUP: block"),
]

_SIMULATION_HEADER = "SIMULATION MODE:"


def check_runner_context_hygiene(tc: dict) -> list[dict]:
    """Require simulation-only runner_context: no filesystem mutations or SETUP
    blocks, and a SIMULATION MODE declaration when context is non-empty.
    Use SIMULATION blocks to describe command outputs.
    The injected sandbox block is exempt from mutation and SETUP checks because
    it quotes the commands it simulates and persists across criteria reuse."""
    findings: list[dict] = []
    tc_id = _get_id(tc)
    rc = _get_runner_context(tc)
    if not rc:
        return findings

    own_context = rc.split(SANDBOX_HEADER, 1)[0]
    for pattern, label in _FS_MUTATING_PATTERNS:
        if pattern.search(own_context):
            findings.append(
                {
                    "test_id": tc_id,
                    "issue": "runner_context_side_effect",
                    "severity": "fixable",
                    "message": (
                        f"runner_context contains '{label}', which causes "
                        "real filesystem side effects during eval runs. "
                        "Replace with a SIMULATION: block that describes "
                        "what the command would output."
                    ),
                }
            )

    if _SIMULATION_HEADER not in rc:
        findings.append(
            {
                "test_id": tc_id,
                "issue": "runner_context_missing_simulation_header",
                "severity": "fixable",
                "message": (
                    f"runner_context must declare '{_SIMULATION_HEADER} do "
                    "not issue real tool calls.' so the executor knows to "
                    "simulate side effects rather than perform them."
                ),
            }
        )

    return findings


def check_allowed_tools_audit(tc: dict) -> dict | None:
    """Flag test cases with missing or incomplete allowed_tools."""
    tc_id = _get_id(tc)
    tools = _get_allowed_tools(tc)
    prompt = _get_prompt(tc)

    if not tools:
        return {
            "test_id": tc_id,
            "issue": "missing_allowed_tools",
            "severity": "fixable",
            "message": "No allowed_tools defined.",
            "suggested_fix": {
                "field": "allowed_tools",
                "value": DEFAULT_SKILL_TOOLS,
            },
        }

    # Require Skill in allowed_tools for slash invocations, using the same
    # detector as side_effect_guard so punctuation and backticks match consistently.
    if find_slash_invocations(prompt) and "Skill" not in tools:
        return {
            "test_id": tc_id,
            "issue": "missing_skill_tool",
            "severity": "fixable",
            "message": "Prompt invokes a slash command but Skill not in allowed_tools.",
            "suggested_fix": {
                "field": "allowed_tools",
                "value": tools + ["Skill"],
            },
        }

    return None


def check_target_skills_audit(tc: dict, artifact_path: str | None) -> dict | None:
    """Flag test cases missing target_skills."""
    tc_id = _get_id(tc)
    targets = _get_target_skills(tc)

    if not targets and artifact_path:
        return {
            "test_id": tc_id,
            "issue": "missing_target_skills",
            "severity": "fixable",
            "message": "No target_skills defined.",
            "suggested_fix": {
                "field": "target_skills",
                "value": [artifact_path],
            },
        }

    return None


def check_semantic_check_quality(tc: dict) -> list[dict]:
    """Heuristically detect keyword-only semantic checks."""
    tc_id = _get_id(tc)
    findings = []
    checks = _get_checks(tc)

    for i, check in enumerate(checks):
        description = ""
        if isinstance(check, dict):
            description = get(check, "description", "", expected=str)
        elif isinstance(check, str):
            description = check

        if not description:
            continue

        has_keyword_lang = bool(KEYWORD_LANGUAGE.search(description))
        has_behavioral_verb = bool(BEHAVIORAL_VERBS.search(description))

        # Only flag if the check uses keyword language WITHOUT behavioral verbs
        if has_keyword_lang and not has_behavioral_verb:
            findings.append(
                {
                    "test_id": tc_id,
                    "issue": "keyword_only_check",
                    "severity": "warning",
                    "check_index": i,
                    "message": (
                        f"Semantic check {i} uses substring-matching language "
                        "without behavioral verbs. May not measure actual skill "
                        "behavior."
                    ),
                    "description_preview": description[:100],
                }
            )

    return findings


def check_minimum_test_count(test_cases: list) -> dict | None:
    """Warn below three cases. Lightweight artifacts allow two; this validator
    cannot see tier, so the tier-aware gate decides whether that warning matters."""
    if len(test_cases) < 3:
        return {
            "test_id": "_global",
            "issue": "low_test_count",
            "severity": "warning",
            "message": (
                f"Only {len(test_cases)} test case(s). The minimum is 3 "
                "(2 for lightweight-tier artifacts)."
            ),
        }
    return None


def validate(path: str, output_stream=None) -> int:
    """Validate eval criteria. Returns exit code.

    output_stream: if provided, print to this stream instead of stdout.
    """
    out = output_stream or sys.stdout

    # Run schema validation first
    schema_rc = validate_schema(path, output_stream=out)
    if schema_rc != 0:
        print(f"ERROR: Schema validation failed for {path}", file=out)
        return 2

    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Malformed JSON in {path}: {e}", file=out)
        return 2
    except OSError as e:
        print(f"ERROR: Cannot read {path}: {e}", file=out)
        return 2

    if not data:
        print(f"ERROR: Empty or null JSON file: {path}", file=out)
        return 2

    test_cases = get(data, "test_cases", [], expected=list)
    if not test_cases:
        print("ERROR: No test cases found", file=out)
        return 2

    warnings = []
    errors = []

    for tc in test_cases:
        tc_id = _get_id(tc)

        required_present = _get_required_present(tc)
        required_absent = _get_required_absent(tc)
        checks = _get_checks(tc)

        for val in required_present:
            brittle, reason = is_brittle_present(val)
            if brittle:
                warnings.append(f'  {tc_id}: required_present "{val}" — {reason}')

        for val in required_absent:
            if val.lower() in DANGEROUS_ABSENT_WORDS:
                warnings.append(
                    f'  {tc_id}: required_absent "{val}" — common word that appears in legitimate technical output'
                )

        if not checks:
            warnings.append(
                f"  {tc_id}: no checks defined (only programmatic checks)"
            )

        if not _get_prompt(tc).strip():
            errors.append(f"  {tc_id}: empty prompt")

        pipeline_warning = check_unsimulatable_pipeline(tc)
        if pipeline_warning:
            warnings.append(pipeline_warning)

        tool_call_warnings = check_tool_call_string_matching(tc)
        warnings.extend(tool_call_warnings)

    if errors:
        print(f"ERRORS ({len(errors)}):", file=out)
        for e in errors:
            print(e, file=out)

    if warnings:
        print(f"WARNINGS ({len(warnings)}):", file=out)
        for w in warnings:
            print(w, file=out)

    if not errors and not warnings:
        print(
            f"CLEAN: {len(test_cases)} test cases validated, no issues found", file=out
        )
        return 0

    if errors:
        return 2
    # Warnings alone do not fail validation: the Step 5 -> Step 6 gate
    # accepts warnings, and validation_passed (defined as exit 0) must
    # agree with the gate or an executor hard-stops on a file the
    # checklist declared acceptable.
    return 0


def audit(criteria_path: str, artifact_path: str | None) -> dict:
    """Run all audit checks. Returns JSON-serializable dict.

    Does NOT modify the criteria file. Returns findings for the caller
    (hone main thread) to apply via Edit tool, preserving JSON formatting.
    """
    # Send schema diagnostics to stderr to keep stdout valid JSON. Continue the
    # audit after schema errors so per-test checks can propose repairs for the
    # missing context and tools that caused them.
    schema_rc = validate_schema(criteria_path, output_stream=sys.stderr)
    schema_valid = schema_rc == 0

    try:
        with open(criteria_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {
            "error": str(e),
            "findings": [],
            "fixable_count": 0,
            "warning_count": 0,
            "should_regenerate": False,
            "unfixable_test_ids": [],
            "total_test_cases": 0,
        }

    if not data:
        return {
            "error": "Empty or null JSON file",
            "findings": [],
            "fixable_count": 0,
            "warning_count": 0,
            "should_regenerate": False,
            "unfixable_test_ids": [],
            "total_test_cases": 0,
        }

    test_cases = get(data, "test_cases", [], expected=list)
    if not test_cases:
        return {
            "error": "No test cases found",
            "findings": [],
            "fixable_count": 0,
            "warning_count": 0,
            "should_regenerate": False,
            "unfixable_test_ids": [],
            "total_test_cases": 0,
        }

    findings: list[dict] = []

    for tc in test_cases:
        # Audit-specific checks
        rc_finding = check_runner_context_present(tc)
        if rc_finding:
            findings.append(rc_finding)

        hygiene_findings = check_runner_context_hygiene(tc)
        findings.extend(hygiene_findings)

        at_finding = check_allowed_tools_audit(tc)
        if at_finding:
            findings.append(at_finding)

        ts_finding = check_target_skills_audit(tc, artifact_path)
        if ts_finding:
            findings.append(ts_finding)

        sq_findings = check_semantic_check_quality(tc)
        findings.extend(sq_findings)

    # Global check
    mtc_finding = check_minimum_test_count(test_cases)
    if mtc_finding:
        findings.append(mtc_finding)

    # Run existing validation checks too (output to stderr, not stdout)
    validate(criteria_path, output_stream=sys.stderr)

    fixable = [f for f in findings if f.get("severity") == "fixable"]
    warnings = [f for f in findings if f.get("severity") == "warning"]

    # Count unfixable issues per test case for regeneration threshold
    unfixable_test_ids = set()
    for f in findings:
        if f.get("severity") == "warning" and f.get("issue") in (
            "missing_runner_context",
            "keyword_only_check",
        ):
            tid = f.get("test_id", "")
            if tid and tid != "_global":
                unfixable_test_ids.add(tid)

    should_regenerate = len(unfixable_test_ids) > len(test_cases) / 2

    return {
        "schema_valid": schema_valid,
        "findings": findings,
        "fixable_count": len(fixable),
        "warning_count": len(warnings),
        "should_regenerate": should_regenerate,
        "unfixable_test_ids": sorted(unfixable_test_ids),
        "total_test_cases": len(test_cases),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate or audit eval criteria JSON files"
    )
    parser.add_argument("criteria_path", help="Path to eval_criteria.json")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run full audit with JSON findings output on stdout",
    )
    parser.add_argument(
        "--artifact-path",
        help="Path to the artifact's source file (used with --audit)",
    )
    args = parser.parse_args()

    if args.audit:
        result = audit(args.criteria_path, args.artifact_path)
        json.dump(result, sys.stdout, indent=2)
        print()  # trailing newline
        sys.exit(1 if result.get("error") else 0)
    else:
        sys.exit(validate(args.criteria_path))
