#!/usr/bin/env python3
"""Validate inter-step handoff data in context-contradiction-checker state files.

Deterministic schema validation for the typed interfaces defined between
the skill's workflow steps. Checks that the actual data in the state file
matches the schemas declared in SKILL.md.

Usage:
    validate_handoffs.py STATE_FILE --handoff step1_to_step2 [--json]
    validate_handoffs.py STATE_FILE --all [--json]
    validate_handoffs.py STATE_FILE --list-schemas

Exit codes:
    0 = all validated handoffs pass
    1 = one or more validation failures
    2 = usage error (bad args, file not found, invalid handoff name)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema DSL (mirrors hone's validate_handoff.py conventions)
# ---------------------------------------------------------------------------

def _str(required: bool = True, non_empty: bool = False) -> dict:
    return {"type": "string", "required": required, "non_empty": non_empty}


def _num(required: bool = True, min_value: float | None = None, max_value: float | None = None) -> dict:
    spec: dict = {"type": "number", "required": required}
    if min_value is not None:
        spec["min_value"] = min_value
    if max_value is not None:
        spec["max_value"] = max_value
    return spec


def _bool(required: bool = True) -> dict:
    return {"type": "boolean", "required": required}


def _enum(values: list[str], required: bool = True) -> dict:
    return {"type": "enum", "required": required, "values": values}


def _arr(items: dict | None = None, required: bool = True, non_empty: bool = False) -> dict:
    spec: dict = {"type": "array", "required": required, "non_empty": non_empty}
    if items is not None:
        spec["items"] = items
    return spec


def _obj(fields: dict, required: bool = True) -> dict:
    return {"type": "object", "required": required, "fields": fields}


# ---------------------------------------------------------------------------
# Handoff schemas (mirrors the SKILL.md typed interfaces)
# ---------------------------------------------------------------------------

HANDOFF_SCHEMAS: dict[str, dict] = {
    # Step 1 → Step 2
    "step1_to_step2": {
        "description": "Files discovered and directives extracted in Step 1",
        "fields": {
            "files_found": _arr(items={"type": "string"}, non_empty=True),
            "directives": _arr(
                items=_obj({
                    "text": _str(non_empty=True),
                    "source_file": _str(non_empty=True),
                    "topic": _str(non_empty=True),
                }),
                non_empty=True,
            ),
        },
    },
    # Step 2 → Step 3
    "step2_to_step3": {
        "description": "Issues list produced by analysis in Step 2",
        "fields": {
            "issues": _arr(
                items=_obj({
                    "id": _str(non_empty=True),
                    "type": _enum(["CONFLICT", "TENSION", "OVERLAP"]),
                    "confidence": _num(min_value=0, max_value=100),
                    "topic": _str(non_empty=True),
                    "directive_a": _str(non_empty=True),
                    "source_a": _str(non_empty=True),
                    "directive_b": _str(non_empty=True),
                    "source_b": _str(non_empty=True),
                    "resolution": _str(non_empty=True),
                }),
                # Empty is valid (zero-findings path) — gate handles the HIGH stop
                non_empty=False,
            ),
        },
    },
    # Step 3 → Step 4
    "step3_to_step4": {
        "description": "Report presented flag and condensed issues list for fix menu",
        "fields": {
            "issues": _arr(
                items=_obj({
                    "id": _str(non_empty=True),
                    "type": _str(non_empty=True),
                    "confidence": _num(min_value=0, max_value=100),
                }),
                non_empty=False,
            ),
            "report_presented": _bool(),
        },
    },
    # Workflow state envelope (top-level state file structure)
    #
    # These fields mirror the state file documented in SKILL.md's "Workflow State"
    # section verbatim. SKILL.md is authoritative: `directives` is persisted as the
    # full array (not a count) because Step 2 consumes the list, and `auto` is a
    # top-level flag because it must survive compaction. Changing either shape here
    # without changing SKILL.md re-breaks the contract.
    "workflow_state": {
        "description": "Top-level state file written at invocation start",
        "fields": {
            "mode": _enum(["full", "targeted", "single-file"]),
            "auto": _bool(),
            "step": _num(min_value=1, max_value=4),
            "files_found": _arr(items={"type": "string"}),
            "directives": _arr(
                items=_obj({
                    "text": _str(non_empty=True),
                    "source_file": _str(non_empty=True),
                    "topic": _str(non_empty=True),
                }),
            ),
            "issues_found": _arr(),
            "gates": _arr(
                items=_obj({
                    "gate": _str(non_empty=True),
                    "result": _enum(["PASS", "HIGH_STOP", "CRITICAL_STOP"]),
                    "ts": _str(non_empty=True),
                }),
            ),
        },
    },
}


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------

@dataclass
class ValidationError:
    path: str
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ValidationResult:
    handoff: str
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    fields_checked: int = 0


def validate_value(value: object, spec: dict, path: str, errors: list[ValidationError]) -> int:
    """Validate a single value against its spec. Returns count of fields checked."""
    checked = 0
    field_type = spec.get("type", "string")

    if field_type == "string":
        checked += 1
        if not isinstance(value, str):
            errors.append(ValidationError(path, f"expected string, got {type(value).__name__}"))
        elif spec.get("non_empty") and not value.strip():
            errors.append(ValidationError(path, "string must be non-empty"))

    elif field_type == "number":
        checked += 1
        if isinstance(value, bool):
            errors.append(ValidationError(path, "expected number, got bool"))
        elif not isinstance(value, (int, float)):
            errors.append(ValidationError(path, f"expected number, got {type(value).__name__}"))
        else:
            if "min_value" in spec and value < spec["min_value"]:
                errors.append(ValidationError(path, f"value {value} below minimum {spec['min_value']}"))
            if "max_value" in spec and value > spec["max_value"]:
                errors.append(ValidationError(path, f"value {value} above maximum {spec['max_value']}"))

    elif field_type == "boolean":
        checked += 1
        if not isinstance(value, bool):
            errors.append(ValidationError(path, f"expected boolean, got {type(value).__name__}"))

    elif field_type == "enum":
        checked += 1
        allowed = spec.get("values", [])
        if value not in allowed:
            errors.append(ValidationError(path, f"value {value!r} not in allowed values: {allowed}"))

    elif field_type == "object":
        checked += 1
        if not isinstance(value, dict):
            errors.append(ValidationError(path, f"expected object, got {type(value).__name__}"))
        elif "fields" in spec:
            checked += validate_fields(value, spec["fields"], path, errors)

    elif field_type == "array":
        checked += 1
        if not isinstance(value, list):
            errors.append(ValidationError(path, f"expected array, got {type(value).__name__}"))
        else:
            if spec.get("non_empty") and len(value) == 0:
                errors.append(ValidationError(path, "array must be non-empty"))
            if "items" in spec:
                item_spec = spec["items"]
                for idx, item in enumerate(value):
                    checked += validate_value(item, item_spec, f"{path}[{idx}]", errors)

    return checked


def validate_fields(data: dict, field_specs: dict, parent_path: str, errors: list[ValidationError]) -> int:
    """Validate all fields in a dict against their specs. Returns fields checked."""
    checked = 0
    for field_name, spec in field_specs.items():
        field_path = f"{parent_path}.{field_name}" if parent_path else field_name
        required = spec.get("required", True)
        if field_name not in data:
            if required:
                errors.append(ValidationError(field_path, "required field missing"))
            continue
        checked += validate_value(data[field_name], spec, field_path, errors)
    return checked


def validate_handoff(state: dict, handoff_name: str) -> ValidationResult:
    """Validate a single handoff schema against the state file data."""
    if handoff_name not in HANDOFF_SCHEMAS:
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[ValidationError(
                handoff_name,
                f"unknown handoff schema: {handoff_name!r}. "
                f"Valid names: {sorted(HANDOFF_SCHEMAS.keys())}",
            )],
        )

    schema = HANDOFF_SCHEMAS[handoff_name]

    # workflow_state validates the root state object itself
    if handoff_name == "workflow_state":
        data = state
    else:
        if handoff_name not in state:
            return ValidationResult(
                handoff=handoff_name,
                valid=False,
                errors=[ValidationError(
                    handoff_name,
                    f"handoff data not found in state file (key {handoff_name!r} missing)",
                )],
            )
        data = state[handoff_name]

    if not isinstance(data, dict):
        return ValidationResult(
            handoff=handoff_name,
            valid=False,
            errors=[ValidationError(
                handoff_name,
                f"handoff data must be an object, got {type(data).__name__}",
            )],
        )

    errors: list[ValidationError] = []
    checked = validate_fields(data, schema["fields"], handoff_name, errors)
    real_errors = [e for e in errors if e.severity == "error"]
    warnings = [e for e in errors if e.severity == "warning"]

    return ValidationResult(
        handoff=handoff_name,
        valid=len(real_errors) == 0,
        errors=real_errors,
        warnings=warnings,
        fields_checked=checked,
    )


def validate_all(state: dict) -> list[ValidationResult]:
    """Validate all known handoffs that are present in the state file."""
    results: list[ValidationResult] = []
    for handoff_name in HANDOFF_SCHEMAS:
        if handoff_name == "workflow_state":
            results.append(validate_handoff(state, handoff_name))
        elif handoff_name in state:
            results.append(validate_handoff(state, handoff_name))
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_text(results: list[ValidationResult]) -> str:
    lines: list[str] = []
    all_valid = all(r.valid for r in results)
    for result in results:
        status = "PASS" if result.valid else "FAIL"
        desc = HANDOFF_SCHEMAS.get(result.handoff, {}).get("description", "")
        label = f"{result.handoff}" + (f" — {desc}" if desc else "")
        lines.append(f"[{status}] {label} ({result.fields_checked} fields checked)")
        for err in result.errors:
            lines.append(f"  ERROR: {err.path}: {err.message}")
        for warn in result.warnings:
            lines.append(f"  WARN:  {warn.path}: {warn.message}")
    lines.append("")
    if all_valid:
        lines.append(f"Result: ALL PASS ({len(results)} handoffs validated)")
    else:
        fail_count = sum(1 for r in results if not r.valid)
        lines.append(f"Result: {fail_count} FAIL, {len(results) - fail_count} PASS")
    return "\n".join(lines)


def format_json(results: list[ValidationResult]) -> str:
    output = {
        "valid": all(r.valid for r in results),
        "handoffs_checked": len(results),
        "results": [
            {
                "handoff": r.handoff,
                "valid": r.valid,
                "fields_checked": r.fields_checked,
                "errors": [asdict(e) for e in r.errors],
                "warnings": [asdict(e) for e in r.warnings],
            }
            for r in results
        ],
    }
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate context-contradiction-checker workflow handoff interfaces",
    )
    parser.add_argument("state_file", type=str, help="Path to the contradiction-check state JSON file")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--handoff", type=str, help="Validate a specific handoff by name")
    mode.add_argument("--all", action="store_true", help="Validate all handoffs present in the state file")
    mode.add_argument("--list-schemas", action="store_true", help="List all known handoff schema names and exit")

    parser.add_argument("--json", action="store_true", help="Output as JSON instead of human-readable text")
    args = parser.parse_args()

    if args.list_schemas:
        for name in sorted(HANDOFF_SCHEMAS.keys()):
            schema = HANDOFF_SCHEMAS[name]
            field_count = len(schema.get("fields", {}))
            desc = schema.get("description", "")
            print(f"  {name} ({field_count} fields)" + (f": {desc}" if desc else ""))
        return 0

    state_path = Path(args.state_file)
    if not state_path.exists():
        print(f"Error: state file not found: {state_path}", file=sys.stderr)
        return 2

    try:
        state = json.loads(state_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in state file: {exc}", file=sys.stderr)
        return 2

    if not isinstance(state, dict):
        print("Error: state file root must be a JSON object", file=sys.stderr)
        return 2

    if args.handoff:
        results = [validate_handoff(state, args.handoff)]
    elif args.all:
        results = validate_all(state)
    else:
        parser.print_help()
        return 2

    print(format_json(results) if args.json else format_text(results))
    return 0 if all(r.valid for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
