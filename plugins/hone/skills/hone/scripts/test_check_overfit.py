#!/usr/bin/env python3
"""Tests for check_overfit.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from check_overfit import check_overfit, classify_item  # noqa: E402

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

    def test_lifted_markup_anchor_is_vocabulary(self):
        self.assertEqual(self._report(["##"])["counts"]["vocabulary"], 1)

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
