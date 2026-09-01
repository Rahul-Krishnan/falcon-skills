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


class TestSizingHonoursAlpha(unittest.TestCase):
    """The sizing block and the comparison block must quote the same floor."""

    def test_min_discordant_tracks_the_caller_alpha(self):
        from check_eval_power import check_sizing, min_discordant_for_alpha

        criteria = {"test_cases": [
            {"id": f"TC-00{i}", "test_profile": "execution"} for i in range(1, 7)
        ]}
        default = check_sizing(criteria, 5, 2)
        loose = check_sizing(criteria, 5, 2, alpha=0.2)
        self.assertEqual(default["min_discordant_for_significance"],
                         min_discordant_for_alpha(0.05))
        self.assertEqual(loose["min_discordant_for_significance"],
                         min_discordant_for_alpha(0.2))
        self.assertNotEqual(default["min_discordant_for_significance"],
                            loose["min_discordant_for_significance"])

    def test_a_stricter_alpha_raises_the_floor_and_the_verdict(self):
        """Reporting the alpha floor is not enforcing it: five cases at
        alpha 0.01 need seven discordant votes, so no arrangement of wins can
        reach significance and `powered` was arithmetically false."""
        from check_eval_power import check_sizing

        criteria = {"test_cases": [
            {"id": f"TC-00{i}", "test_profile": "execution"} for i in range(1, 6)
        ]}
        self.assertEqual(check_sizing(criteria, 5, 2)["verdict"], "powered")

        strict = check_sizing(criteria, 5, 2, alpha=0.01)
        self.assertEqual(strict["verdict"], "underpowered")
        self.assertEqual(strict["effective_floor"], 7)
        self.assertTrue(strict["errors"])

    def test_the_floor_never_drops_below_min_stimuli(self):
        """A loose alpha must not let a caller under --min-stimuli through."""
        from check_eval_power import check_sizing

        criteria = {"test_cases": [
            {"id": f"TC-00{i}", "test_profile": "execution"} for i in range(1, 5)
        ]}
        loose = check_sizing(criteria, 5, 2, alpha=0.2)
        self.assertEqual(loose["effective_floor"], 5)
        self.assertEqual(loose["verdict"], "underpowered")


class TestRoundLoaderPrefersDeterministicScores(unittest.TestCase):
    """Phase 2 decides on the deterministic composite, so the sign test must
    run over that file and not over results.json, which carries a per-test
    `score` only when an LLM judge ran."""

    def _round(self, tmp, name, results, deterministic=None):
        import json
        import os

        directory = os.path.join(tmp, name)
        os.makedirs(directory)
        results_path = os.path.join(directory, "results.json")
        with open(results_path, "w") as handle:
            json.dump(results, handle)
        if deterministic is not None:
            with open(os.path.join(directory, "deterministic_scores.json"), "w") as handle:
                json.dump(deterministic, handle)
        return results_path

    def test_a_results_path_reads_the_sibling_deterministic_file(self):
        import tempfile

        from check_eval_power import _load_round, _scores_by_id

        deterministic = {"per_test": [
            {"test_id": "TC-001", "composite": 0.9},
            {"test_id": "TC-002", "composite": 0.4},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            # results.json holds no per-test score at all: a deterministic-only
            # round. Before the fix this paired zero cases.
            path = self._round(tmp, "r1", {"tests": [{"id": "TC-001"}]}, deterministic)
            self.assertEqual(
                _scores_by_id(_load_round(path)), {"TC-001": 0.9, "TC-002": 0.4}
            )

    def test_the_deterministic_path_itself_is_accepted(self):
        import os
        import tempfile

        from check_eval_power import _load_round, _scores_by_id

        deterministic = {"per_test": [{"test_id": "TC-001", "composite": 0.75}]}
        with tempfile.TemporaryDirectory() as tmp:
            results_path = self._round(tmp, "r1", {}, deterministic)
            sibling = os.path.join(os.path.dirname(results_path),
                                   "deterministic_scores.json")
            self.assertEqual(_scores_by_id(_load_round(sibling)), {"TC-001": 0.75})

    def test_a_judge_only_round_still_falls_back_to_results(self):
        import tempfile

        from check_eval_power import _load_round, _scores_by_id

        with tempfile.TemporaryDirectory() as tmp:
            path = self._round(
                tmp, "r1", {"per_test": [{"test_id": "TC-001", "score": 0.6}]}
            )
            self.assertEqual(_scores_by_id(_load_round(path)), {"TC-001": 0.6})

    def test_the_composite_wins_when_a_record_carries_both(self):
        """Phase 2 decides on the composite, so a record carrying both must
        be compared on the composite, not the judge's score."""
        from check_eval_power import _scores_by_id

        entries = {"results": [{"test_id": "TC-001", "score": 0.4,
                                "composite": 0.9}]}
        self.assertEqual(_scores_by_id(entries), {"TC-001": 0.9})

    def test_a_null_composite_falls_through_to_the_judge_score(self):
        """A present-but-null key escapes a `get` default; dropping the pair
        instead of falling back manufactures an `underpowered` verdict."""
        from check_eval_power import _scores_by_id

        entries = {"results": [{"test_id": "TC-001", "composite": None,
                                "score": 0.8},
                               {"test_id": "TC-002", "score": None,
                                "composite": 0.5}]}
        self.assertEqual(_scores_by_id(entries), {"TC-001": 0.8, "TC-002": 0.5})


class TestSizingLinePrintsTheEnforcedFloor(unittest.TestCase):
    """The human-readable line and the error must not disagree about which
    floor is in force: alpha can raise the floor above --min-stimuli."""

    def test_the_printed_floor_is_the_effective_one(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile

        from check_eval_power import min_discordant_for_alpha

        criteria = {"test_cases": [
            {"id": f"tc{i}", "test_profile": "p1"} for i in range(6)
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "criteria.json")
            with open(path, "w") as handle:
                json.dump(criteria, handle)
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                    path,
                    "--alpha",
                    "0.001",
                ],
                capture_output=True,
                text=True,
            )
        floor = min_discordant_for_alpha(0.001)
        self.assertGreater(floor, 5)
        self.assertIn(f"floor {floor},", proc.stdout)
        self.assertNotIn("floor 5,", proc.stdout)
        self.assertIn(f"floor is {floor}", proc.stdout)


if __name__ == "__main__":
    unittest.main()
