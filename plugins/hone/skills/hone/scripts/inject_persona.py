#!/usr/bin/env python3
"""
Create persona-modified copies of eval criteria JSON files.

Injects persona framing into each check description so the
eval runner evaluates from a specific perspective.
"""

import argparse
import json
import sys

PERSONAS = {
    "pragmatist": (
        "Evaluate from a PRAGMATIST perspective: value brevity, YAGNI, and "
        "does-it-actually-work. Penalize over-engineering, unnecessary abstraction, "
        "and ceremony without outcomes. A 20-line skill that works reliably is better "
        "than a 200-line skill with perfect structure but the same outcome. "
    ),
    "architect": (
        "Evaluate from an ARCHITECT perspective: value structure, gates, error handling, "
        "and handoff contracts. Penalize shortcuts, missing error recovery, and implicit "
        "assumptions about state between steps. Robust, maintainable design matters more "
        "than brevity. "
    ),
    "user-advocate": (
        "Evaluate from a USER ADVOCATE perspective: value output quality, helpfulness, "
        "and actionability. Penalize verbose process logging that obscures results, "
        "boilerplate sections regardless of context, and scores/grades without interpretation. "
        "Would the user learn something or make a better decision because this ran? "
    ),
}


def inject_persona(
    criteria_path: str, persona_name: str, output_path: str, dry_run: bool = False
) -> dict:
    """Return a report describing the injection; write unless dry_run."""
    with open(criteria_path) as handle:
        data = json.load(handle)

    preamble = PERSONAS[persona_name]
    checks_modified = 0

    for test_case in data.get("test_cases", []):
        for check in test_case.get("checks", []):
            description = check.get("description", "")
            if description:
                check["description"] = preamble + description
                checks_modified += 1

    if not dry_run:
        with open(output_path, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    return {
        "criteria_path": criteria_path,
        "persona": persona_name,
        "output_path": None if dry_run else output_path,
        "test_cases": len(data.get("test_cases", [])),
        "checks_modified": checks_modified,
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a persona-modified copy of an eval criteria JSON file"
    )
    parser.add_argument("criteria_path", help="Path to the source eval_criteria.json")
    parser.add_argument(
        "persona", choices=sorted(PERSONAS), help="Persona framing to inject"
    )
    parser.add_argument("output_path", help="Path to write the modified criteria")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing the output file",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    try:
        report = inject_persona(
            args.criteria_path, args.persona, args.output_path, args.dry_run
        )
    except FileNotFoundError:
        print(f"ERROR: criteria file not found: {args.criteria_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: criteria file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    elif args.dry_run:
        print(
            f"DRY RUN: would inject {args.persona} into "
            f"{report['checks_modified']} check(s); no file written"
        )
    else:
        print(
            f"Created {args.output_path} with {args.persona} persona "
            f"({report['checks_modified']} check(s) modified)"
        )


if __name__ == "__main__":
    main()
