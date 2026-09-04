#!/usr/bin/env python3
"""Tests for check_overfit.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from check_overfit import (  # noqa: E402
    MARKDOWN_SYNTAX_CHARS,
    check_overfit,
    classify_item,
)

ARTIFACT = (
    "# Demo Skill\n"
    "Run the pipeline and report the outcome to the user clearly.\n"
    "Always emit a structured gate event before leaving a phase.\n"
)


def _ngrams_of(text):
    from check_overfit import _ngrams, _normalize

    return _ngrams(_normalize(text), 6)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.ngrams = _ngrams_of(ARTIFACT)

    def test_plain_outcome_item(self):
        item = classify_item(
            "Identified the root cause and proposed a working fix", self.ngrams, "demo"
        )
        self.assertEqual(item["class"], "outcome")

    def test_lifted_phrase_is_vocabulary(self):
        item = classify_item(
            "Agent must emit a structured gate event before leaving a phase",
            self.ngrams,
            "demo",
        )
        self.assertEqual(item["class"], "vocabulary")

    def test_script_name_is_technique(self):
        item = classify_item("Agent ran structural_audit.py", self.ngrams, "demo")
        self.assertEqual(item["class"], "technique")

    def test_numbered_phase_is_technique(self):
        item = classify_item("Agent completed Phase 2 before exiting", self.ngrams, "demo")
        self.assertEqual(item["class"], "technique")

    def test_artifact_name_is_technique(self):
        item = classify_item("Agent invoked demo correctly", self.ngrams, "demo")
        self.assertEqual(item["class"], "technique")

    def test_short_overlap_is_not_vocabulary(self):
        # "report the outcome" is three words: ordinary English, not lifted.
        item = classify_item("Agent should report the outcome", self.ngrams, "demo")
        self.assertEqual(item["class"], "outcome")


class TestCheckOverfit(unittest.TestCase):
    def test_required_absent_is_exempt(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {
                    "required_absent": ["Phase 1", "structural_audit"],
                    "checks": [{"description": "Reached a correct result"}],
                }
            ],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["items_exempt_required_absent"], 2)
        self.assertEqual(report["counts"]["outcome"], 1)
        self.assertEqual(report["verdict"], "within_threshold")

    def test_overfitted_set_is_flagged(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {"checks": [{"description": "Agent ran structural_audit.py"}]},
                {"checks": [{"description": "Agent completed Phase 1"}]},
                {"checks": [{"description": "Reached a correct result"}]},
            ],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["verdict"], "overfitted")
        self.assertEqual(len(report["flagged"]), 2)

    def test_only_top_rubric_band_is_scored(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {
                    "checks": [
                        {
                            "description": "Reached a correct result",
                            "rubric": {
                                "1": "Agent ran structural_audit.py",
                                "5": "Agent solved the user's problem",
                            },
                        }
                    ]
                }
            ],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        # The failing band mentions a script but is not what scoring well means.
        self.assertEqual(report["counts"]["technique"], 0)
        self.assertEqual(report["items_classified"], 2)

    def test_empty_criteria_is_not_measurable_rather_than_passing(self):
        """Zero classifiable items clears nothing: Step 6a is a mandatory gate."""
        report = check_overfit({"skill_name": "demo", "test_cases": []}, ARTIFACT, "demo", 0.34)
        self.assertIsNone(report["overfit_ratio"])
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["items_classified"], 0)

    def test_only_required_absent_items_is_not_measurable(self):
        """`required_absent` is exempt by construction, so it scores nothing."""
        criteria = {
            "skill_name": "demo",
            "test_cases": [{"id": "t1", "required_absent": ["Phase 2"]}],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["items_exempt_required_absent"], 1)

    def test_an_empty_artifact_is_not_measurable_rather_than_passing(self):
        """Same hole as zero classifiable items, on the artifact side: with
        nothing to compare against, every lift test is vacuous and every item
        classifies `outcome` for ratio 0.0, clearing the mandatory gate on no
        evidence."""
        criteria = {
            "skill_name": "demo",
            "test_cases": [{
                "id": "t1",
                "required_present": ["validate_handoff"],
                "checks": [{"description": "Ran Phase 2 and reported the outcome"}],
            }],
        }
        report = check_overfit(criteria, "", "demo", 0.34)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIsNone(report["overfit_ratio"])
        self.assertIn("artifact", report["reason"])

    def test_a_whitespace_only_artifact_is_not_measurable(self):
        """A truncated write leaves whitespace, not an empty string."""
        criteria = {"skill_name": "demo",
                    "test_cases": [{"checks": [{"description": "Did the thing"}]}]}
        self.assertEqual(
            check_overfit(criteria, "\n \t\n", "demo", 0.34)["verdict"],
            "not_measurable",
        )


class TestCriteriaRootShape(unittest.TestCase):
    """A non-object criteria root is a usage error, not an AttributeError."""

    def test_a_list_rooted_criteria_file_exits_2(self):
        import subprocess
        import sys
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "criteria.json")
            with open(path, "w") as handle:
                handle.write('[{"id": "TC-001"}]')
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_overfit.py"),
                    path,
                    "--artifact", path,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must be a JSON object", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_directory_criteria_path_exits_2(self):
        """A directory reaches open() as IsADirectoryError, not exit 2."""
        import subprocess
        import sys
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_overfit.py"),
                    tmp,
                    "--artifact", tmp,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot read criteria file", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TestEnrichmentAnchorsAreVisible(unittest.TestCase):
    """Step 6 enrichment appends artifact identifiers to `required_present`.

    Those are the purest vocabulary lift there is, and the NGRAM_SIZE=6 prose
    rule cannot see a single token, so they classified `outcome` and padded
    the denominator: enriching a set used to *lower* its ratio.
    """

    ARTIFACT = (
        "# Demo Skill\n"
        "Call validate_handoff before the gate, then record gate_compliance.\n"
        "Sections start with ## in the report.\n"
    )

    def _report(self, present):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {
                    "required_present": present,
                    "checks": [{"description": "Reached a correct result"}],
                }
            ],
        }
        return check_overfit(criteria, self.ARTIFACT, "demo", 0.34)

    def test_lifted_identifier_anchor_is_vocabulary(self):
        report = self._report(["validate_handoff", "gate_compliance"])
        self.assertEqual(report["counts"]["vocabulary"], 2)
        self.assertEqual(report["counts"]["outcome"], 1)

    def test_generic_markdown_syntax_is_not_a_lift(self):
        """`##` is in every markdown artifact by construction, and
        phase1-evaluation.md recommends it as the minimal structural check.
        Flagging it made the classifier report its own documented practice as
        recitation, on a rule with no collision guard of its own."""
        report = self._report(["##", "---", "```"])
        self.assertEqual(report["counts"]["vocabulary"], 0)

    def test_distinctive_markup_is_still_a_lift(self):
        """The exemption is for markdown syntax, not for all punctuation:
        artifact-specific decoration reproduced verbatim is still recitation."""
        criteria = {
            "skill_name": "demo",
            "test_cases": [{"required_present": ["▓▒░"]}],
        }
        artifact = self.ARTIFACT + "Banner: ▓▒░\n"
        report = check_overfit(criteria, artifact, "demo", 0.34)
        self.assertEqual(report["counts"]["vocabulary"], 1)

    def test_an_anchor_absent_from_the_artifact_is_not_a_lift(self):
        self.assertEqual(self._report(["never_written"])["counts"]["vocabulary"], 0)

    def test_ordinary_short_anchors_are_not_flagged(self):
        """`OK`/`id`/`42` substring-match almost any artifact; flagging a
        literal assertion on a common token would inflate the gated ratio."""
        report = self._report(["OK", "id", "42"])
        self.assertEqual(report["counts"]["vocabulary"], 0)

    def test_enrichment_can_no_longer_dilute_the_ratio(self):
        """Adding artifact-derived anchors must not move the verdict toward
        `within_threshold`; before the fix each one landed in the denominator
        only."""
        bare = self._report([])
        enriched = self._report(["validate_handoff", "gate_compliance", "##"])
        self.assertLess(bare["overfit_ratio"], enriched["overfit_ratio"])
        self.assertEqual(enriched["verdict"], "overfitted")

    def test_a_multi_word_anchor_absent_from_the_artifact_is_not_a_lift(self):
        """The anchor rule is verbatim containment, not word membership:
        `record the gate` uses only artifact words but never in that order."""
        report = self._report(["record the gate"])
        self.assertEqual(report["counts"]["vocabulary"], 0)

    def test_a_short_verbatim_prose_anchor_is_a_lift(self):
        """The identifier/markup rule left the dilution loophole open one step
        out: `before the gate` is prose, is under NGRAM_SIZE, is verbatim in
        the artifact, and used to land in the denominator as `outcome`."""
        report = self._report(["before the gate"])
        self.assertEqual(report["counts"]["vocabulary"], 1)

    def test_recitation_anchors_cannot_clear_the_gate(self):
        """The reported exploit: appending verbatim anchors moved the ratio
        *down*, so an overfitted set cleared the threshold by adding more
        recitation checks."""
        bare = self._report([])
        padded = self._report(["before the gate", "start with", "in the report"])
        self.assertGreater(padded["overfit_ratio"], bare["overfit_ratio"])

    def test_an_anchor_matches_across_a_separator_swap(self):
        """`gate-compliance` and the artifact's `gate_compliance` are the same
        lift; a raw substring test scores it as an outcome item."""
        self.assertEqual(self._report(["gate-compliance"])["counts"]["vocabulary"], 1)

    def test_an_anchor_matches_across_a_line_break(self):
        """An anchor that evades the check by wrapping is free denominator."""
        criteria = {
            "skill_name": "demo",
            "test_cases": [{"required_present": ["before the gate"]}],
        }
        wrapped = "Call validate_handoff before\nthe gate, then record it.\n"
        report = check_overfit(criteria, wrapped, "demo", 0.34)
        self.assertEqual(report["counts"]["vocabulary"], 1)


class TestFlaggedItemsAreLocatable(unittest.TestCase):
    """Step 6a says "rewrite flagged items", so a flagged item has to name the
    test case and the field it came from, and must not arrive truncated."""

    def test_flagged_entries_carry_case_id_and_location(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {"id": "TC-001",
                 "checks": [{"description": "Agent ran structural_audit.py"}]},
                {"id": "TC-002",
                 "checks": [{"description": "Agent ran structural_audit.py"}]},
            ],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        located = {(i["case_id"], i["location"]) for i in report["flagged"]}
        self.assertEqual(
            located,
            {("TC-001", "checks[0].description"), ("TC-002", "checks[0].description")},
        )

    def test_a_case_without_an_id_is_located_by_index(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [{"checks": [{"description": "Agent completed Phase 2"}]}],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["flagged"][0]["case_id"], "test_cases[0]")

    def test_a_rubric_band_names_its_check_and_band(self):
        criteria = {
            "skill_name": "demo",
            "test_cases": [
                {"id": "TC-001",
                 "checks": [{"description": "Reached a correct result",
                             "rubric": {"1": "no", "5": "Agent completed Phase 2"}}]}
            ],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["flagged"][0]["location"], "checks[0].rubric[5]")

    def test_flagged_text_is_not_truncated(self):
        long_text = "Agent completed Phase 2 after " + "a very long preamble " * 20
        criteria = {
            "skill_name": "demo",
            "test_cases": [{"id": "TC-001", "checks": [{"description": long_text}]}],
        }
        report = check_overfit(criteria, ARTIFACT, "demo", 0.34)
        self.assertEqual(report["flagged"][0]["text"], long_text)


class TestSkillNameRuleIgnoresOrdinaryWords(unittest.TestCase):
    """A skill named for a common verb must not flag its own outcome checks."""

    def test_a_one_word_name_used_as_a_verb_stays_outcome(self):
        item = classify_item(
            "Committed the reviewed changes and reported the result", set(), "commit"
        )
        self.assertEqual(item["class"], "outcome")

    def test_a_one_word_name_in_slash_form_is_technique(self):
        item = classify_item("Agent reached the /commit output", set(), "commit")
        self.assertEqual(item["class"], "technique")

    def test_a_multi_segment_name_still_flags_bare(self):
        item = classify_item("Ran temper-rework end to end", set(), "temper-rework")
        self.assertEqual(item["class"], "technique")


if __name__ == "__main__":
    unittest.main()


class TestArtifactPathIsNeverGuessed(unittest.TestCase):
    """`--artifact` is required. The old default of `~/.claude/skills/<name>/
    SKILL.md` is the path phase1-evaluation.md says never to hardcode, and a
    stale copy there returned within_threshold against a file that was not
    the artifact under test, with nothing on stderr."""

    def test_omitting_the_artifact_is_a_usage_error(self):
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "criteria.json")
            with open(path, "w") as handle:
                handle.write('{"skill_name": "hone", "test_cases": []}')
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_overfit.py"),
                    path,
                    "--json",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": tmp},
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--artifact", proc.stderr)
        self.assertEqual(proc.stdout, "")


class TestTechniquePatternsCoverTheWorkflowVocabulary(unittest.TestCase):
    """The step rule stopped at a digit, so the lettered sub-steps this very
    workflow uses (Step 6a, 9a, 3a) classified `outcome` and a recitation
    check about them lowered the ratio instead of raising it; the script rule
    was case-sensitive; the invocation rule only knew `invoked the skill`."""

    def _class(self, text):
        return classify_item(text, set(), "")["class"]

    def test_a_lettered_sub_step_is_technique(self):
        for text in ("Completes Step 6a before scoring", "runs step 9a", "Phase 3A exit"):
            self.assertEqual(self._class(text), "technique", text)

    def test_a_bare_step_number_is_still_technique(self):
        self.assertEqual(self._class("Runs Step 6 first"), "technique")

    def test_a_capitalised_script_name_is_technique(self):
        self.assertEqual(self._class("Runs Score_Execution.py first"), "technique")

    def test_invocation_verb_forms_are_technique(self):
        for text in (
            "called the skill",
            "dispatched the skill",
            "calls the /hone skill",
            "invoking the skill",
            "uses the hone skill",
            "ran the skill",
        ):
            self.assertEqual(self._class(text), "technique", text)

    def test_the_word_skill_in_ordinary_prose_stays_outcome(self):
        for text in (
            "the user calls their skill set impressive",
            "a skilled reply that covers the edge case",
        ):
            self.assertEqual(self._class(text), "outcome", text)


class TestIntegerIdsAreIds(unittest.TestCase):
    def test_an_id_of_zero_is_not_replaced_by_the_index_label(self):
        criteria = {"test_cases": [
            {"id": 0, "checks": [{"description": "Runs step 1"}]},
        ]}
        flagged = check_overfit(criteria, "some artifact words here", "", 0.34)["flagged"]
        self.assertEqual(flagged[0]["case_id"], "0")


class TestExemptAnchorsLeaveTheDenominator(unittest.TestCase):
    """r3-B2. Generic-markdown anchors were exempt from the lift test but still
    classified `outcome` and counted in `total`, so padding `required_present`
    with `##`/`---`/`**` diluted the ratio and flipped `overfitted` to
    `within_threshold`: the exact exploit the anchor rule exists to close,
    reopened on the one anchor shape it exempts."""

    ARTIFACT = (
        "# Title\n\nRun validate_handoff then gate_compliance.\n\n"
        "## Section\n\n---\n\n**bold**\n```\ncode\n```\n"
    )

    def _report(self, present):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": present},
        ]}
        return check_overfit(criteria, self.ARTIFACT, "x", 0.34)

    def test_padding_with_generic_markdown_cannot_clear_the_gate(self):
        bare = self._report(["validate_handoff", "gate_compliance"])
        padded = self._report(
            ["validate_handoff", "gate_compliance", "#", "##", "---", "```", "**"]
        )
        self.assertEqual(bare["verdict"], "overfitted")
        self.assertEqual(padded["verdict"], "overfitted")
        self.assertEqual(padded["overfit_ratio"], bare["overfit_ratio"])

    def test_exempt_anchors_are_counted_separately_not_as_outcome(self):
        report = self._report(["validate_handoff", "#", "##", "---"])
        self.assertEqual(report["items_classified"], 1)
        self.assertEqual(report["counts"]["outcome"], 0)
        self.assertEqual(report["items_exempt_contentless"], 3)

    def test_only_exempt_anchors_is_not_measurable(self):
        report = self._report(["##", "---"])
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["items_exempt_contentless"], 2)


class TestMalformedCriteriaFieldsAreNotScored(unittest.TestCase):
    """r3-S4. A string `required_present` was iterated character by character
    into one-char `outcome` items (sixteen of them cleared the gate at ratio
    0.0), and the rubric's top band was `max` over keys that all tied at -1,
    so a non-numeric rubric picked whichever key JSON order served first."""

    ARTIFACT = "Run validate_handoff now and report the result."

    def test_a_string_required_present_yields_no_items(self):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": "validate_handoff"},
        ]}
        report = check_overfit(criteria, self.ARTIFACT, "x", 0.34)
        self.assertEqual(report["items_classified"], 0)
        self.assertEqual(report["verdict"], "not_measurable")

    def test_a_string_required_absent_is_not_counted_as_exempt(self):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_absent": "phase",
             "checks": [{"description": "Reached a correct result"}]},
        ]}
        report = check_overfit(criteria, self.ARTIFACT, "x", 0.34)
        self.assertEqual(report["items_exempt_required_absent"], 0)

    def test_a_rubric_with_no_numeric_band_is_skipped(self):
        for rubric in (
            {"excellent": "Runs Step 3 exactly", "poor": "Correct result"},
            {"poor": "Correct result", "excellent": "Runs Step 3 exactly"},
        ):
            criteria = {"skill_name": "x", "test_cases": [
                {"id": "t1", "checks": [{"description": "ok", "rubric": rubric}]},
            ]}
            report = check_overfit(criteria, self.ARTIFACT, "x", 0.34)
            self.assertEqual(report["items_classified"], 1, rubric)
            self.assertEqual(report["counts"]["technique"], 0, rubric)

    def test_the_numeric_top_band_wins_regardless_of_key_order(self):
        for rubric in (
            {"5": "Runs Step 3 exactly", "1": "bad", "n/a": "skipped"},
            {"n/a": "skipped", "1": "bad", "5": "Runs Step 3 exactly"},
        ):
            criteria = {"skill_name": "x", "test_cases": [
                {"id": "t1", "checks": [{"description": "ok", "rubric": rubric}]},
            ]}
            report = check_overfit(criteria, self.ARTIFACT, "x", 0.34)
            self.assertEqual(report["counts"]["technique"], 1, rubric)
            self.assertEqual(report["flagged"][0]["location"], "checks[0].rubric[5]")


class TestTechniqueRulesDoNotOverFire(unittest.TestCase):
    """r3-S6. The script rule matched JavaScript technology names (`Node.js`)
    as bundled scripts, and the single-word skill-name rule matched the name
    as any path segment (`~/forge/output.md`), so a JS-oriented suite or a
    skill whose name is a directory in its own output paths was pushed over
    the threshold with nothing legitimately rewritable."""

    def _class(self, text, name=""):
        return classify_item(text, set(), name)["class"]

    def test_a_javascript_technology_name_is_outcome(self):
        for text in (
            "Output is a valid Node.js project",
            "built with vue.js and Next.js",
            "ships a three.js scene",
        ):
            self.assertEqual(self._class(text), "outcome", text)

    def test_a_lowercase_js_script_is_still_technique(self):
        self.assertEqual(self._class("runs build_index.js then reports"), "technique")

    def test_a_capitalised_py_script_is_still_technique(self):
        self.assertEqual(self._class("Runs Score_Execution.py first"), "technique")

    def test_the_name_as_a_path_segment_is_outcome(self):
        for text in (
            "writes the report to ~/forge/output.md",
            "saved under src/forge/cli",
            "see /forge.md",
        ):
            self.assertEqual(self._class(text, "forge"), "outcome", text)

    def test_the_name_in_slash_form_is_still_technique(self):
        for text in ("invoke /forge on the branch", "/forge writes a plan"):
            self.assertEqual(self._class(text, "forge"), "technique", text)


class TestMixedCharacterMarkdownIsGeneric(unittest.TestCase):
    """r3-N1. The exemption covered a run of ONE structural character, so the
    table separator `|---|` built from the same characters as `---` was
    flagged as a verbatim lift against any artifact with a table in it."""

    ARTIFACT = "| a | b |\n|---|---|\nStep 4 -> Step 5\n<!-- note -->\nBanner: ▓▒░\n"

    def _vocabulary(self, anchor):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": [anchor],
             "checks": [{"description": "Reached a correct result"}]},
        ]}
        return check_overfit(criteria, self.ARTIFACT, "x", 0.34)["counts"]["vocabulary"]

    def test_structural_markdown_built_from_several_characters_is_exempt(self):
        for anchor in ("|---|", "|:--|", "->", "<!--"):
            self.assertEqual(self._vocabulary(anchor), 0, anchor)

    def test_non_ascii_decoration_is_still_a_lift(self):
        self.assertEqual(self._vocabulary("▓▒░"), 1)


class TestEveryExemptCharacterReachesTheExemption(unittest.TestCase):
    """r4-B1. Third appearance of the denominator-dilution exploit, and the
    first test written against the whole exempt set rather than one example.

    `MARKUP_ANCHOR`/`BARE_MARKUP` used `[^\\w\\s]`, which never matches `_`
    because `\\w` counts it as a word character. So `___` -- a markdown
    horizontal rule, and `_` is a MARKDOWN_SYNTAX_CHARS member -- reached
    neither pattern, never reached `_is_generic_markdown`, and was classified
    `outcome`. 17 of them dropped a ratio-1.0 criteria set to 0.1053 and
    cleared Step 6a's mandatory gate on no evidence.

    Two earlier fixes each closed one door on this exploit and left another
    open, so this asserts the property per character, driven off the set
    itself: a character added to MARKDOWN_SYNTAX_CHARS that the patterns
    cannot reach fails here rather than at the gate."""

    ARTIFACT = (
        "# Title\n\nRun validate_handoff then gate_compliance.\n\n"
        "## Section\n\n---\n\n___\n\n**bold**\n\n| a | b |\n|---|---|\n"
    )

    def _report(self, present):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": present},
        ]}
        return check_overfit(criteria, self.ARTIFACT, "x", 0.34)

    def test_each_syntax_character_is_exempt_alone_and_as_a_run(self):
        for char in sorted(MARKDOWN_SYNTAX_CHARS):
            for anchor in (char, char * 2, char * 3):
                report = self._report(["validate_handoff", anchor])
                self.assertEqual(
                    report["items_exempt_contentless"], 1, repr(anchor)
                )
                self.assertEqual(report["items_classified"], 1, repr(anchor))

    def test_no_syntax_character_can_dilute_the_ratio(self):
        bare = self._report(["validate_handoff", "gate_compliance"])
        self.assertEqual(bare["verdict"], "overfitted")
        for char in sorted(MARKDOWN_SYNTAX_CHARS):
            padded = self._report(
                ["validate_handoff", "gate_compliance"] + [char * 3] * 17
            )
            self.assertEqual(padded["verdict"], "overfitted", repr(char))
            self.assertEqual(
                padded["overfit_ratio"], bare["overfit_ratio"], repr(char)
            )

    def test_underscore_runs_are_the_reported_exploit(self):
        padded = self._report(
            ["validate_handoff", "gate_compliance"] + ["___"] * 17
        )
        self.assertEqual(padded["items_classified"], 2)
        self.assertEqual(padded["items_exempt_contentless"], 17)
        self.assertEqual(padded["overfit_ratio"], 1.0)

    def test_decoration_outside_the_set_scores_only_when_it_is_a_lift(self):
        # The set decides RECITATION, not denominator membership. Decoration
        # reproduced verbatim from the artifact is a lift and stays in the
        # scored set (numerator and denominator alike); decoration that is not
        # in the artifact measures nothing and leaves the set, rather than
        # taking a denominator seat as `outcome` -- which is how "█" and
        # "▓ ▒ ░" diluted the ratio before.
        absent = self._report(["validate_handoff", "▓▒░"])
        self.assertEqual(absent["items_exempt_contentless"], 1)
        self.assertEqual(absent["items_classified"], 1)

        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": ["validate_handoff", "▓▒░"]},
        ]}
        lifted = check_overfit(criteria, self.ARTIFACT + "\n▓▒░\n", "x", 0.34)
        self.assertEqual(lifted["items_exempt_contentless"], 0)
        self.assertEqual(lifted["items_classified"], 2)
        self.assertEqual(lifted["counts"]["vocabulary"], 2)
        self.assertEqual(lifted["counts"]["outcome"], 0)



class TestContentlessItemsNeverSitInTheDenominator(unittest.TestCase):
    """r5-B1. Fourth appearance of the denominator-dilution exploit, and the
    first test written against the INVARIANT instead of a list of shapes.

    History: the rule was closed "by construction" three times and reopened
    three times -- identifier-shaped anchors, then short verbatim prose, then
    underscore runs -- because each fix matched a decoration SHAPE and the
    next shape walked around it. The fourth door was internal whitespace:
    `"| --- |"`, `"- - -"` and `"** **"` matched neither the exemption
    (`BARE_MARKUP`, whitespace-free) nor the lift test (`MARKUP_ANCHOR`,
    whitespace-free), normalised to zero words so MIN_ANCHOR_WORDS never
    applied, and landed in the denominator as `outcome`; three of each turned
    a ratio-1.0 criteria set into `within_threshold`, exit 0. Single-character
    decoration (`"█"`) went through the same gap under MARKUP_ANCHOR's
    two-character floor.

    The invariant asserted here, and the only thing check_overfit.py promises:
    an item that yields no normalised words never occupies a denominator seat
    as `outcome`. It is either recitation (counted in both halves of the
    ratio) or it leaves the scored set. The corpus below is deliberately
    open-ended -- ASCII markdown, box drawing, CJK punctuation, bare
    whitespace -- because the point is that no fifth shape can exist, not that
    these particular ten are handled."""

    ARTIFACT = (
        "# Demo\n\nRun validate_handoff then gate_compliance.\n\n"
        "## Section\n\n---\n\n| a | b |\n| --- | --- |\n\n- - -\n\n** **\n"
    )

    # Every entry normalises to zero words. Nothing else is asserted about
    # them: not their characters, not their length, not their whitespace.
    CONTENTLESS = (
        "| --- |", "- - -", "** **", "|  ---  |", "|---|", "---", "#", "##",
        "___", "█", "▓ ▒ ░", "▓▒░", "→", "···", "* * *", "<!-- -->", "=== ",
        " ", "\t", "。", "•", "()", "[ ]", "{...}", "!!", "??",
    )

    def _report(self, present=(), checks=()):
        criteria = {"skill_name": "x", "test_cases": [
            {"id": "t1", "required_present": list(present),
             "checks": list(checks)},
        ]}
        return check_overfit(criteria, self.ARTIFACT, "x", 0.34)

    def test_no_contentless_anchor_is_ever_classified_outcome(self):
        for anchor in self.CONTENTLESS:
            report = self._report(present=["validate_handoff", anchor])
            outcomes = [i for i in report["flagged"] if i["class"] == "outcome"]
            self.assertEqual(outcomes, [], repr(anchor))
            # One genuine item is classified; the contentless one either
            # joined it as a lift or left the set. Never as `outcome`.
            self.assertEqual(report["counts"]["outcome"], 0, repr(anchor))
            self.assertIn(report["items_classified"], (1, 2), repr(anchor))

    def test_no_contentless_item_can_lower_the_ratio(self):
        bare = self._report(present=["validate_handoff", "gate_compliance"])
        self.assertEqual(bare["verdict"], "overfitted")
        self.assertEqual(bare["overfit_ratio"], 1.0)
        for anchor in self.CONTENTLESS:
            padded = self._report(
                present=["validate_handoff", "gate_compliance"] + [anchor] * 17
            )
            self.assertGreaterEqual(
                padded["overfit_ratio"], bare["overfit_ratio"], repr(anchor)
            )
            self.assertEqual(padded["verdict"], "overfitted", repr(anchor))

    def test_the_reported_whitespace_forms_no_longer_dilute(self):
        # The exact reproduction: one genuine vocabulary anchor scores
        # `overfitted`; three `"| --- |"` and three `"- - -"` used to drop it
        # to 0.1429 and exit 0.
        padded = self._report(present=[
            "gate_compliance", "| --- |", "| --- |", "| --- |",
            "- - -", "- - -", "- - -",
        ])
        self.assertEqual(padded["verdict"], "overfitted")
        self.assertEqual(padded["items_classified"], 1)
        self.assertEqual(padded["items_exempt_contentless"], 6)

    def test_the_invariant_covers_descriptions_and_rubric_bands_too(self):
        # Scoping the earlier fixes to `required_present` was itself part of
        # the leak: a contentless check description or rubric band measures
        # exactly as much as a contentless anchor, which is nothing.
        for text in self.CONTENTLESS:
            report = self._report(checks=[
                {"description": "the report names the failing case"},
                {"description": text},
                {"rubric": {"5": text, "0": text}},
            ])
            self.assertEqual(report["counts"]["outcome"], 1, repr(text))
            self.assertEqual(report["items_classified"], 1, repr(text))
            self.assertEqual(report["items_exempt_contentless"], 2, repr(text))

    def test_a_set_of_only_contentless_items_measures_nothing(self):
        report = self._report(present=list(self.CONTENTLESS))
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIsNone(report["overfit_ratio"])

    def test_every_declared_syntax_character_is_contentless(self):
        # The import-time check in check_overfit.py, asserted end to end: a
        # member of MARKDOWN_SYNTAX_CHARS that normalised to a word would be
        # a scorable item, and the exemption would be lying about it.
        from check_overfit import _carries_content

        for char in sorted(MARKDOWN_SYNTAX_CHARS):
            self.assertFalse(_carries_content(char), repr(char))

    def test_one_word_is_enough_to_be_scored(self):
        # The other side of the invariant: `_carries_content` is a floor at
        # one word, not a licence to drop short items. A one-word anchor is
        # below MIN_ANCHOR_WORDS so it is not a LIFT, but it still occupies a
        # denominator seat, because it does measure something.
        report = self._report(present=["gate_compliance", "ok"])
        self.assertEqual(report["items_classified"], 2)
        self.assertEqual(report["counts"]["outcome"], 1)
        self.assertEqual(report["items_exempt_contentless"], 0)


class TestANonStringSkillNameIsAUsageError(unittest.TestCase):
    """r5-B4. `criteria.get("skill_name") or ""` accepted any truthy value and
    a non-string one reached `re.escape` in `_names_the_artifact`: an uncaught
    TypeError, traceback on stderr, exit 1. Step 6a reads exit 1 as
    `overfitted` and halts the run blaming the criteria set, when the fault is
    a malformed field. Every other malformed-input path in this script exits 2
    and names what is wrong."""

    def _run(self, criteria_json):
        import json
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "criteria.json")
            with open(path, "w") as handle:
                json.dump(criteria_json, handle)
            artifact = os.path.join(tmp, "SKILL.md")
            with open(artifact, "w") as handle:
                handle.write("# Demo\n\nRun the pipeline and report.\n")
            return subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_overfit.py"),
                    path,
                    "--artifact", artifact,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )

    CASES = {"test_cases": [
        {"id": "t1", "checks": [{"description": "the run reports a verdict"}]}
    ]}

    def test_an_integer_skill_name_exits_2_without_a_traceback(self):
        proc = self._run(dict(self.CASES, skill_name=123))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("skill_name", proc.stderr)
        self.assertIn("must be a string", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_list_skill_name_exits_2(self):
        proc = self._run(dict(self.CASES, skill_name=["hone"]))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must be a string", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_missing_or_null_skill_name_is_still_allowed(self):
        for criteria in (self.CASES, dict(self.CASES, skill_name=None)):
            proc = self._run(criteria)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
