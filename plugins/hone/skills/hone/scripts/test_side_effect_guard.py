#!/usr/bin/env python3
"""Tests for side_effect_guard.py sandboxing and allowed-tools filtering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from side_effect_guard import (
    _base_tool_name,
    guard_criteria,
    parse_allowed_tools_frontmatter,
    scan_artifact,
)


class TestBaseToolName(unittest.TestCase):
    def test_plain_and_scoped_forms(self) -> None:
        self.assertEqual(_base_tool_name("Bash"), "bash")
        self.assertEqual(_base_tool_name("Bash(git:*)"), "bash")
        self.assertEqual(_base_tool_name("Bash(python3:*, ls:*)"), "bash")
        self.assertEqual(_base_tool_name("mcp__x__y"), "mcp__x__y")


class TestAllowedToolsIntersection(unittest.TestCase):
    def _criteria(self, tools: list[str]) -> dict:
        return {"test_cases": [{"allowed_tools": list(tools)}]}

    def test_harness_tools_survive_intersection(self) -> None:
        # Skill invokes the artifact under test and AskUserQuestion is added
        # by criteria_self_repair; neither is ever declared by the artifact.
        criteria = self._criteria(["Read", "Skill", "AskUserQuestion", "Write"])
        guard_criteria(criteria, [], [], [], ["Read", "Grep", "Glob"])
        self.assertEqual(
            criteria["test_cases"][0]["allowed_tools"],
            ["Read", "Skill", "AskUserQuestion"],
        )

    def test_scoped_declaration_keeps_base_tool(self) -> None:
        criteria = self._criteria(["Bash", "Edit"])
        guard_criteria(criteria, [], [], [], ["Bash(git:*)", "Read"])
        self.assertEqual(criteria["test_cases"][0]["allowed_tools"], ["Bash"])

    def test_no_declared_toolset_applies_no_filter(self) -> None:
        criteria = self._criteria(["Read", "Write"])
        guard_criteria(criteria, [], [], [], None)
        self.assertEqual(criteria["test_cases"][0]["allowed_tools"], ["Read", "Write"])


class TestDelegationDetection(unittest.TestCase):
    def test_known_pipeline_skill_detected(self) -> None:
        findings = scan_artifact("Then run /forge to implement the plan.")
        self.assertIn("forge", findings["delegated_skills"])

    def test_unknown_delegation_fails_closed(self) -> None:
        findings = scan_artifact("Finally invoke /my-publish-pipeline to release.")
        self.assertIn("my-publish-pipeline", findings["unknown_delegations"])

    def test_self_name_excluded(self) -> None:
        findings = scan_artifact(
            "Usage: /my-skill --auto runs unattended.",
            self_names=frozenset({"my-skill"}),
        )
        self.assertNotIn("my-skill", findings["unknown_delegations"])

    def test_file_paths_do_not_fire(self) -> None:
        findings = scan_artifact(
            "Files at /tmp/out.json and /usr/bin/python3 and src/spikes/a.py"
        )
        self.assertEqual(findings["unknown_delegations"], [])

    def test_mid_word_slash_does_not_fire(self) -> None:
        findings = scan_artifact("A 30/360 day count and factor/face ratios.")
        self.assertEqual(findings["unknown_delegations"], [])


class TestFrontmatterParsing(unittest.TestCase):
    def test_inline_list(self) -> None:
        content = "---\nname: x\nallowed-tools: [Read, Grep, Bash]\n---\nBody"
        self.assertEqual(
            parse_allowed_tools_frontmatter(content), ["Read", "Grep", "Bash"]
        )

    def test_missing_key_returns_none(self) -> None:
        content = "---\nname: x\n---\nBody"
        self.assertIsNone(parse_allowed_tools_frontmatter(content))


class TestEmptyIntersectionFallback(unittest.TestCase):
    """The guard must never write allowed_tools: [] — the criteria schema
    declares the field non_empty, so an empty list fails the very next
    mandatory validation step with no backup to restore."""

    def test_empty_intersection_falls_back_to_declared_tools(self) -> None:
        criteria = {"test_cases": [{"allowed_tools": ["Bash", "Write"]}]}
        changes = guard_criteria(
            criteria, [], [], [], artifact_allowed_tools=["Read", "Grep"]
        )
        tc = criteria["test_cases"][0]
        self.assertEqual(tc["allowed_tools"], ["Read", "Grep"])
        self.assertEqual(changes["fallbacks_applied"], 1)
        self.assertEqual(sorted(changes["tools_removed"]), ["Bash", "Write"])

    def test_mcp_filter_emptying_list_falls_back_to_safe_default(self) -> None:
        from side_effect_guard import SAFE_FALLBACK_TOOLS

        criteria = {"test_cases": [{"allowed_tools": ["mcp__evil__send"]}]}
        changes = guard_criteria(
            criteria, [], ["mcp__evil"], [], artifact_allowed_tools=None
        )
        tc = criteria["test_cases"][0]
        self.assertEqual(tc["allowed_tools"], list(SAFE_FALLBACK_TOOLS))
        self.assertEqual(changes["fallbacks_applied"], 1)

    def test_nonempty_intersection_untouched_by_fallback(self) -> None:
        criteria = {"test_cases": [{"allowed_tools": ["Read", "Bash"]}]}
        changes = guard_criteria(
            criteria, [], [], [], artifact_allowed_tools=["Read"]
        )
        tc = criteria["test_cases"][0]
        self.assertEqual(tc["allowed_tools"], ["Read"])
        self.assertEqual(changes["fallbacks_applied"], 0)


if __name__ == "__main__":
    unittest.main()
