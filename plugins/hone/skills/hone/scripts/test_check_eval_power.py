#!/usr/bin/env python3
"""Tests for check_eval_power.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from check_eval_power import (  # noqa: E402
    check_compare,
    check_sizing,
    min_discordant_for_alpha,
    sign_test_p,
)


class TestSignTest(unittest.TestCase):
    def test_matches_published_thresholds(self):
        # The dotnet/skills create-skill-test table this implements:
        # <=4 discordant never passes, 5-7 need a clean sweep, 8 tolerates one loss.
        self.assertGreater(sign_test_p(4, 4), 0.05)
        self.assertAlmostEqual(sign_test_p(5, 5), 0.03125, places=5)
        self.assertAlmostEqual(sign_test_p(7, 8), 0.03516, places=5)
        self.assertGreater(sign_test_p(6, 7), 0.05)

    def test_floor_is_five(self):
        self.assertEqual(min_discordant_for_alpha(0.05), 5)

    def test_no_discordant_is_not_significant(self):
        self.assertEqual(sign_test_p(0, 0), 1.0)


class TestSizing(unittest.TestCase):
    def _criteria(self, n, profile="alpha"):
        return {
            "test_cases": [
                {"id": f"tc{i:02d}", "test_profile": profile} for i in range(n)
            ]
        }

    def test_below_floor_is_underpowered(self):
        report = check_sizing(self._criteria(3), 5, 2)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertTrue(report["errors"])

    def test_at_floor_is_powered(self):
        report = check_sizing(self._criteria(5), 5, 1)
        self.assertEqual(report["verdict"], "powered")

    def test_duplicate_ids_are_an_error(self):
        criteria = {"test_cases": [{"id": "tc01"}] * 6}
        report = check_sizing(criteria, 5, 1)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertTrue(any("duplicate" in e for e in report["errors"]))

    def test_single_profile_warns_but_does_not_block(self):
        report = check_sizing(self._criteria(6), 5, 2)
        self.assertEqual(report["verdict"], "powered")
        self.assertTrue(any("profile" in w for w in report["warnings"]))


class TestCompare(unittest.TestCase):
    def _results(self, scores):
        return {"test_results": [{"test_id": k, "score": v} for k, v in scores.items()]}

    def test_clean_sweep_of_five_is_significant(self):
        before = self._results({f"tc{i}": 0.5 for i in range(5)})
        after = self._results({f"tc{i}": 0.9 for i in range(5)})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["wins"], 5)

    def test_ties_hold_the_discordant_count_down(self):
        # Two moved, six tied: the round showed nothing.
        before = self._results({f"tc{i}": 0.5 for i in range(8)})
        after_scores = {f"tc{i}": 0.5 for i in range(8)}
        after_scores["tc0"] = 0.9
        after_scores["tc1"] = 0.9
        report = check_compare(before, self._results(after_scores), 0.05)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertEqual(report["ties"], 6)
        self.assertEqual(report["discordant"], 2)

    def test_mixed_movement_is_inconclusive(self):
        before = self._results({f"tc{i}": 0.5 for i in range(6)})
        after_scores = {f"tc{i}": 0.9 for i in range(3)}
        after_scores.update({f"tc{i}": 0.1 for i in range(3, 6)})
        report = check_compare(before, self._results(after_scores), 0.05)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_regression_is_detected(self):
        before = self._results({f"tc{i}": 0.9 for i in range(6)})
        after = self._results({f"tc{i}": 0.4 for i in range(6)})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["verdict"], "regressed")

    def test_unpaired_cases_are_reported(self):
        before = self._results({"tc0": 0.5, "tc1": 0.5})
        after = self._results({"tc1": 0.9, "tc2": 0.9})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["unpaired_before"], ["tc0"])
        self.assertEqual(report["unpaired_after"], ["tc2"])

    def test_per_test_mapping_shape_is_accepted(self):
        before = {"per_test": {"tc0": 0.5, "tc1": 0.5}}
        after = {"per_test": {"tc0": {"score": 0.9}, "tc1": {"score": 0.9}}}
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["wins"], 2)

    def test_scorer_per_test_list_shape_is_paired(self):
        # score_from_results emits per_test as a LIST of records keyed by
        # test_id/composite, and that payload is what --before/--after are
        # pointed at. Treating per_test as a mapping only sent it down the
        # raw-results branch, which paired nothing.
        def scored(scores):
            return {
                "composite_score": 0.5,
                "per_test": [
                    {"test_id": tid, "status": "scored", "composite": value}
                    for tid, value in scores.items()
                ],
            }

        before = scored({f"tc{i}": 0.4 for i in range(6)})
        after = scored({f"tc{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["paired_cases"], 6)
        self.assertEqual(report["wins"], 6)

    def test_inconclusive_records_are_dropped_not_crashed(self):
        before = {"per_test": [{"test_id": "tc0", "composite": None}]}
        after = {"per_test": [{"test_id": "tc0", "composite": None}]}
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["paired_cases"], 0)

    def test_mapping_of_records_without_score_key_does_not_raise(self):
        # hone names the field "composite"; the old mapping branch called
        # float() on the record itself and raised TypeError.
        before = {"per_test": {"tc0": {"composite": 0.4}}}
        after = {"per_test": {"tc0": {"composite": 0.9}}}
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["paired_cases"], 1)
        self.assertEqual(report["wins"], 1)


if __name__ == "__main__":
    unittest.main()


class TestCriteriaRootShape(unittest.TestCase):
    """A non-object criteria root is a usage error, not an AttributeError."""

    def _run(self, contents):
        import subprocess
        import sys
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "criteria.json")
            with open(path, "w") as handle:
                handle.write(contents)
            return subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                    path,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )

    def test_a_list_rooted_criteria_file_exits_2(self):
        proc = self._run('[{"id": "TC-001"}]')
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must be a JSON object", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_scalar_rooted_criteria_file_exits_2(self):
        proc = self._run('"just a string"')
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
