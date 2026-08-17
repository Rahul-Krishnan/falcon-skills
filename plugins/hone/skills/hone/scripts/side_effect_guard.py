#!/usr/bin/python3
"""Guard eval criteria against side-effecting commands in the artifact under test.

Scans the artifact for patterns that cause real-world side effects (diff submission,
GChat posting, source control mutations) and modifies eval criteria to sandbox them:
  1. Removes dangerous MCP tools from allowed_tools
  2. Prepends simulation instructions to runner_context for Bash-based side effects

Usage:
  python3 side_effect_guard.py --artifact-path <path> --criteria-path <path> [--json] [--dry-run]

Exit codes:
  0 — criteria modified (or --dry-run with findings)
  1 — error
  2 — no side effects detected, criteria unchanged
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Canonical skill list shared with validate_eval_criteria.py; add names there.
from pipeline_skills import SIDE_EFFECTING_SKILLS

# Bash command patterns that cause real-world side effects.
# Each tuple: (regex_pattern, human_label, simulated_response)
BASH_SIDE_EFFECTS = [
    (r"\bgit\s+push\b", "git push", "Branch pushed to remote successfully"),
    (r"\bgit\s+push\s+--force\b", "git push --force", "Force pushed to remote"),
    (r"\bgh\s+pr\s+create\b", "gh pr create", "Pull request created: #99"),
    (r"\bgh\s+pr\s+merge\b", "gh pr merge", "Pull request #99 merged"),
    (r"\bgit\s+commit\b", "git commit", "Created commit abc1234def5"),
    # Filesystem-mutating commands — flagged so eval criteria never
    # actually create scripts or files during a test run. These are the
    # shapes that showed up in SETUP: blocks and caused flaky eval state.
    (r"\bmkdir\s+(-p\s+)?[^\s]+", "mkdir", "mkdir completed"),
    (r"\bprintf\s+[^|]*>[>\s]*[^\s]+", "printf > file", "file written"),
    (r"\becho\s+[^|]*>[>\s]*[^\s]+", "echo > file", "file written"),
    (r"\bcp\s+[^\s]+\s+[^\s]+", "cp", "file copied"),
]

# MCP tool name patterns to remove from allowed_tools.
# Matched as substrings against each tool name in the list.
MCP_TOOL_BLOCKLIST = [
    "google_chat",
    "send_message",
    "send_message_as_user",
]

# Runner context block prepended to each test case when side effects are detected.
SANDBOX_HEADER = "SAFETY SANDBOX — side-effect simulation mode"

# Harness tools exempt from the allowed-tools intersection: Skill invokes the
# artifact under test and criteria_self_repair.py adds AskUserQuestion for
# error-handling tests; artifacts never declare either. Lowercase base names.
HARNESS_TOOLS = frozenset({"skill", "askuserquestion"})

# Fail-closed delegation detection: any /slash-command in the artifact that is
# not a known side-effecting skill is still treated as side-effecting, because
# an unlisted user pipeline (/deploy, /release) escaping the sandbox can run a
# real `git push` during an unattended eval. The pattern matches a
# delegation-shaped token (line start / whitespace / backtick / bracket before
# the slash, no second slash after the name) so file paths like /tmp/x or
# factor/face never fire; the stoplist below drops bare path heads.
DELEGATION_RE = re.compile(r"(?:^|[\s`(\[])/([a-z][a-z0-9-]{2,})\b(?!/)", re.MULTILINE)
DELEGATION_STOPLIST = frozenset(
    {"tmp", "usr", "bin", "etc", "var", "opt", "dev", "home", "private", "users"}
)


def _base_tool_name(tool: str) -> str:
    """Normalize a tool string to its lowercase base name.

    'Bash(git:*)' -> 'bash', 'Bash' -> 'bash', 'mcp__x__y' -> 'mcp__x__y'.
    Scoped forms must match their base tool, otherwise a declared
    'Bash(git:*)' would strip a test case's plain 'Bash'.
    """
    return re.split(r"[(\s:]", tool.strip(), maxsplit=1)[0].lower()


def parse_allowed_tools_frontmatter(content: str) -> list[str] | None:
    """Extract `allowed-tools:` list from artifact YAML frontmatter.

    Returns None if no frontmatter or no allowed-tools key — meaning "no
    declared prohibition, apply no filter." Returns a (possibly empty) list
    if the key is present — an empty list means "no tools allowed."
    """
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return None
    fm = fm_match.group(1)

    # Inline form: allowed-tools: [Read, Grep, Bash]
    inline = re.search(
        r"^allowed-tools:\s*\[(.*?)\]\s*$", fm, re.MULTILINE
    )
    if inline:
        raw = inline.group(1)
        return [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]

    # Block form:
    #   allowed-tools:
    #     - Read
    #     - Grep
    block = re.search(
        r"^allowed-tools:\s*\n((?:\s+-\s+.*\n?)+)", fm, re.MULTILINE
    )
    if block:
        items = re.findall(r"^\s+-\s+(.*)$", block.group(1), re.MULTILINE)
        return [t.strip().strip("'\"") for t in items if t.strip()]

    return None


def scan_artifact(content: str, self_names: frozenset[str] = frozenset()) -> dict:
    """Scan artifact content for side-effecting patterns.

    Returns dict with 'bash_commands', 'mcp_tools', 'delegated_skills', and
    'unknown_delegations'. `self_names` are the artifact's own names (dir and
    file stem), excluded from delegation detection so a skill's usage examples
    of itself never sandbox the invocation the eval depends on.
    """
    bash_hits: list[str] = []
    for pattern, label, _ in BASH_SIDE_EFFECTS:
        if re.search(pattern, content, re.IGNORECASE):
            bash_hits.append(label)

    mcp_hits: list[str] = []
    for tool_pattern in MCP_TOOL_BLOCKLIST:
        if re.search(rf"\b{re.escape(tool_pattern)}\b", content, re.IGNORECASE):
            mcp_hits.append(tool_pattern)

    # Detect invocations of known side-effecting skills/commands
    delegated: list[str] = []
    for skill_name in SIDE_EFFECTING_SKILLS:
        # Match /skill-name or Skill("skill-name") or skill: "skill-name"
        if re.search(rf"/{re.escape(skill_name)}\b", content):
            delegated.append(skill_name)

    # Fail closed: any other delegation-shaped slash command is treated as
    # side-effecting too. A false positive costs one inert sandbox block; a
    # false negative is a real push from an unattended eval.
    unknown: list[str] = []
    known = set(delegated)
    for match in DELEGATION_RE.finditer(content):
        name = match.group(1)
        if (
            name not in known
            and name not in DELEGATION_STOPLIST
            and name not in self_names
            and name not in unknown
        ):
            unknown.append(name)

    return {
        "bash_commands": bash_hits,
        "mcp_tools": mcp_hits,
        "delegated_skills": delegated,
        "unknown_delegations": sorted(unknown),
    }


def build_sandbox_context(
    bash_commands: list[str], delegated_skills: list[str]
) -> str:
    """Build runner_context simulation block for detected side effects."""
    lines = [
        f"\n\n{SANDBOX_HEADER}",
        "The skill being evaluated has real-world side effects. When it instructs",
        "you to run any of the following commands, DO NOT execute them. Instead,",
        "return the simulated response shown below:\n",
    ]
    for _pattern, label, response in BASH_SIDE_EFFECTS:
        if label in bash_commands:
            lines.append(f'  {label} → simulate: "{response}"')
    if delegated_skills:
        lines.append("")
        lines.append(
            "This skill also invokes sub-skills with side effects. "
            "Do NOT invoke these skills for real. Instead, simulate success:"
        )
        for skill in sorted(delegated_skills):
            lines.append(f'  /{skill} → simulate: "/{skill} completed successfully"')
    lines.append(
        "\nEvaluate whether the skill attempts the right commands with correct "
        "arguments — but never execute them for real."
    )
    return "\n".join(lines)


def guard_criteria(
    criteria: dict,
    bash_commands: list[str],
    mcp_tools: list[str],
    delegated_skills: list[str],
    artifact_allowed_tools: list[str] | None = None,
) -> dict:
    """Modify criteria to sandbox side effects. Returns summary of changes.

    If artifact_allowed_tools is provided (non-None), each test case's
    `allowed_tools` is intersected with it — tools not declared by the
    artifact itself are removed.
    """
    sandbox_context = (
        build_sandbox_context(bash_commands, delegated_skills)
        if bash_commands or delegated_skills
        else ""
    )
    tests_modified = 0
    tools_removed: list[str] = []

    for tc in criteria.get("test_cases", []):
        modified = False

        # Remove dangerous MCP tools from allowed_tools
        allowed = tc.get("allowed_tools", [])
        if allowed:
            filtered = [
                t
                for t in allowed
                if not any(blocked in t.lower() for blocked in mcp_tools)
            ]
            removed = set(allowed) - set(filtered)
            if removed:
                tc["allowed_tools"] = filtered
                tools_removed.extend(removed)
                modified = True

        # Intersect with artifact-declared allowed_tools frontmatter.
        # Any tool not declared by the artifact is removed from the test case,
        # except harness tools (Skill, AskUserQuestion), which the eval itself
        # needs and no artifact declares. Comparison is on base tool names so
        # a declared scoped form like Bash(git:*) keeps a test's plain Bash.
        if artifact_allowed_tools is not None:
            current = tc.get("allowed_tools", [])
            if current:
                declared_bases = {_base_tool_name(t) for t in artifact_allowed_tools}
                filtered = [
                    t
                    for t in current
                    if _base_tool_name(t) in declared_bases
                    or _base_tool_name(t) in HARNESS_TOOLS
                ]
                removed = set(current) - set(filtered)
                if removed:
                    tc["allowed_tools"] = filtered
                    tools_removed.extend(removed)
                    modified = True

        # Prepend sandbox instructions to runner_context
        if sandbox_context:
            existing = tc.get("runner_context", "")
            if SANDBOX_HEADER not in existing:
                tc["runner_context"] = existing + sandbox_context
                modified = True

        if modified:
            tests_modified += 1

    return {
        "tests_modified": tests_modified,
        "tools_removed": sorted(set(tools_removed)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-path", required=True, help="Path to artifact file")
    parser.add_argument(
        "--criteria-path", required=True, help="Path to eval_criteria.json"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON summary to stdout"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report findings without modifying criteria",
    )
    args = parser.parse_args()

    artifact_path = Path(args.artifact_path)
    criteria_path = Path(args.criteria_path)

    if not artifact_path.exists():
        print(f"Error: artifact not found: {artifact_path}", file=sys.stderr)
        return 1
    if not criteria_path.exists():
        print(f"Error: criteria not found: {criteria_path}", file=sys.stderr)
        return 1

    artifact_content = artifact_path.read_text()

    # Also scan referenced files (references/ directory for skills)
    refs_dir = artifact_path.parent / "references"
    if refs_dir.is_dir():
        for ref_file in refs_dir.glob("*.md"):
            artifact_content += "\n" + ref_file.read_text()

    # Scan for side effects. The artifact's own names never count as
    # delegations (a skill quoting its own invocation is the eval's entry
    # point, not a side effect).
    resolved = artifact_path.resolve()
    self_names = frozenset({resolved.stem.lower(), resolved.parent.name.lower()})
    findings = scan_artifact(artifact_content, self_names)
    bash_commands = findings["bash_commands"]
    mcp_tools = findings["mcp_tools"]
    # Unknown delegations are sandboxed exactly like known ones (fail closed).
    delegated_skills = sorted(
        set(findings["delegated_skills"]) | set(findings["unknown_delegations"])
    )

    # Parse artifact's declared allowed_tools frontmatter (primary SKILL.md only,
    # not references/). Used to filter test-case allowed_tools down to the
    # artifact's declared toolset.
    artifact_allowed = parse_allowed_tools_frontmatter(artifact_path.read_text())

    # Proceed if ANY signal is present (including a declared toolset that may
    # narrow allowed_tools in the criteria).
    if (
        not bash_commands
        and not mcp_tools
        and not delegated_skills
        and artifact_allowed is None
    ):
        if args.json:
            print(
                json.dumps({"side_effects_detected": False, "action": "none"}, indent=2)
            )
        else:
            print("No side effects detected.", file=sys.stderr)
        return 2

    # Load criteria
    criteria_text = criteria_path.read_text()
    criteria = json.loads(criteria_text)

    if not criteria or "test_cases" not in criteria:
        print("Error: invalid criteria file (no test_cases)", file=sys.stderr)
        return 1

    # Guard criteria
    changes = guard_criteria(
        criteria, bash_commands, mcp_tools, delegated_skills, artifact_allowed
    )

    result = {
        "side_effects_detected": True,
        "bash_commands": bash_commands,
        "mcp_tools": mcp_tools,
        "delegated_skills": delegated_skills,
        "unknown_delegations": findings["unknown_delegations"],
        "action": "dry_run" if args.dry_run else "modified",
        "tests_modified": changes["tests_modified"],
        "tools_removed": changes["tools_removed"],
    }

    if not args.dry_run:
        # Write modified criteria back
        with open(criteria_path, "w") as f:
            json.dump(criteria, f, indent=2)
            f.write("\n")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        action = "Would modify" if args.dry_run else "Modified"
        print(
            f"{action} {changes['tests_modified']} test cases. "
            f"Bash side effects: {bash_commands}. "
            f"MCP tools removed: {changes['tools_removed']}.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
