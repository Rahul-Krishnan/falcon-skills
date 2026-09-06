#!/usr/bin/env python3
"""Regression tests for validate_structure.py.

Every case here is a defect the validator once had. The two that matter most are the
false negatives (a malformed plugin passing) and the crashes (a traceback in CI instead
of a violation report), because both fail silently in the direction of "looks fine".

Builds throwaway plugin trees in a temp dir and runs the real validator against them as
a subprocess, so this exercises the actual CI entry point rather than importing internals.

Stdlib only. Usage: python3 scripts/test_validate_structure.py
"""

import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

VALIDATOR = Path(__file__).parent / "validate_structure.py"

GOOD_SKILL = """---
name: demo
description: "A demo skill."
metadata:
  user-invocable: true
  allowed-tools: "Read, Write"
---

# Demo
"""
GOOD_PLUGIN = {
    "name": "demo",
    "description": "d",
    "version": "0.1.0",
    "author": {"name": "R"},
    "keywords": ["k"],
}
GOOD_EVAL = {
    "project": "demo",
    "skill_name": "demo",
    "test_cases": [{"id": "TC-1", "name": "n", "prompt": "p", "checks": ["c"]}],
    "dimensions": {
        "task_completion": 0.3,
        "invocation": 0.2,
        "efficiency": 0.2,
        "best_practices": 0.15,
        "business_impact": 0.15,
    },
}
GOOD_OUTCOME_EVAL = {
    "schema_version": 3,
    "measurement": "outcomes",
    "project": "demo",
    "skill_name": "demo",
    "test_cases": [{
        "id": "TC-1",
        "name": "Preserves the requested artifact",
        "prompt": "Improve the skill without changing its required output.",
        "mode": "execution",
        "checks": [{
            "id": "artifact-contract",
            "description": "The improved skill still produces the required artifact.",
            "method": "artifact",
            "required": True,
        }],
    }],
}
GOOD_MARKETPLACE = {
    "name": "m",
    "plugins": [{"name": "demo", "source": "./plugins/demo", "description": "d"}],
}

SKILL_DEEP_NESTING = """---
name: demo
description: "A demo skill."
metadata:
  user-invocable: true
  some-other-block:
    allowed-tools: buried-three-levels-deep
---
"""
SKILL_TAB_INDENTED = (
    '---\nname: demo\ndescription: "A demo skill."\nmetadata:\n'
    '\tuser-invocable: true\n\tallowed-tools: "Read"\n---\n'
)
SKILL_TOOLS_AS_LIST = """---
name: demo
description: "A demo skill."
metadata:
  user-invocable: true
  allowed-tools:
    - Read
    - Write
    - Bash
---
"""
# Same-indent sequence items are valid YAML too, and a following sibling key must still
# be read as a key rather than swallowed into the list.
SKILL_TOOLS_AS_FLAT_LIST = """---
name: demo
description: "A demo skill."
metadata:
  allowed-tools:
  - Read
  - Write
  user-invocable: true
---
"""


def build_tree(root, skill=GOOD_SKILL, plugin=GOOD_PLUGIN, evals=GOOD_EVAL,
               marketplace=GOOD_MARKETPLACE, skill_bytes=None):
    skill_dir = root / "plugins" / "demo" / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    (root / "plugins" / "demo" / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin").mkdir(parents=True)

    if skill_bytes is not None:
        (skill_dir / "SKILL.md").write_bytes(skill_bytes)
    else:
        (skill_dir / "SKILL.md").write_text(skill)
    (root / "plugins" / "demo" / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin))
    (skill_dir / "evals" / "eval_criteria.json").write_text(json.dumps(evals))
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(marketplace))
    return root


def outcome_suite(case=None, **updates):
    suite = deepcopy(GOOD_OUTCOME_EVAL)
    if case:
        suite["test_cases"][0].update(case)
    suite.update(updates)
    return {"evals": suite}


# (description, tree kwargs, expected outcome)
CASES = [
    ("a valid tree passes", {}, "pass"),

    # Crashes: malformed JSON types must report a violation, never traceback.
    ("plugin.json is a list", {"plugin": [{"name": "demo"}]}, "fail"),
    ("author is a string", {"plugin": {**GOOD_PLUGIN, "author": "Rahul"}}, "fail"),
    ("test_cases is a dict", {"evals": {**GOOD_EVAL, "test_cases": {"t1": {"id": "x"}}}}, "fail"),
    ("dimension weights are strings",
     {"evals": {**GOOD_EVAL, "dimensions": {k: "0.2" for k in GOOD_EVAL["dimensions"]}}}, "fail"),
    ("SKILL.md is not UTF-8", {"skill_bytes": b"\xff\xfe---\nname: demo\n---\n"}, "fail"),
    ("marketplace plugin entries are strings",
     {"marketplace": {"name": "m", "plugins": ["demo"]}}, "fail"),
    ("marketplace source is a number",
     {"marketplace": {"name": "m", "plugins": [{"name": "demo", "source": 1, "description": "d"}]}}, "fail"),

    # False negatives: malformed frontmatter that used to pass.
    ("allowed-tools buried below metadata's children", {"skill": SKILL_DEEP_NESTING}, "fail"),
    ("frontmatter indented with tabs", {"skill": SKILL_TAB_INDENTED}, "fail"),

    # False positives: valid YAML that used to be rejected.
    ("allowed-tools written as a YAML list", {"skill": SKILL_TOOLS_AS_LIST}, "pass"),
    ("allowed-tools as a same-indent list, followed by a sibling key",
     {"skill": SKILL_TOOLS_AS_FLAT_LIST}, "pass"),

    ("outcome execution suite needs no weighted dimensions or fixtures",
     outcome_suite(), "pass"),
    ("outcome simulation suite declares its simulation boundary",
     outcome_suite(case={
         "mode": "simulation",
         "runner_context": "SIMULATION MODE: do not issue real tool calls. Use supplied evidence.",
     }), "pass"),
    ("outcome suite rejects legacy dimensions",
     outcome_suite(dimensions=GOOD_EVAL["dimensions"]), "fail", "'dimensions' is not allowed"),
    ("outcome suite requires outcome measurement",
     outcome_suite(measurement="process"), "fail", "'measurement' must be 'outcomes'"),
    ("outcome suite retains project validation", outcome_suite(project=""),
     "fail", "missing or empty 'project'"),
    ("outcome suite retains matching skill name validation", outcome_suite(skill_name="other"),
     "fail", "does not match plugin 'demo'"),
    ("outcome suite rejects duplicate case ids",
     outcome_suite(test_cases=GOOD_OUTCOME_EVAL["test_cases"] * 2),
     "fail", "duplicate test case id 'TC-1'"),
    ("outcome suite rejects duplicate check ids",
     outcome_suite(case={"checks": GOOD_OUTCOME_EVAL["test_cases"][0]["checks"] * 2}),
     "fail", "duplicate check id 'artifact-contract'"),
]

for version in (2, 4, None, True, "3", 3.0):
    CASES.append((
        f"explicit unsupported schema version {version!r} cannot route to legacy validation",
        {"evals": {**GOOD_EVAL, "schema_version": version}},
        "fail", "unsupported 'schema_version'",
    ))

for field in ("id", "name", "prompt"):
    for value in (None, " ", ["not-a-string"]):
        CASES.append((
            f"outcome case {field} rejects {value!r}", outcome_suite(case={field: value}),
            "fail", f"'{field}' must be a nonempty string",
        ))

for mode in (None, "live", ["execution"]):
    CASES.append((
        f"outcome case rejects mode {mode!r}", outcome_suite(case={"mode": mode}),
        "fail", "'mode' must be 'simulation' or 'execution'",
    ))

for context in (None, "Use no real tools.", " SIMULATION MODE: do not issue real tool calls."):
    CASES.append((
        f"simulation rejects missing or misplaced boundary {context!r}",
        outcome_suite(case={"mode": "simulation", "runner_context": context}),
        "fail", "'runner_context' must start with",
    ))

for field in ("test_cases", "checks"):
    for value in ([], {}, "checks"):
        kwargs = outcome_suite(**{field: value}) if field == "test_cases" else outcome_suite(case={field: value})
        CASES.append((
            f"outcome {field} rejects {value!r}", kwargs, "fail", f"'{field}' must be a nonempty list",
        ))

CASES += [
    ("outcome case must be an object", outcome_suite(test_cases=["case"]),
     "fail", "test case at index 0 must be an object"),
    ("outcome check must be an object", outcome_suite(case={"checks": ["check"]}),
     "fail", "check at index 0 must be an object"),
]

for field, values in {
    "id": (None, " ", ["id"]),
    "description": (None, " ", 4),
    "method": (None, "regex", ["artifact"]),
    "required": (None, 1, "true"),
}.items():
    message = {
        "id": "'id' must be a nonempty string",
        "description": "'description' must be a nonempty string",
        "method": "'method' must be 'artifact', 'judgment', or 'trace'",
        "required": "'required' must be a boolean",
    }[field]
    for value in values:
        check = {**GOOD_OUTCOME_EVAL["test_cases"][0]["checks"][0], field: value}
        CASES.append((
            f"outcome check {field} rejects {value!r}", outcome_suite(case={"checks": [check]}),
            "fail", message,
        ))

for method in ("artifact", "judgment", "trace"):
    check = {**GOOD_OUTCOME_EVAL["test_cases"][0]["checks"][0], "method": method, "required": False}
    CASES.append((
        f"outcome check supports {method} and optional checks",
        outcome_suite(case={"checks": [check]}), "pass",
    ))


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for index, case in enumerate(CASES):
            description, kwargs, expected, *required_output = case
            root = build_tree(Path(tmp) / f"case{index}", **kwargs)
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), str(root)], capture_output=True, text=True
            )
            output = result.stdout + result.stderr
            crashed = "Traceback" in output
            actual = "pass" if result.returncode == 0 else "fail"

            message_matches = not required_output or required_output[0] in output
            if actual == expected and not crashed and message_matches:
                print(f"  ok    {description}")
                continue

            failures += 1
            reason = "crashed with a traceback" if crashed else f"expected {expected}, got {actual}"
            if not message_matches:
                reason += f"; missing diagnostic {required_output[0]!r}"
            print(f"  FAIL  {description} ({reason})")
            print("        " + output.strip()[:300].replace("\n", "\n        "))

    print()
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
