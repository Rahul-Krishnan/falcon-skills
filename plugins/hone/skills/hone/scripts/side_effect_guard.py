#!/usr/bin/python3
"""Guard eval criteria against side-effecting commands in the artifact under test.

Scans the artifact for patterns that cause real-world side effects (diff submission,
GChat posting, source control mutations, deletions, network writes) and modifies
eval criteria to sandbox them:
  1. Removes dangerous MCP tools from allowed_tools
  2. Appends simulation instructions to runner_context for Bash-based side effects

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
import os
import re
import sys
from pathlib import Path

# Shared pattern set + frontmatter helpers; the bash patterns, the
# delegation regex, and the sandbox header are also consumed by
# validate_eval_criteria.py's hygiene and allowed-tools checks, so edits to
# any of them belong in hone_common, not here.
from hone_common import (
    BASH_SIDE_EFFECT_PATTERNS,
    DELEGATION_RE,
    DELEGATION_STOPLIST,
    SANDBOX_HEADER,
    frontmatter_field,
    split_frontmatter,
)

# Canonical skill list shared with validate_eval_criteria.py; add names there.
from pipeline_skills import SIDE_EFFECTING_SKILLS

# Simulated responses per pattern label — guard-specific metadata layered on
# the shared patterns. Every shared pattern must have a response here; a
# missing one fails loud at import time (KeyError) rather than silently
# sandboxing without a simulation line.
_SIMULATED_RESPONSES = {
    "git push": "Branch pushed to remote successfully",
    "git push --force": "Force pushed to remote",
    "gh pr create": "Pull request created: #99",
    "gh pr merge": "Pull request #99 merged",
    "gh pr ready": "Pull request #99 is marked as ready for review",
    "gh pr edit": "Pull request #99 updated",
    "gh pr comment": "Comment posted to pull request #99",
    "git commit": "Created commit abc1234def5",
    "mkdir": "mkdir completed",
    "printf > file": "file written",
    "echo > file": "file written",
    "cp": "file copied",
    # Destructive group. Each response reports the outcome the skill expected
    # and nothing else: it must never name a substitute command, because the
    # executor reading it would then run that substitute for real. "Do not
    # execute, report this instead" is the whole contract.
    "rm": "removed (simulated: nothing was deleted)",
    "trash": "moved to trash (simulated: nothing was moved)",
    "find -delete": "matching files removed (simulated: nothing was deleted)",
    "find -exec rm": "matching files removed (simulated: nothing was deleted)",
    "mv": "moved (simulated: nothing was moved)",
    "git reset --hard": "HEAD is now at abc1234 (simulated: working tree untouched)",
    "git branch -D": "Deleted branch (simulated: nothing was deleted)",
    "git checkout .": "working tree restored (simulated: nothing was restored)",
    "git clean -fd": "untracked files removed (simulated: nothing was deleted)",
    # Network writes.
    "curl -X POST": "HTTP 200 OK (simulated: no request was sent)",
    "curl --data": "HTTP 200 OK (simulated: no request was sent)",
    "gh api -X POST": "HTTP 201 Created (simulated: no request was sent)",
    "wget --post-data": "HTTP 200 OK (simulated: no request was sent)",
}

# Bash command patterns that cause real-world side effects.
# Each tuple: (regex_pattern, human_label, simulated_response)
BASH_SIDE_EFFECTS = [
    (pattern, label, _SIMULATED_RESPONSES[label])
    for pattern, label in BASH_SIDE_EFFECT_PATTERNS
]

# MCP tool name patterns to remove from allowed_tools, matched as substrings
# against each tool name (see scan_artifact for why substrings and not \b
# regexes).
#
# Most entries are underscore-anchored write verbs rather than tool names, so
# a newly connected server's delete_/deploy_/save_ tools are covered the day
# it appears instead of the day someone remembers to edit this list. Three
# properties are load-bearing:
#   - the leading underscore keeps the fragment matching a tool NAME segment
#     (mcp__gmail__trash_message, slack_send_message) and not a stray English
#     word;
#   - the trailing underscore, where present, is what keeps a read-only
#     sibling out: "_deploy_" catches deploy_to_vercel but not
#     get_deployment, "_publish_" catches nothing in get_publishable_keys;
#   - read-only verbs (list_, get_, search_, read_) are deliberately absent:
#     this guard narrows what an unattended eval can change, not what it can
#     look at.
_MCP_TOOL_BLOCKLIST_BASE = [
    # Original chat entries, kept as whole names because artifacts name them
    # in prose without the mcp__ prefix.
    "google_chat",
    "send_message",
    "send_message_as_user",
    # Deletion and archival: memory_delete, delete_branch, delete_event,
    # delete_comment, trash_message, trash_thread, trash_file. "_trash"
    # deliberately does not match untrash_*, which restores rather than
    # destroys.
    "_delete",
    "_trash",
    "_remove",
    "_purge",
    # Outbound messages and comments: send_message, slack_send_message,
    # schedule_message, Gmail reply/forward, reply_to_toolbar_thread.
    "_send_",
    "_post_",
    "_reply",
    "_forward",
    "_schedule_",
    # Writes to shared state: save_issue, save_comment, save_document,
    # create_event, create_file, update_file, update_draft, copy_file,
    # share_file, upload_image, prepare_attachment_upload.
    "_create_",
    "_update_",
    "_save_",
    "_write_",
    "_copy_",
    "_share_",
    "_upload",
    # Database and branch mutations: execute_sql, apply_migration,
    # merge_diff, merge_branch, reset_branch, rebase_branch, restore_project.
    "_execute_",
    "_apply_",
    "_merge_",
    "_reset_",
    "_rebase_",
    "_revert_",
    "_restore_",
    "_submit_",
    "_respond_",
    # Publishing and spend: deploy_to_vercel, deploy_edge_function,
    # buy_domain, buy_credits.
    "_deploy_",
    "_publish_",
    "_buy_",
]


def _blocklist_from_env() -> list[str]:
    """Extra patterns from HONE_MCP_TOOL_BLOCKLIST (comma-separated).

    Mirrors pipeline_skills.SIDE_EFFECTING_SKILLS' HONE_SIDE_EFFECTING_SKILLS
    hatch: a server this list has never heard of can be covered without
    editing the module.
    """
    raw = os.environ.get("HONE_MCP_TOOL_BLOCKLIST", "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


MCP_TOOL_BLOCKLIST = _MCP_TOOL_BLOCKLIST_BASE + [
    pattern
    for pattern in _blocklist_from_env()
    if pattern not in _MCP_TOOL_BLOCKLIST_BASE
]

# MCP tool references in artifact text, eg mcp__claude_ai_Gmail__trash_message.
MCP_TOOL_REF_RE = re.compile(r"mcp__[a-z0-9_.-]+__[a-z0-9_]+")

# The sandbox block appended to each test case's runner_context opens with
# hone_common.SANDBOX_HEADER (shared so validate_eval_criteria.py can exempt
# the block from its hygiene scan).

# Harness tools exempt from the allowed-tools intersection: Skill invokes the
# artifact under test, AskUserQuestion is added for error-handling tests, and
# ToolSearch loads deferred tools like AskUserQuestion (score_execution.py
# scores that attempt as gate evidence). Artifacts never declare any of these.
HARNESS_TOOLS = frozenset({"skill", "askuserquestion", "toolsearch"})

# Read-only default used when filtering would otherwise empty a test case's
# allowed_tools (which the criteria schema rejects as invalid).
SAFE_FALLBACK_TOOLS = ("Read", "Grep", "Glob")

# File types read during the bundle scan. A skill's destructive work almost
# never lives in SKILL.md: SKILL.md says "run scripts/purge.py" and purge.py
# is the file that calls rm -rf. hone, workout, smithy and humanize are all
# shaped this way, so a scan of the markdown alone reported "no side effects"
# for exactly the artifacts with the largest blast radius.
SCANNED_SUFFIXES = frozenset(
    {".md", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb", ".pl"}
)

# Directories skipped by the bundle scan: caches, VCS internals, vendored
# dependencies, and eval output (which holds the criteria this guard writes,
# sandbox block and all, so scanning it would re-detect the guard's own
# simulation lines as artifact behaviour).
SKIPPED_SCAN_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "evals",
        "eval-output",
        "eval_output",
        "eval-results",
        "results",
    }
)

# Bounds. This scan runs before every eval, and an artifact can sit anywhere —
# a hook checked in next to a whole repo — so the walk stops at a file count
# and a byte budget instead of reading whatever happens to be beneath it.
MAX_SCAN_FILES = 200
MAX_SCAN_BYTES = 2_000_000

# Fail-closed delegation detection: any /slash-command in the artifact that is
# not a known side-effecting skill is still treated as side-effecting, because
# an unlisted user pipeline (/deploy, /release) escaping the sandbox can run a
# real `git push` during an unattended eval. DELEGATION_RE and
# DELEGATION_STOPLIST live in hone_common (imported above), shared with
# validate_eval_criteria.py so the sandboxer and the auditor agree on what
# counts as a slash invocation.


def _base_tool_name(tool: str) -> str:
    """Normalize a tool string to its lowercase base name.

    'Bash(git:*)' -> 'bash', 'Bash' -> 'bash', 'mcp__x__y' -> 'mcp__x__y'.
    Scoped forms must match their base tool, otherwise a declared
    'Bash(git:*)' would strip a test case's plain 'Bash'.
    """
    return re.split(r"[(\s:]", tool.strip(), maxsplit=1)[0].lower()


# Outcomes of reading the artifact's declared `allowed-tools:` frontmatter.
# The three states are distinct on purpose. ABSENT means the artifact declared
# no prohibition, so no filter applies. PARSED carries the declared list.
# UNPARSED means the key is present but this parser could not read it, which
# used to collapse into ABSENT — reading a declaration the guard cannot
# understand as permission to skip filtering, the opposite of what it said.
TOOLS_ABSENT = "absent"
TOOLS_PARSED = "parsed"
TOOLS_UNPARSED = "unparsed"

_ALLOWED_TOOLS_LINE_RE = re.compile(
    r"^allowed-tools:[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE
)


def _split_tool_items(raw: str) -> list[str]:
    """Split a comma-separated tool list on commas at bracket depth 0.

    `Bash(ls:*, du:*, rm:*)` is one declaration, not three: those commas
    belong to the scope. A plain `raw.split(",")` tore it into fragments like
    `du:*)` whose base name matched no tool at all.
    """
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in raw:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item.strip().strip("'\"").strip() for item in items if item.strip()]


def _unquote(value: str) -> str:
    """Strip one layer of matching surrounding quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].strip()
    return value


def _join_multiline_flow(frontmatter: str) -> str | None:
    """Rejoin an `allowed-tools: [` flow sequence that spans several lines.

    frontmatter_field returns only the field line's tail, which for this shape
    is a bare "[", so the parser saw an unterminated bracket and gave up.
    Returns the whole `[...]` collapsed onto one line, None if never closed.
    """
    m = _ALLOWED_TOOLS_LINE_RE.search(frontmatter)
    if m is None:
        return None
    parts = [m.group(1).strip()]
    for line in frontmatter[m.end():].split("\n")[1:]:
        parts.append(line.strip())
        if "]" in line:
            joined = " ".join(part for part in parts if part)
            return joined[: joined.index("]") + 1]
    return None


def parse_allowed_tools_frontmatter(content: str) -> tuple[list[str] | None, str]:
    """Extract `allowed-tools:` from artifact YAML frontmatter.

    Returns (tools, status). On TOOLS_PARSED, tools is a possibly-empty list
    and an empty one means "no tools allowed". On TOOLS_ABSENT and
    TOOLS_UNPARSED, tools is None; callers must keep those two apart, because
    only ABSENT means the artifact granted everything.

    Every shape below appears in installed skills:
      allowed-tools: [Read, Grep]
      allowed-tools: [                    (flow sequence over several lines)
        Read,
        Grep ]
      allowed-tools:
        - Read
      allowed-tools: Read, Grep
      allowed-tools: "Bash(ls:*, du:*, rm:*), Read, Write"
    """
    fm = split_frontmatter(content)
    if fm is None:
        return None, TOOLS_ABSENT
    value = frontmatter_field(fm, "allowed-tools")
    if value is None:
        # frontmatter_field returns None both for an absent key and for a bare
        # key with nothing under it. Those are different declarations, so
        # re-check the raw text rather than reporting the second as ABSENT and
        # granting the criteria everything.
        if _ALLOWED_TOOLS_LINE_RE.search(fm):
            return None, TOOLS_UNPARSED
        return None, TOOLS_ABSENT
    value = value.strip()
    if not value:
        return None, TOOLS_UNPARSED

    # Flow sequence, on one line or continued across several.
    if value.startswith("["):
        if not value.endswith("]"):
            rejoined = _join_multiline_flow(fm)
            if rejoined is None:
                return None, TOOLS_UNPARSED
            value = rejoined
        return _split_tool_items(value[1:-1]), TOOLS_PARSED

    # Block form (frontmatter_field returns the dedented block):
    #   allowed-tools:
    #     - Read
    #     - Grep
    items = re.findall(r"^\s*-\s+(.*)$", value, re.MULTILINE)
    if items:
        return [_unquote(t.strip()) for t in items if t.strip()], TOOLS_PARSED

    # Inline scalar, bare or quoted. Restricted to a single line so a
    # multi-line block that is not a `- item` list reports UNPARSED instead of
    # being comma-split across newlines into fragments.
    if "\n" not in value:
        tools = _split_tool_items(_unquote(value))
        if tools:
            return tools, TOOLS_PARSED

    return None, TOOLS_UNPARSED


# Bash scope heads that are destructive whatever their arguments, mapped to
# the sandbox label that simulates them. scan_artifact can only see prose, and
# a cleanup skill routinely describes what it deletes without spelling the
# command out, so the declared scope is the only signal that it deletes at all:
# memory-cleanup declares Bash(rm:*) and names no command in its body.
DESTRUCTIVE_SCOPE_COMMANDS = {
    "rm": "rm",
    "rmdir": "rm",
    "shred": "rm",
    "trash": "trash",
    "mv": "mv",
}


def declared_destructive_labels(artifact_allowed_tools: list[str] | None) -> list[str]:
    """Sandbox labels implied by destructive commands in declared Bash scopes.

    `Bash(ls:*, du:*, rm:*)` yields ["rm"]. Order of first appearance, deduped.
    """
    labels: list[str] = []
    for tool in artifact_allowed_tools or []:
        if _base_tool_name(tool) != "bash" or "(" not in tool:
            continue
        scope = tool[tool.find("(") + 1 : tool.rfind(")")]
        for token in scope.split(","):
            token = token.strip()
            if not token:
                continue
            head = token.split(":", 1)[0].strip().split(" ", 1)[0]
            label = DESTRUCTIVE_SCOPE_COMMANDS.get(head.lower())
            if label and label not in labels:
                labels.append(label)
    return labels


def scan_artifact(content: str, self_names: frozenset[str] = frozenset()) -> dict:
    """Scan artifact content for side-effecting patterns.

    Returns dict with 'bash_commands', 'mcp_tools', 'delegated_skills', and
    'unknown_delegations'. `self_names` are the artifact's own names (dir and
    file stem), excluded from BOTH delegation loops so a skill's usage examples
    of itself never sandbox the invocation the eval depends on.
    """
    bash_hits: list[str] = []
    for pattern, label, _ in BASH_SIDE_EFFECTS:
        if re.search(pattern, content, re.IGNORECASE):
            bash_hits.append(label)

    # Substring match, mirroring the guard_criteria removal filter: real MCP
    # tool names join segments with underscores (mcp__server__tool_name), and
    # underscores are word characters, so \b-bounded regexes never fire inside
    # them (\bsend_message\b cannot match ...slack_send_message). Lookaround
    # boundaries fail the same way on the leading "_".
    #
    # Verb fragments are matched against the mcp__server__tool names found in
    # the text rather than against the prose, because "_write_" and "_create_"
    # also occur in ordinary Python (`_write_report`), and now that the scan
    # reads a skill's bundled scripts, matching those against the raw text
    # would report an MCP write tool for every skill that ships code. Whole
    # names keep matching the raw text, so an artifact that says "posts to
    # google_chat" without the mcp__ prefix is still caught.
    content_lower = content.lower()
    mcp_names = {m.group(0) for m in MCP_TOOL_REF_RE.finditer(content_lower)}
    mcp_hits: list[str] = []
    for tool_pattern in MCP_TOOL_BLOCKLIST:
        if tool_pattern.startswith("_"):
            found = any(tool_pattern in name for name in mcp_names)
        else:
            found = tool_pattern in content_lower
        if found:
            mcp_hits.append(tool_pattern)

    # Detect invocations of known side-effecting skills/commands
    delegated: list[str] = []
    for skill_name in SIDE_EFFECTING_SKILLS:
        # self_names is checked here as well as in the unknown loop below. The
        # artifact under test is the eval's entry point, so sandboxing its own
        # name instructs the executor to simulate the very invocation being
        # measured: the run scores nothing while the guard summary still
        # reports test cases modified, and nothing signals the neutered eval.
        # A pipeline skill evaluating itself (quench, forge, present) is the
        # normal case, not an edge one.
        if skill_name in self_names:
            continue
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
    """Build runner_context simulation block for detected side effects.

    Each section is emitted only when it has entries. The command preamble
    used to print unconditionally, so an artifact whose side effects are all
    delegations announced a list of commands and then showed none.
    """
    lines = [f"\n\n{SANDBOX_HEADER}"]
    matched = [
        (label, response)
        for _pattern, label, response in BASH_SIDE_EFFECTS
        if label in bash_commands
    ]
    if matched:
        lines.append(
            "The skill being evaluated has real-world side effects. When it instructs"
        )
        lines.append(
            "you to run any of the following commands, DO NOT execute them. Instead,"
        )
        lines.append("return the simulated response shown below:\n")
        for label, response in matched:
            lines.append(f'  {label} → simulate: "{response}"')
    if delegated_skills:
        if matched:
            lines.append("")
            lines.append("This skill also invokes sub-skills with side effects.")
        else:
            lines.append(
                "The skill being evaluated invokes sub-skills with real-world "
                "side effects."
            )
        lines.append(
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
    fallbacks_applied = 0

    # Filter against the full MCP_TOOL_BLOCKLIST, not just scan hits:
    # mcp_tools only carries patterns the artifact text happens to name, and
    # LLM-generated criteria can grant a dangerous tool the artifact never
    # mentions. Keying the filter off artifact text alone fails open.
    blocked_patterns = set(mcp_tools) | set(MCP_TOOL_BLOCKLIST)

    for tc in criteria.get("test_cases", []):
        modified = False

        # Remove dangerous MCP tools from allowed_tools
        allowed = tc.get("allowed_tools", [])
        if allowed:
            filtered = [
                t
                for t in allowed
                if not any(blocked in t.lower() for blocked in blocked_patterns)
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

        # Never write allowed_tools: [] — the criteria schema declares it
        # non_empty, and the next mandatory validation step has no backup to
        # restore. Fall back to the declared frontmatter set (MCP-filtered),
        # else a read-only default.
        if tc.get("allowed_tools") == [] and modified:
            # Filter against blocked_patterns (scan hits + full blocklist),
            # exactly like the removal filter above: filtering on scan hits
            # alone re-adds the very tool the blocklist just removed whenever
            # the artifact frontmatter declares it.
            fallback = [
                t
                for t in (artifact_allowed_tools or [])
                if not any(blocked in t.lower() for blocked in blocked_patterns)
            ]
            if not fallback:
                fallback = list(SAFE_FALLBACK_TOOLS)
            tc["allowed_tools"] = fallback
            fallbacks_applied += 1

        # Append sandbox instructions to runner_context
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
        "fallbacks_applied": fallbacks_applied,
    }


def collect_scan_text(artifact_path: Path) -> tuple[str, list[str]]:
    """Artifact text plus every file the artifact ships beside it.

    Returns (combined_text, relative paths of the bundled files read). The
    walk covers the artifact's own directory tree — scripts/, references/ and
    any other subdirectory — which is where a skill keeps the code that
    actually deletes and publishes; the previous scan read SKILL.md and
    references/*.md only, so a SKILL.md whose whole body was
    `python3 scripts/purge.py` matched no pattern and scanned clean.

    The walk stays inside artifact_path.parent: symlinks are not followed and
    any entry resolving outside that root is skipped, so it can never wander
    up into the operator's home directory. MAX_SCAN_FILES and MAX_SCAN_BYTES
    bound the cost.
    """
    text = artifact_path.read_text(errors="replace")
    bundled: list[str] = []
    try:
        root = artifact_path.parent.resolve()
        artifact_resolved = artifact_path.resolve()
    except OSError:
        return text, bundled

    budget = MAX_SCAN_BYTES
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIPPED_SCAN_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if len(bundled) >= MAX_SCAN_FILES or budget <= 0:
                return text, bundled
            path = Path(dirpath) / name
            if path.suffix.lower() not in SCANNED_SUFFIXES or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                if resolved == artifact_resolved or not resolved.is_relative_to(root):
                    continue
                blob = path.read_text(errors="replace")
            except OSError:
                continue
            budget -= len(blob)
            text += "\n" + blob
            bundled.append(str(resolved.relative_to(root)))
    return text, bundled


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

    # Scan the artifact and everything it ships alongside it: scripts/,
    # references/ at any depth, and non-markdown reference files, all of which
    # the old references/*.md glob missed.
    artifact_content, bundled_files = collect_scan_text(artifact_path)

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
    artifact_allowed, tools_status = parse_allowed_tools_frontmatter(
        artifact_path.read_text()
    )
    if tools_status == TOOLS_UNPARSED:
        print(
            f"Warning: {artifact_path} declares allowed-tools but the value could "
            "not be parsed; no tool narrowing was applied to the test cases.",
            file=sys.stderr,
        )

    # A declared destructive scope counts as a detected command even when the
    # body never spells the invocation out. Without this, a skill whose whole
    # job is deletion scanned clean and its unattended eval ran holding real rm.
    declared_destructive = declared_destructive_labels(artifact_allowed)
    for label in declared_destructive:
        if label not in bash_commands:
            bash_commands.append(label)

    # Load criteria before deciding "no side effects": the MCP blocklist is
    # enforced on the criteria's own allowed_tools, so a clean artifact scan
    # alone cannot prove the criteria grant nothing dangerous.
    criteria_text = criteria_path.read_text()
    try:
        criteria = json.loads(criteria_text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid criteria JSON: {exc}", file=sys.stderr)
        return 1

    if not criteria or "test_cases" not in criteria:
        print("Error: invalid criteria file (no test_cases)", file=sys.stderr)
        return 1

    changes = guard_criteria(
        criteria, bash_commands, mcp_tools, delegated_skills, artifact_allowed
    )

    # Genuinely clean only when the artifact shows no signals AND the guard
    # removed nothing from the criteria themselves. An unparseable
    # allowed-tools declaration is never clean: the artifact asked for a
    # narrower toolset than the criteria grant and the guard could not honour
    # it, which the operator has to see rather than read "no side effects".
    if (
        not bash_commands
        and not mcp_tools
        and not delegated_skills
        and artifact_allowed is None
        and tools_status != TOOLS_UNPARSED
        and changes["tests_modified"] == 0
    ):
        if args.json:
            print(
                json.dumps({"side_effects_detected": False, "action": "none"}, indent=2)
            )
        else:
            print("No side effects detected.", file=sys.stderr)
        return 2

    result = {
        "side_effects_detected": True,
        "bash_commands": bash_commands,
        "mcp_tools": mcp_tools,
        "delegated_skills": delegated_skills,
        "unknown_delegations": findings["unknown_delegations"],
        "allowed_tools_status": tools_status,
        "declared_destructive": declared_destructive,
        "bundled_files_scanned": len(bundled_files),
        "action": "dry_run" if args.dry_run else "modified",
        "tests_modified": changes["tests_modified"],
        "tools_removed": changes["tools_removed"],
        "fallbacks_applied": changes["fallbacks_applied"],
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
