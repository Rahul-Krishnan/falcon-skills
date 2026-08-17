#!/usr/bin/env python3
"""Tests for side_effect_guard.py sandboxing and allowed-tools filtering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from side_effect_guard import (
    TOOLS_ABSENT,
    TOOLS_PARSED,
    TOOLS_UNPARSED,
    _base_tool_name,
    build_sandbox_context,
    declared_destructive_labels,
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
    def _parse(self, allowed: str) -> tuple[list[str] | None, str]:
        return parse_allowed_tools_frontmatter(
            f"---\nname: x\n{allowed}\n---\nBody"
        )

    def test_inline_list(self) -> None:
        self.assertEqual(
            self._parse("allowed-tools: [Read, Grep, Bash]"),
            (["Read", "Grep", "Bash"], TOOLS_PARSED),
        )

    def test_missing_key_is_absent_not_unparsed(self) -> None:
        # Absent means the artifact declared no prohibition, so no filter
        # applies. Only this state may skip the intersection.
        self.assertEqual(
            parse_allowed_tools_frontmatter("---\nname: x\n---\nBody"),
            (None, TOOLS_ABSENT),
        )

    def test_no_frontmatter_is_absent(self) -> None:
        self.assertEqual(
            parse_allowed_tools_frontmatter("# Just a heading\n"),
            (None, TOOLS_ABSENT),
        )

    def test_block_list(self) -> None:
        self.assertEqual(
            self._parse("allowed-tools:\n  - Read\n  - Grep"),
            (["Read", "Grep"], TOOLS_PARSED),
        )

    def test_bare_inline_scalar(self) -> None:
        self.assertEqual(
            self._parse("allowed-tools: Read, Grep"),
            (["Read", "Grep"], TOOLS_PARSED),
        )

    def test_single_tool_scalar(self) -> None:
        self.assertEqual(self._parse("allowed-tools: Read"), (["Read"], TOOLS_PARSED))

    def test_quoted_scoped_scalar_keeps_scope_intact(self) -> None:
        # The shape memory-cleanup ships. The commas inside Bash(...) belong to
        # the scope; splitting on them produced fragments like "du:*)" and the
        # whole value used to parse to None, disabling the filter entirely.
        tools, status = self._parse(
            'allowed-tools: "Bash(ls:*, du:*, find:*, rm:*), Read, Write, '
            'mcp__falcon-memory__memory_delete"'
        )
        self.assertEqual(status, TOOLS_PARSED)
        self.assertEqual(
            tools,
            [
                "Bash(ls:*, du:*, find:*, rm:*)",
                "Read",
                "Write",
                "mcp__falcon-memory__memory_delete",
            ],
        )

    def test_unquoted_scoped_scalar(self) -> None:
        tools, status = self._parse("allowed-tools: Bash(git status:*), Read, Edit")
        self.assertEqual(status, TOOLS_PARSED)
        self.assertEqual(tools, ["Bash(git status:*)", "Read", "Edit"])

    def test_multiline_flow_sequence(self) -> None:
        # `allowed-tools: [` on its own line, as the official plugin-dev skills
        # write it. frontmatter_field only returns the "[", so the value has to
        # be rejoined from the frontmatter or it reads as unterminated.
        tools, status = self._parse("allowed-tools: [\n  Read,\n  Grep,\n  Bash\n]")
        self.assertEqual(status, TOOLS_PARSED)
        self.assertEqual(tools, ["Read", "Grep", "Bash"])

    def test_unterminated_flow_sequence_is_unparsed(self) -> None:
        self.assertEqual(
            self._parse("allowed-tools: [\n  Read,\n  Grep"), (None, TOOLS_UNPARSED)
        )

    def test_empty_value_is_unparsed_not_absent(self) -> None:
        # The key is there, so the artifact did declare something. Reporting
        # ABSENT here would grant the criteria everything.
        self.assertEqual(self._parse("allowed-tools:"), (None, TOOLS_UNPARSED))


class TestSelfNameExclusionForPipelineSkills(unittest.TestCase):
    """The artifact under test is the eval's entry point, so its own name must
    never be sandboxed. Applied to the unknown loop only, the known-skill loop
    told the executor to simulate the very invocation being measured."""

    def test_pipeline_skill_evaluating_itself_is_not_sandboxed(self) -> None:
        # Directory-name shape: ~/.claude/skills/quench/SKILL.md
        findings = scan_artifact(
            "Usage: /quench --auto. Afterwards run /present to publish.",
            self_names=frozenset({"quench", "skill"}),
        )
        self.assertNotIn("quench", findings["delegated_skills"])
        self.assertIn("present", findings["delegated_skills"])

    def test_file_stem_shape_is_not_sandboxed(self) -> None:
        # Command shape: ~/.claude/commands/forge.md -> stem "forge"
        findings = scan_artifact(
            "Run /forge to implement, then /quench for the PR.",
            self_names=frozenset({"forge", "commands"}),
        )
        self.assertNotIn("forge", findings["delegated_skills"])
        self.assertIn("quench", findings["delegated_skills"])

    def test_every_listed_skill_can_be_evaluated_as_itself(self) -> None:
        from pipeline_skills import SIDE_EFFECTING_SKILLS

        for name in SIDE_EFFECTING_SKILLS:
            with self.subTest(skill=name):
                findings = scan_artifact(
                    f"Usage: /{name} --auto", self_names=frozenset({name, "skill"})
                )
                self.assertNotIn(name, findings["delegated_skills"])
                self.assertNotIn(name, findings["unknown_delegations"])

    def test_other_pipeline_skills_still_sandboxed(self) -> None:
        findings = scan_artifact(
            "Run /smelt, then /forge, then /present.",
            self_names=frozenset({"smithy", "skill"}),
        )
        self.assertEqual(
            sorted(findings["delegated_skills"]), ["forge", "present", "smelt"]
        )


class TestDestructivePatternDetection(unittest.TestCase):
    """A skill whose job is deletion used to scan clean, so an unattended eval
    of it got a sandbox block with no commands in it."""

    def _labels(self, text: str) -> list[str]:
        return scan_artifact(text)["bash_commands"]

    def test_rm_detected(self) -> None:
        self.assertIn("rm", self._labels("Run `rm -rf ~/.claude/state` to clear it."))

    def test_trash_detected(self) -> None:
        self.assertIn("trash", self._labels("Use `trash ~/.claude/shell-snapshots`."))

    def test_find_delete_detected(self) -> None:
        self.assertIn(
            "find -delete", self._labels("find /tmp -name '*.log' -mtime +7 -delete")
        )

    def test_find_exec_rm_detected(self) -> None:
        self.assertIn(
            "find -exec rm", self._labels("find /tmp -name '*.tmp' -exec rm {} +")
        )

    def test_mv_detected(self) -> None:
        self.assertIn("mv", self._labels("mv old.json backup.json"))

    def test_destructive_git_detected(self) -> None:
        labels = self._labels(
            "git reset --hard origin/main\ngit branch -D feature\ngit checkout .\n"
        )
        self.assertIn("git reset --hard", labels)
        self.assertIn("git branch -D", labels)
        self.assertIn("git checkout .", labels)

    def test_network_writes_detected(self) -> None:
        labels = self._labels(
            "curl -X POST https://example.com/hook\n"
            "gh api -X POST repos/o/r/issues\n"
        )
        self.assertIn("curl -X POST", labels)
        self.assertIn("gh api -X POST", labels)

    def test_every_pattern_has_a_simulated_response(self) -> None:
        # BASH_SIDE_EFFECTS is built by keying _SIMULATED_RESPONSES off every
        # shared pattern label, so a pattern added without a response is an
        # import-time KeyError. Assert it here so the failure names the gap.
        from hone_common import BASH_SIDE_EFFECT_PATTERNS
        from side_effect_guard import _SIMULATED_RESPONSES

        for _pattern, label in BASH_SIDE_EFFECT_PATTERNS:
            with self.subTest(label=label):
                self.assertIn(label, _SIMULATED_RESPONSES)

    def test_simulated_responses_name_no_substitute_command(self) -> None:
        # A response that suggests another command is one the executor may run
        # for real. Every simulated reply reports an outcome and stops there.
        from side_effect_guard import _SIMULATED_RESPONSES

        for label, response in _SIMULATED_RESPONSES.items():
            with self.subTest(label=label):
                lowered = response.lower()
                self.assertNotIn("`", response)
                for hint in ("instead", "use ", "run ", "-rf", "--force", "sudo"):
                    self.assertNotIn(hint, lowered)


class TestDeclaredDestructiveScopes(unittest.TestCase):
    """scan_artifact only sees prose. A cleanup skill describes what it deletes
    without spelling the command out, so its declared Bash scope is the only
    signal that it deletes at all."""

    def test_rm_scope_detected(self) -> None:
        self.assertEqual(
            declared_destructive_labels(
                ["Bash(ls:*, du:*, find:*, rm:*, wc:*)", "Read", "Write"]
            ),
            ["rm"],
        )

    def test_trash_and_mv_scopes_detected(self) -> None:
        self.assertEqual(
            declared_destructive_labels(["Bash(trash:*, mv:*)"]), ["trash", "mv"]
        )

    def test_read_only_scopes_are_clean(self) -> None:
        self.assertEqual(
            declared_destructive_labels(
                ["Bash(ls:*, git status:*, grep:*)", "Read", "Grep"]
            ),
            [],
        )

    def test_unscoped_and_absent_declarations_are_clean(self) -> None:
        self.assertEqual(declared_destructive_labels(["Bash", "Read"]), [])
        self.assertEqual(declared_destructive_labels(None), [])


class TestSandboxContextSections(unittest.TestCase):
    def test_command_section_omitted_when_no_bash_commands(self) -> None:
        block = build_sandbox_context([], ["quench"])
        self.assertNotIn("following commands", block)
        self.assertIn('/quench → simulate:', block)

    def test_command_section_present_when_commands_found(self) -> None:
        block = build_sandbox_context(["rm"], [])
        self.assertIn("following commands", block)
        self.assertIn("rm → simulate:", block)

    def test_both_sections_render_together(self) -> None:
        block = build_sandbox_context(["git push"], ["present"])
        self.assertIn("git push → simulate:", block)
        self.assertIn("This skill also invokes sub-skills", block)


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
