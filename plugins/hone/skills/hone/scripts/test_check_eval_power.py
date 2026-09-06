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


def _results(scores):
    """One round's scores in the raw results.json shape."""
    return {"test_results": [{"test_id": k, "score": v} for k, v in scores.items()]}


# Fixtures comparing the same scorer record its fingerprint explicitly.
# Missing fingerprints mean unknown scoring logic; identity tests set their own.
SAME_SCORER = "ast1:0000000000000000"


def _with_scorer(payload, fingerprint=SAME_SCORER):
    """A deterministic payload stamped with the scorer that produced it."""
    if fingerprint is None:
        return payload
    metadata = dict(payload.get("metadata") or {})
    metadata["scorer_fingerprint"] = fingerprint
    return {**payload, "metadata": metadata}


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
        # The floor is advisory: reported, with the floor it enforced and the
        # reason, but not an error and not a blocking finding.
        self.assertTrue(report["advisories"])
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["blocking"])
        self.assertEqual(report["effective_floor"], 5)

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
    def test_clean_sweep_of_five_is_significant(self):
        before = _results({f"tc{i}": 0.5 for i in range(5)})
        after = _results({f"tc{i}": 0.9 for i in range(5)})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["wins"], 5)

    def test_ties_hold_the_discordant_count_down(self):
        # Two moved, six tied: the round showed nothing.
        before = _results({f"tc{i}": 0.5 for i in range(8)})
        after_scores = {f"tc{i}": 0.5 for i in range(8)}
        after_scores["tc0"] = 0.9
        after_scores["tc1"] = 0.9
        report = check_compare(before, _results(after_scores), 0.05)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertEqual(report["ties"], 6)
        self.assertEqual(report["discordant"], 2)

    def test_mixed_movement_is_inconclusive(self):
        before = _results({f"tc{i}": 0.5 for i in range(6)})
        after_scores = {f"tc{i}": 0.9 for i in range(3)}
        after_scores.update({f"tc{i}": 0.1 for i in range(3, 6)})
        report = check_compare(before, _results(after_scores), 0.05)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_regression_is_detected(self):
        before = _results({f"tc{i}": 0.9 for i in range(6)})
        after = _results({f"tc{i}": 0.4 for i in range(6)})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["verdict"], "regressed")

    def test_unpaired_cases_are_reported(self):
        before = _results({"tc0": 0.5, "tc1": 0.5})
        after = _results({"tc1": 0.9, "tc2": 0.9})
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["unpaired_before"], ["tc0"])
        self.assertEqual(report["unpaired_after"], ["tc2"])

    def test_per_test_mapping_shape_is_accepted(self):
        before = {"per_test": {"tc0": 0.5, "tc1": 0.5}}
        after = {"per_test": {"tc0": {"score": 0.9}, "tc1": {"score": 0.9}}}
        report = check_compare(before, after, 0.05)
        self.assertEqual(report["wins"], 2)

    def test_scorer_per_test_list_shape_is_paired(self):
        # Accept the scorer's list of test_id/composite records.
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
        """At alpha 0.01, five cases cannot meet the seven-discordant-vote floor."""
        from check_eval_power import check_sizing

        criteria = {"test_cases": [
            {"id": f"TC-00{i}", "test_profile": "execution"} for i in range(1, 6)
        ]}
        self.assertEqual(check_sizing(criteria, 5, 2)["verdict"], "powered")

        strict = check_sizing(criteria, 5, 2, alpha=0.01)
        self.assertEqual(strict["verdict"], "underpowered")
        self.assertEqual(strict["effective_floor"], 7)
        self.assertTrue(strict["advisories"])
        self.assertFalse(strict["blocking"])

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
    """Compare deterministic composites, the scores Phase 2 acts on."""

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
                json.dump(_with_scorer(deterministic), handle)
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
                _scores_by_id(_load_round(path)[0]), {"TC-001": 0.9, "TC-002": 0.4}
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
            self.assertEqual(_scores_by_id(_load_round(sibling)[0]), {"TC-001": 0.75})

    def test_a_judge_only_round_still_falls_back_to_results(self):
        import tempfile

        from check_eval_power import _load_round, _scores_by_id

        with tempfile.TemporaryDirectory() as tmp:
            path = self._round(
                tmp, "r1", {"per_test": [{"test_id": "TC-001", "score": 0.6}]}
            )
            round_payload, source = _load_round(path)
            self.assertEqual(_scores_by_id(round_payload), {"TC-001": 0.6})
            self.assertEqual(source, "results")

    def test_an_all_null_round_is_still_the_deterministic_scorer(self):
        """All-null composites mean lost evidence, not a switch to judge scoring."""
        import tempfile

        from check_eval_power import _load_round, _scores_by_id

        deterministic = {"per_test": [
            {"test_id": "TC-001", "composite": None, "status": "inconclusive"},
            {"test_id": "TC-002", "composite": None, "status": "inconclusive"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._round(
                tmp, "r1",
                {"per_test": [{"test_id": "TC-001", "score": 0.6}]},
                deterministic,
            )
            round_payload, source = _load_round(path)
            self.assertEqual(source, "deterministic")
            # And it does not fall through to the judge: that would swap
            # scorers inside one side of the comparison.
            self.assertEqual(_scores_by_id(round_payload), {})

    def test_an_all_null_round_reports_the_pairing_failure_not_a_scorer_swap(self):
        import tempfile

        from check_eval_power import _load_round, check_compare

        good = {"per_test": [{"test_id": f"TC-{i}", "composite": 0.8}
                             for i in range(6)]}
        null = {"per_test": [{"test_id": f"TC-{i}", "composite": None}
                             for i in range(6)]}
        with tempfile.TemporaryDirectory() as tmp:
            before_path = self._round(tmp, "r1", {}, good)
            after_path = self._round(tmp, "r2", {}, null)
            before, before_source = _load_round(before_path)
            after, after_source = _load_round(after_path)
            report = check_compare(before, after, 0.05, before_source, after_source)
            self.assertEqual(report["verdict"], "not_measurable")
            said = " ".join(report["errors"])
            # Every case collapsed, and the diagnosis says so by name rather
            # than reporting a bare pairing failure or a scorer swap.
            self.assertIn("came back inconclusive in --after", said)
            self.assertEqual(report["inconclusive_after"], [f"TC-{i}" for i in range(6)])
            self.assertNotIn("scorer", said)


class TestRoundPathsMustBeFiles(unittest.TestCase):
    """Reject directory paths before sibling lookup can read the wrong round."""

    def test_a_directory_is_a_usage_error(self):
        import tempfile

        from check_eval_power import _require_path

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                _require_path(tmp)
            self.assertEqual(caught.exception.code, 2)

    def test_a_missing_path_is_still_a_usage_error(self):
        from check_eval_power import _require_path

        with self.assertRaises(SystemExit) as caught:
            _require_path("/nonexistent/round/deterministic_scores.json")
        self.assertEqual(caught.exception.code, 2)

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
    """Print the alpha-adjusted floor actually enforced."""

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


class TestSizingCountsOnlyScorableCases(unittest.TestCase):
    """Size only cases eligible for deterministic pairing."""

    def _criteria(self, scorable, unscorable):
        cases = [
            {"id": f"ex{i:02d}", "test_profile": "execution"} for i in range(scorable)
        ]
        cases += [
            {"id": f"ke{i:02d}", "test_profile": "knowledge_extraction"}
            for i in range(unscorable)
        ]
        return {"test_cases": cases}

    def test_unscorable_cases_do_not_clear_the_floor(self):
        # Six cases, three unscorable: sizing must not certify all six as pairable.
        report = check_sizing(self._criteria(3, 3), 5, 1)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertEqual(report["distinct_cases"], 6)
        self.assertEqual(report["scorable_cases"], 3)

    def test_adding_more_unscorable_cases_cannot_clear_the_floor(self):
        padded = check_sizing(self._criteria(3, 30), 5, 1)
        self.assertEqual(padded["verdict"], "underpowered")
        self.assertEqual(padded["scorable_cases"], 3)

    def test_the_excluded_cases_are_named(self):
        report = check_sizing(self._criteria(5, 2), 5, 1)
        self.assertEqual(report["verdict"], "powered")
        self.assertEqual(report["excluded_cases"], ["ke00", "ke01"])
        self.assertTrue(any("cannot clear the floor" in w for w in report["warnings"]))

    def test_profile_diversity_ignores_unscorable_profiles(self):
        """Two profiles of which only one can be scored is one profile of
        evidence, so the diversity warning has to fire."""
        report = check_sizing(self._criteria(5, 2), 5, 2)
        self.assertEqual(report["profiles"], ["execution"])
        self.assertTrue(any("profile(s)" in w for w in report["warnings"]))

    def test_a_case_without_a_profile_still_counts(self):
        """Nothing in the criteria file says an unset profile is unscorable;
        counting it out would fail suites the comparison can rule on."""
        report = check_sizing({"test_cases": [{"id": f"tc{i}"} for i in range(5)]}, 5, 1)
        self.assertEqual(report["scorable_cases"], 5)
        self.assertEqual(report["verdict"], "powered")


class TestZeroPairingIsNotATieHeavyRound(unittest.TestCase):
    """Report input mismatches separately from insufficient discordant votes."""

    def test_no_shared_ids_is_not_measurable(self):
        report = check_compare(
            _results({"a1": 0.5}), _results({"b1": 0.9}), 0.05
        )
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["paired_cases"], 0)
        self.assertTrue(report["errors"])
        self.assertFalse(report["warnings"])

    def test_the_tie_explanation_is_not_offered_for_zero_pairs(self):
        report = check_compare(_results({}), _results({}), 0.05)
        self.assertEqual(report["verdict"], "not_measurable")
        said = " ".join(report["errors"] + report["warnings"])
        self.assertNotIn("ties hold", said)

    def test_a_genuinely_tie_heavy_round_still_reads_underpowered(self):
        before = _results({f"tc{i}": 0.5 for i in range(8)})
        report = check_compare(before, before, 0.05)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertTrue(any("ties hold" in w for w in report["warnings"]))


class TestMismatchedScorersAreNotCompared(unittest.TestCase):
    """The deterministic composite and the judge score are both 0-1 and
    neither is a rescaling of the other."""

    def test_a_scorer_swap_is_not_measurable(self):
        before = _results({f"tc{i}": 0.4 for i in range(6)})
        after = _results({f"tc{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05, "results", "deterministic")
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("scorer" in e for e in report["errors"]))

    def test_matching_scorers_still_rule(self):
        before = _results({f"tc{i}": 0.4 for i in range(6)})
        after = _results({f"tc{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["verdict"], "improved")

    def test_a_missing_round_path_exits_2(self):
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = os.path.join(tmp, "criteria.json")
            with open(criteria, "w") as handle:
                handle.write('{"test_cases": []}')
            # The sibling deterministic file exists; the named path does not.
            with open(os.path.join(tmp, "deterministic_scores.json"), "w") as handle:
                handle.write('{"per_test": [{"test_id": "tc0", "composite": 0.5}]}')
            typo = os.path.join(tmp, "reslts.json")
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                    criteria,
                    "--before", typo,
                    "--after", typo,
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("file not found", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TestAlphaIsPerDirection(unittest.TestCase):
    def test_the_combined_rate_is_reported(self):
        report = check_compare({"per_test": {}}, {"per_test": {}}, 0.05)
        self.assertEqual(report["alpha"], 0.05)
        self.assertEqual(report["two_sided_alpha"], 0.1)


class TestSizingOverridesAreStated(unittest.TestCase):
    def test_a_suppressed_regression_is_named_in_the_warnings(self):
        from check_eval_power import _combined_verdict

        sizing = {"verdict": "underpowered"}
        comparison = {"verdict": "regressed", "warnings": []}
        self.assertEqual(_combined_verdict(sizing, comparison), "underpowered")
        self.assertTrue(any("suppressed" in w for w in comparison["warnings"]))

    def test_a_powered_suite_passes_the_comparison_verdict_through(self):
        from check_eval_power import _combined_verdict

        comparison = {"verdict": "regressed", "warnings": []}
        self.assertEqual(
            _combined_verdict({"verdict": "powered"}, comparison), "regressed"
        )
        self.assertFalse(comparison["warnings"])

    def test_an_already_underpowered_comparison_gains_no_warning(self):
        from check_eval_power import _combined_verdict

        comparison = {"verdict": "underpowered", "warnings": []}
        _combined_verdict({"verdict": "underpowered"}, comparison)
        self.assertFalse(comparison["warnings"])

    def test_a_hidden_not_measurable_says_the_remedy_differs(self):
        """A hidden input problem needs repair, not more test cases."""
        from check_eval_power import _combined_verdict

        comparison = {"verdict": "not_measurable", "warnings": []}
        self.assertEqual(
            _combined_verdict({"verdict": "underpowered"}, comparison),
            "underpowered",
        )
        self.assertTrue(
            any("not_measurable" in w for w in comparison["warnings"])
        )


class TestArtifactTypeScopesTheProfileExclusion(unittest.TestCase):
    """Hooks and scripts can score knowledge_extraction and must count those cases."""

    CRITERIA = {"test_cases": [
        {"id": f"TC-{i}", "test_profile": "knowledge_extraction"}
        for i in range(6)
    ]}

    def test_a_skill_suite_still_excludes_the_profile(self):
        report = check_sizing(self.CRITERIA, 5, 2, 0.05, "skill")
        self.assertEqual(report["scorable_cases"], 0)
        self.assertEqual(report["verdict"], "underpowered")

    def test_an_unset_artifact_type_keeps_the_conservative_reading(self):
        self.assertEqual(check_sizing(self.CRITERIA, 5, 2, 0.05)["scorable_cases"], 0)

    def test_a_hook_suite_counts_every_case(self):
        report = check_sizing(self.CRITERIA, 5, 1, 0.05, "hook")
        self.assertEqual(report["scorable_cases"], 6)
        self.assertEqual(report["excluded_cases"], [])
        self.assertEqual(report["verdict"], "powered")
        self.assertFalse(any("never pair" in w for w in report["warnings"]))

    def test_a_script_suite_counts_every_case(self):
        self.assertEqual(
            check_sizing(self.CRITERIA, 5, 1, 0.05, "script")["scorable_cases"], 6
        )


def _deterministic(scores, inconclusive=()):
    """One round in the shape `_load_round` builds from deterministic_scores.json."""
    return {
        "per_test": [{"test_id": k, "composite": v} for k, v in scores.items()],
        "inconclusive": sorted(inconclusive),
    }


class TestInconclusiveCasesAreNotSilentlyDropped(unittest.TestCase):
    """Cases losing evidence must block comparison over the surviving scores."""

    def _rounds(self, tmp):
        import json
        import os

        before = {"per_test": [
            {"test_id": f"t{i}", "composite": 0.7, "status": "pass"} for i in range(8)
        ]}
        after = {"per_test": [
            {"test_id": f"t{i}", "composite": 0.8, "status": "pass"} for i in range(5)
        ] + [
            {"test_id": f"t{i}", "composite": None, "status": "inconclusive"}
            for i in range(5, 8)
        ]}
        paths = []
        for name, payload in (("r1", before), ("r2", after)):
            os.makedirs(os.path.join(tmp, name))
            path = os.path.join(tmp, name, "deterministic_scores.json")
            with open(path, "w") as handle:
                json.dump(_with_scorer(payload), handle)
            paths.append(path)
        return paths

    def test_a_partial_collapse_is_not_an_improvement(self):
        import tempfile

        from check_eval_power import _load_round

        with tempfile.TemporaryDirectory() as tmp:
            before_path, after_path = self._rounds(tmp)
            before, before_source = _load_round(before_path)
            after, after_source = _load_round(after_path)
        report = check_compare(before, after, 0.05, before_source, after_source)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["inconclusive_after"], ["t5", "t6", "t7"])
        self.assertEqual(report["paired_cases"], 5)
        said = " ".join(report["errors"])
        self.assertIn("t5", said)
        self.assertIn("inconclusive in --after", said)

    def test_the_collapsed_ids_are_not_reported_as_missing(self):
        before = _deterministic({f"t{i}": 0.7 for i in range(8)})
        after = _deterministic({f"t{i}": 0.8 for i in range(5)}, inconclusive=["t5", "t6", "t7"])
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["unpaired_before"], [])
        self.assertEqual(report["inconclusive_after"], ["t5", "t6", "t7"])

    def test_the_loader_carries_the_inconclusive_ids(self):
        import tempfile

        from check_eval_power import _load_round

        with tempfile.TemporaryDirectory() as tmp:
            _, after_path = self._rounds(tmp)
            payload, _ = _load_round(after_path)
        self.assertEqual(payload["inconclusive"], ["t5", "t6", "t7"])

    def test_a_recovered_case_is_a_warning_not_a_verdict(self):
        """The other direction has no baseline to rule from, so the case is
        simply not paired; it is named so an unstable suite is visible."""
        before = _deterministic({f"t{i}": 0.7 for i in range(5)}, inconclusive=["t5"])
        after = _deterministic({f"t{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["unpaired_after"], [])
        self.assertTrue(any("t5" in w and "inconclusive in --before" in w
                            for w in report["warnings"]))

    def test_hand_built_payloads_still_compare(self):
        report = check_compare(
            _results({f"t{i}": 0.4 for i in range(6)}),
            _results({f"t{i}": 0.9 for i in range(6)}),
            0.05,
        )
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["inconclusive_after"], [])


class TestTieClassificationSurvivesFloatRepresentation(unittest.TestCase):
    """Equal nominal deltas must classify alike despite floating-point noise."""

    def _shift(self, start, end, n=8):
        before = _deterministic({f"t{i}": start for i in range(n)})
        after = _deterministic({f"t{i}": end for i in range(n)})
        return check_compare(before, after, 0.05, "deterministic", "deterministic")

    def test_the_same_nominal_movement_classifies_the_same_way(self):
        low = self._shift(0.50, 0.55)
        high = self._shift(0.80, 0.85)
        self.assertEqual(
            (low["wins"], low["ties"], low["verdict"]),
            (high["wins"], high["ties"], high["verdict"]),
        )

    def test_a_movement_at_the_epsilon_is_a_tie_on_both_sides_of_the_float(self):
        for start, end in ((0.50, 0.55), (0.80, 0.85), (0.10, 0.15), (0.30, 0.35)):
            report = self._shift(start, end)
            self.assertEqual(report["ties"], 8, (start, end))
            self.assertEqual(report["verdict"], "underpowered", (start, end))

    def test_the_recorded_delta_is_the_classified_one(self):
        report = self._shift(0.80, 0.85)
        self.assertEqual({m["delta"] for m in report["movements"]}, {0.05})

    def test_a_movement_just_over_the_epsilon_is_still_a_win(self):
        report = self._shift(0.80, 0.86)
        self.assertEqual(report["wins"], 8)
        self.assertEqual(report["verdict"], "improved")


class TestTwoJudgeRoundsAreNotCompared(unittest.TestCase):
    """Matching judge sources still cannot justify action on deterministic scores."""

    def _judge_round(self, tmp, name, score, n=6):
        import json
        import os

        os.makedirs(os.path.join(tmp, name))
        path = os.path.join(tmp, name, "results.json")
        with open(path, "w") as handle:
            json.dump({"per_test": [{"test_id": f"t{i}", "score": score}
                                    for i in range(n)]}, handle)
        return path

    def test_matching_judge_sources_are_not_measurable(self):
        before = _results({f"t{i}": 0.2 for i in range(6)})
        after = _results({f"t{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05, "results", "results")
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("deterministic_scores.json" in e for e in report["errors"]))

    def test_the_cli_exits_1_and_names_the_scorer(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = os.path.join(tmp, "criteria.json")
            with open(criteria, "w") as handle:
                json.dump({"test_cases": [{"id": f"t{i}", "test_profile": "execution"}
                                          for i in range(6)]}, handle)
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                    criteria,
                    "--before", self._judge_round(tmp, "r1", 0.2),
                    "--after", self._judge_round(tmp, "r2", 0.9),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("VERDICT: not_measurable", proc.stdout)
        self.assertIn("scorers results/results", proc.stdout)

    def test_deterministic_rounds_still_rule(self):
        before = _deterministic({f"t{i}": 0.2 for i in range(6)})
        after = _deterministic({f"t{i}": 0.9 for i in range(6)})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["verdict"], "improved")


class TestProfileMirrorsTheScorer(unittest.TestCase):
    """Only error_handling maps from category to profile; other categories are not diversity."""

    CATEGORIES = ("invocation", "execution", "edge_case", "task_completion", "error_handling")

    def test_categories_are_not_profiles(self):
        criteria = {"test_cases": [
            {"id": f"tc{i}", "category": category}
            for i, category in enumerate(self.CATEGORIES)
        ]}
        report = check_sizing(criteria, 5, 2)
        self.assertEqual(report["profiles"], ["error_handling", "execution"])

    def test_the_diversity_warning_does_not_depend_on_filling_in_test_profile(self):
        bare = {"test_cases": [
            {"id": f"tc{i}", "category": category}
            for i, category in enumerate(("invocation", "execution", "edge_case",
                                          "task_completion", "invocation"))
        ]}
        explicit = {"test_cases": [
            {**case, "test_profile": "execution"} for case in bare["test_cases"]
        ]}
        bare_report = check_sizing(bare, 5, 2)
        explicit_report = check_sizing(explicit, 5, 2)
        self.assertEqual(bare_report["profiles"], ["execution"])
        self.assertEqual(bare_report["profiles"], explicit_report["profiles"])
        self.assertTrue(any("profile(s)" in w for w in bare_report["warnings"]))
        self.assertTrue(any("profile(s)" in w for w in explicit_report["warnings"]))

    def test_an_unknown_test_profile_is_the_default(self):
        """The scorer does not honour a profile it does not know, so neither
        does the floor's diversity count."""
        report = check_sizing({"test_cases": [
            {"id": f"tc{i}", "test_profile": "made_up"} for i in range(5)
        ]}, 5, 2)
        self.assertEqual(report["profiles"], ["execution"])

    def test_the_error_handling_category_still_resolves(self):
        report = check_sizing({"test_cases": [
            {"id": f"tc{i}", "category": "error-handling"} for i in range(5)
        ]}, 5, 1)
        self.assertEqual(report["profiles"], ["error_handling"])


def _run_cli(*argv):
    import os
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "check_eval_power.py"), *argv],
        capture_output=True,
        text=True,
    )


def _write_round(tmp, name, per_test, artifact_type=None, raw=None,
                 scorer=SAME_SCORER):
    """A round directory holding a deterministic_scores.json; returns its path.

    `scorer` is the `metadata.scorer_fingerprint` the file records; pass None
    for a round written by a scorer old enough not to record one.
    """
    import json
    import os

    os.makedirs(os.path.join(tmp, name))
    path = os.path.join(tmp, name, "deterministic_scores.json")
    with open(path, "w") as handle:
        if raw is not None:
            handle.write(raw)
        else:
            payload = {"per_test": per_test}
            if artifact_type:
                payload["metadata"] = {"artifact_type": artifact_type}
            json.dump(_with_scorer(payload, scorer), handle)
    return path


def _write_criteria(tmp, cases):
    import json
    import os

    path = os.path.join(tmp, "criteria.json")
    with open(path, "w") as handle:
        json.dump({"test_cases": cases}, handle)
    return path


class TestCompareModeReadsTheRecordedArtifactType(unittest.TestCase):
    """Size using recorded scorer metadata when the caller omits artifact type."""

    CASES = [{"id": f"t{i}", "test_profile": "execution"} for i in range(4)] + [
        {"id": "t4", "test_profile": "knowledge_extraction"}
    ]

    def _rounds(self, tmp, before_type="hook", after_type="hook"):
        before = [{"test_id": f"t{i}", "composite": 0.5} for i in range(5)]
        after = [{"test_id": f"t{i}", "composite": 0.9} for i in range(5)]
        return (
            _write_round(tmp, "r1", before, before_type),
            _write_round(tmp, "r2", after, after_type),
        )

    def test_the_recorded_type_sizes_the_suite_without_the_flag(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, self.CASES)
            before, after = self._rounds(tmp)
            proc = _run_cli(criteria, "--before", before, "--after", after, "--json")
        report = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["mode"], "compare")
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["sizing"]["artifact_type"], "hook")

    def test_a_disagreeing_flag_is_warned_and_overridden(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, self.CASES)
            before, after = self._rounds(tmp)
            proc = _run_cli(criteria, "--artifact-type", "skill",
                            "--before", before, "--after", after, "--json")
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "improved")
        self.assertTrue(any("disagrees" in w for w in report["sizing"]["warnings"]))

    def test_rounds_recording_different_types_fall_back_to_the_flag(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, self.CASES)
            before, after = self._rounds(tmp, "hook", "skill")
            proc = _run_cli(criteria, "--before", before, "--after", after, "--json")
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertTrue(any("different artifact types" in w for w in report["sizing"]["warnings"]))

    def test_rounds_without_metadata_keep_the_flag(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, self.CASES)
            before, after = self._rounds(tmp, None, None)
            proc = _run_cli(criteria, "--artifact-type", "hook",
                            "--before", before, "--after", after, "--json")
        self.assertEqual(json.loads(proc.stdout)["verdict"], "improved")


class TestACorruptDeterministicFileIsAUsageError(unittest.TestCase):
    """Report corrupt deterministic JSON before tolerant loaders hide it as empty."""

    def test_invalid_json_exits_2_and_names_the_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, [{"id": f"t{i}"} for i in range(5)])
            before = _write_round(tmp, "r1", [{"test_id": "t0", "composite": 0.5}])
            after = _write_round(tmp, "r2", None, raw="{not json")
            proc = _run_cli(criteria, "--before", before, "--after", after)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not valid JSON", proc.stderr)
        self.assertIn("deterministic_scores.json", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_list_rooted_deterministic_file_exits_2(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, [{"id": f"t{i}"} for i in range(5)])
            before = _write_round(tmp, "r1", [{"test_id": "t0", "composite": 0.5}])
            after = _write_round(tmp, "r2", None, raw="[]")
            proc = _run_cli(criteria, "--before", before, "--after", after)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("root must be a JSON object", proc.stderr)


class TestAMissingBaselineIsNamedAsSuch(unittest.TestCase):
    """An inconclusive before-round has no baseline, even if test ids match."""

    def test_all_recovered_cases_name_the_missing_baseline(self):
        before = _deterministic({}, inconclusive=[f"t{i}" for i in range(5)])
        after = _deterministic({f"t{i}": 0.9 for i in range(5)})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIn("no baseline", report["errors"][0])
        self.assertNotIn("no test id is present in both rounds", report["errors"][0])
        # Named once, in the error, not again as a warning.
        self.assertFalse(any("no baseline" in w for w in report["warnings"]))

    def test_a_genuine_id_mismatch_still_reads_as_one(self):
        # Nothing recovered: the before round scored ids the after round has
        # never heard of, which really is a pairing failure.
        before = _deterministic({"t0": 0.4})
        after = _deterministic({"other": 0.9})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertIn("no test id is present in both rounds", report["errors"][0])

    def test_one_new_after_case_does_not_suppress_the_diagnosis(self):
        # New after-only ids must not hide an inconclusive baseline for matching ids.
        before = _deterministic({}, inconclusive=[f"t{i}" for i in range(5)])
        after = _deterministic({f"t{i}": 0.9 for i in range(5)} | {"new1": 0.9})
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["verdict"], "not_measurable")
        error = report["errors"][0]
        self.assertIn("no baseline", error)
        self.assertNotIn("no test id is present in both rounds", error)
        self.assertIn("new1", error)
        self.assertIn("new in --after", error)


class TestIdsAreComparedAsStrings(unittest.TestCase):
    """`_scores_by_id` keys on `str(test_id)`; sizing dropped an integer 0 as
    falsy and raised TypeError sorting mixed int/str ids."""

    def test_an_integer_zero_id_is_a_case(self):
        report = check_sizing({"test_cases": [{"id": i} for i in range(5)]}, 5, 1)
        self.assertEqual(report["distinct_cases"], 5)
        self.assertEqual(report["verdict"], "powered")

    def test_mixed_id_types_do_not_raise(self):
        report = check_sizing({"test_cases": [{"id": "a"}, {"id": 1}]}, 5, 1)
        self.assertEqual(report["distinct_cases"], 2)

    def test_a_numeric_and_string_spelling_of_one_id_are_duplicates(self):
        report = check_sizing({"test_cases": [{"id": 1}, {"id": "1"}]}, 5, 1)
        self.assertTrue(any("duplicate" in e for e in report["errors"]))


class TestProfileDiversityIsNotAskedOfHooksAndScripts(unittest.TestCase):
    """Profile changes do not change the dimensions measured for hooks and scripts."""

    CASES = [{"id": f"t{i}"} for i in range(5)]

    def test_a_hook_suite_does_not_warn_at_the_default_floor(self):
        report = check_sizing({"test_cases": self.CASES}, 5, 2, 0.05, "hook")
        self.assertEqual(report["warnings"], [])

    def test_a_skill_suite_still_warns(self):
        report = check_sizing({"test_cases": self.CASES}, 5, 2, 0.05, "skill")
        self.assertTrue(any("profile" in w for w in report["warnings"]))


class TestResultsFallbackSharesHoneCommonsShape(unittest.TestCase):
    """The raw results.json fallback reads through hone_common, so a
    skill-creator-shaped file (`test_results` + `final_score`) is seen."""

    def test_final_score_alias_is_read(self):
        from check_eval_power import _scores_by_id

        scores = _scores_by_id({"test_results": [{"test_id": "a", "final_score": 0.5}]})
        self.assertEqual(scores, {"a": 0.5})

    def test_results_key_takes_precedence_over_test_results(self):
        from check_eval_power import _scores_by_id

        scores = _scores_by_id({
            "results": [{"test_id": "a", "score": 0.5}],
            "test_results": [{"test_id": "b", "score": 0.1}],
        })
        self.assertEqual(scores, {"a": 0.5})



# V2 phase-document string checks retired with the v3 outcome workflow.
# Runtime compatibility tests for the legacy comparison API remain below.


class TestFalsyIdsSurviveTheResultsFallback(unittest.TestCase):
    """Preserve numeric id 0 in both sizing and raw-result pairing."""

    def test_an_integer_zero_id_pairs(self):
        before = {"test_results": [{"test_id": 0, "score": 0.4},
                                   {"test_id": 1, "score": 0.4}]}
        after = {"test_results": [{"test_id": 0, "score": 0.9},
                                  {"test_id": 1, "score": 0.9}]}
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["paired_cases"], 2)
        self.assertEqual(report["unpaired_before"], [])
        self.assertEqual(report["unpaired_after"], [])
        self.assertIn("0", [m["test_id"] for m in report["movements"]])

    def test_the_fallback_keys_agree_with_case_id(self):
        # Same rule on every key the fallback tries: present-and-not-null wins,
        # so `id: 0` is not passed over for a `name` further down the chain.
        for key in ("test_id", "id", "name"):
            before = {"test_results": [{key: 0, "score": 0.4}]}
            after = {"test_results": [{key: 0, "score": 0.9}]}
            report = check_compare(
                before, after, 0.05, "deterministic", "deterministic"
            )
            self.assertEqual(report["paired_cases"], 1, key)

    def test_a_null_id_is_still_absent(self):
        before = {"test_results": [{"test_id": None, "score": 0.4}]}
        after = {"test_results": [{"test_id": None, "score": 0.9}]}
        report = check_compare(before, after, 0.05, "deterministic", "deterministic")
        self.assertEqual(report["paired_cases"], 0)


class TestUnderpoweredSizingIsAdvisoryNotAHalt(unittest.TestCase):
    """Suites below the power floor may continue with a warning.

    The generator permits 2-4 cases; requiring statistical power at this gate
    would block valid suites or encourage duplicate padding.
    """

    def _write(self, tmp, cases):
        import json
        import os

        path = os.path.join(tmp, "criteria.json")
        with open(path, "w") as handle:
            json.dump({"test_cases": cases}, handle)
        return path

    def _run(self, path, *extra):
        import os
        import subprocess
        import sys

        return subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                path,
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_a_lightweight_two_case_suite_reports_underpowered_and_exits_zero(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"id": "TC-001", "test_profile": "execution"},
                {"id": "TC-002", "test_profile": "error_handling"},
            ])
            proc = self._run(path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "underpowered")
        self.assertFalse(report["blocking"])
        self.assertEqual(report["scorable_cases"], 2)
        self.assertEqual(report["effective_floor"], 5)
        # The floor it enforced and the reason both survive into the report.
        self.assertTrue(report["advisories"])
        self.assertIn("floor is 5", report["advisories"][0])
        self.assertEqual(report["errors"], [])

    def test_the_advisory_is_printed_in_the_human_readable_mode_too(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"id": "TC-001", "test_profile": "execution"},
            ])
            proc = self._run(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("VERDICT: underpowered", proc.stdout)
        self.assertIn("ADVISORY:", proc.stdout)
        self.assertIn("advisory: not blocking", proc.stdout)

    def test_a_powered_suite_is_still_powered_and_carries_no_advisory(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"id": f"TC-00{i}", "test_profile": f"p{i % 2}"} for i in range(1, 6)
            ])
            proc = self._run(path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "powered")
        self.assertFalse(report["blocking"])
        self.assertEqual(report["advisories"], [])

    def test_duplicate_ids_still_block_and_exit_one(self):
        """Duplicate ids break pairing and remain blocking despite advisory sizing."""
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"id": "TC-001", "test_profile": f"p{i}"} for i in range(6)
            ])
            proc = self._run(path, "--json")
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertTrue(report["blocking"])
        self.assertTrue(any("duplicate" in e for e in report["errors"]))

    def test_a_genuine_input_error_still_exits_two(self):
        """Advisory sizing must preserve path, read, and root-shape errors."""
        import os
        import tempfile

        missing = self._run("/nonexistent/dir/eval_criteria.json", "--json")
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "criteria.json")
            with open(bad, "w") as handle:
                handle.write("{not json")
            unreadable = self._run(bad, "--json")
        self.assertEqual(unreadable.returncode, 2)
        self.assertNotIn("Traceback", unreadable.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            rooted = os.path.join(tmp, "criteria.json")
            with open(rooted, "w") as handle:
                handle.write('[{"id": "TC-001"}]')
            non_object = self._run(rooted, "--json")
        self.assertEqual(non_object.returncode, 2)
        self.assertIn("must be a JSON object", non_object.stderr)


class TestAdvisoryHoldsInBothDirections(unittest.TestCase):
    """Underpowered suites justify neither promotion nor auto-revert in Phase 3."""

    # Sizing sees four scorable cases below a floor of five, even when all
    # five score ids happen to pair and produce a nominal regression.
    CASES = [
        {"id": f"TC-00{i}", "test_profile": "execution"} for i in range(1, 5)
    ] + [{"id": "TC-005", "test_profile": "knowledge_extraction"}]

    def _compare(self, tmp, before_scores, after_scores):
        import json
        import os
        import subprocess
        import sys

        criteria = os.path.join(tmp, "criteria.json")
        with open(criteria, "w") as handle:
            json.dump({"test_cases": self.CASES}, handle)
        paths = []
        for name, scores in (("before", before_scores), ("after", after_scores)):
            directory = os.path.join(tmp, name)
            os.makedirs(directory)
            path = os.path.join(directory, "deterministic_scores.json")
            with open(path, "w") as handle:
                json.dump(_with_scorer({"per_test": [
                    {"test_id": k, "composite": v} for k, v in scores.items()
                ]}), handle)
            paths.append(path)
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "check_eval_power.py"),
                criteria, "--before", paths[0], "--after", paths[1], "--json",
            ],
            capture_output=True,
            text=True,
        )
        return proc, json.loads(proc.stdout) if proc.stdout else None

    def test_an_underpowered_round_never_hands_phase_3_a_regressed_verdict(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc, report = self._compare(
                tmp,
                {f"TC-00{i}": 0.8 for i in range(1, 6)},
                {f"TC-00{i}": 0.3 for i in range(1, 6)},
            )
        self.assertEqual(report["verdict"], "underpowered")
        # Non-zero on purpose: `underpowered` is not a result Phase 3 may act
        # on in either direction, and exit 0 would read as the clean pass the
        # sizing half just denied.
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(report["blocking"])
        # The nominal movement survives for a human to read.
        self.assertEqual(report["comparison"]["verdict"], "regressed")
        self.assertTrue(
            any("suppressed" in w for w in report["comparison"]["warnings"])
        )

    def test_an_underpowered_round_never_claims_an_improvement_either(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc, report = self._compare(
                tmp,
                {f"TC-00{i}": 0.3 for i in range(1, 6)},
                {f"TC-00{i}": 0.9 for i in range(1, 6)},
            )
        self.assertEqual(report["verdict"], "underpowered")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(report["blocking"])

    def test_the_sizing_half_of_a_compare_report_carries_the_advisory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _, report = self._compare(
                tmp,
                {f"TC-00{i}": 0.8 for i in range(1, 6)},
                {f"TC-00{i}": 0.3 for i in range(1, 6)},
            )
        self.assertTrue(report["sizing"]["advisories"])
        self.assertFalse(report["sizing"]["blocking"])


class TestTheAdvisoryLineIsSizingOnly(unittest.TestCase):
    """An improved comparison must not print the underpowered sizing warning."""

    CASES = [{"id": f"t{i}", "test_profile": "execution"} for i in range(6)]
    ADVISORY = "justifies neither a promotion nor a revert"

    def _compare(self, tmp, before_score, after_score):
        criteria = _write_criteria(tmp, self.CASES)
        before = _write_round(
            tmp, "r1",
            [{"test_id": f"t{i}", "composite": before_score} for i in range(6)],
            "skill",
        )
        after = _write_round(
            tmp, "r2",
            [{"test_id": f"t{i}", "composite": after_score} for i in range(6)],
            "skill",
        )
        return _run_cli(criteria, "--before", before, "--after", after)

    def test_an_improved_comparison_prints_no_advisory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = self._compare(tmp, 0.5, 0.9)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("VERDICT: improved", proc.stdout)
        self.assertNotIn("advisory: not blocking", proc.stdout)
        self.assertNotIn(self.ADVISORY, proc.stdout)

    def test_a_regressed_comparison_prints_no_advisory_either(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = self._compare(tmp, 0.9, 0.5)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("advisory: not blocking", proc.stdout)

    def test_sizing_mode_still_carries_the_advisory(self):
        # The line is not deleted, only scoped: an under-floor sizing run is
        # exactly the case it was written for.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(
                tmp, [{"id": "t1", "test_profile": "execution"},
                      {"id": "t2", "test_profile": "knowledge_extraction"}]
            )
            proc = _run_cli(criteria)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("VERDICT: underpowered", proc.stdout)
        self.assertIn("advisory: not blocking", proc.stdout)

    def test_a_powered_sizing_run_prints_no_advisory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_cli(_write_criteria(tmp, self.CASES))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("VERDICT: powered", proc.stdout)
        self.assertNotIn("advisory: not blocking", proc.stdout)


class TestTheScorerThatProducedEachRoundIsChecked(unittest.TestCase):
    """Compare scorer fingerprints, regardless of how the scorer change landed.

    A merged scorer change can alter old baselines just as a local edit can.
    The round metadata must reveal that mismatch.
    """

    CASES = [{"id": f"t{i}", "test_profile": "execution"} for i in range(6)] + [
        {"id": "t6", "test_profile": "knowledge_extraction"}
    ]

    def _pair(self, tmp, before_scorer, after_scorer):
        criteria = _write_criteria(tmp, self.CASES)
        before = _write_round(
            tmp, "r1", [{"test_id": f"t{i}", "composite": 0.5} for i in range(6)],
            "skill", scorer=before_scorer,
        )
        after = _write_round(
            tmp, "r2", [{"test_id": f"t{i}", "composite": 0.9} for i in range(6)],
            "skill", scorer=after_scorer,
        )
        return criteria, before, after

    def _compare(self, tmp, before_scorer, after_scorer):
        from check_eval_power import _load_round

        _, before_path, after_path = self._pair(tmp, before_scorer, after_scorer)
        before, before_source = _load_round(before_path)
        after, after_source = _load_round(after_path)
        return check_compare(before, after, 0.05, before_source, after_source)

    def test_one_scorer_on_both_sides_still_reaches_a_verdict(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = self._compare(tmp, "ast1:aaaaaaaaaaaaaaaa", "ast1:aaaaaaaaaaaaaaaa")
        self.assertEqual(report["verdict"], "improved")
        self.assertEqual(report["errors"], [])

    def test_a_changed_scorer_is_not_measurable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = self._compare(tmp, "ast1:aaaaaaaaaaaaaaaa", "ast1:bbbbbbbbbbbbbbbb")
        self.assertEqual(report["verdict"], "not_measurable")
        said = " ".join(report["errors"])
        self.assertIn("ast1:aaaaaaaaaaaaaaaa", said)
        self.assertIn("ast1:bbbbbbbbbbbbbbbb", said)
        self.assertIn("scoring code changed", said)
        # The remedy is a re-score of the older round, not more test cases.
        self.assertIn("Re-score", said)

    def test_a_baseline_with_no_fingerprint_is_not_assumed_unchanged(self):
        # Baselines without fingerprints require re-scoring before comparison.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = self._compare(tmp, None, "ast1:bbbbbbbbbbbbbbbb")
        self.assertEqual(report["verdict"], "not_measurable")
        said = " ".join(report["errors"])
        self.assertIn("--before", said)
        self.assertNotIn("--after", said.split("Re-score")[0])
        self.assertIn("Absent is not unchanged", said)

    def test_two_unfingerprinted_rounds_are_two_unknown_scorers(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = self._compare(tmp, None, None)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIn("--before and --after", " ".join(report["errors"]))

    def test_the_report_names_both_scorers(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = self._compare(tmp, "ast1:aaaaaaaaaaaaaaaa", None)
        self.assertEqual(report["before_scorer_fingerprint"], "ast1:aaaaaaaaaaaaaaaa")
        self.assertIsNone(report["after_scorer_fingerprint"])

    def test_a_judge_deterministic_swap_is_still_reported_as_the_swap(self):
        # Precedence: the source mismatch is the more fundamental problem and
        # keeps its own message; the fingerprint check must not shadow it.
        report = check_compare(
            _deterministic({f"t{i}": 0.5 for i in range(6)}),
            _results({f"t{i}": 0.9 for i in range(6)}),
            0.05, "deterministic", "results",
        )
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIn("scorer; both are 0-1", " ".join(report["errors"]))

    def test_hand_built_rounds_are_unaffected(self):
        # `_deterministic` carries no scorer_fingerprint key at all, which is
        # a caller that never read a file, not a round of unknown provenance.
        report = check_compare(
            _deterministic({f"t{i}": 0.5 for i in range(6)}),
            _deterministic({f"t{i}": 0.9 for i in range(6)}),
            0.05, "deterministic", "deterministic",
        )
        self.assertEqual(report["verdict"], "improved")

    def test_the_cli_refuses_a_stale_baseline(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            criteria, before, after = self._pair(
                tmp, None, "ast1:bbbbbbbbbbbbbbbb"
            )
            proc = _run_cli(criteria, "--before", before, "--after", after, "--json")
        report = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(report["blocking"])
        self.assertIn(
            "scorer_fingerprint", " ".join(report["comparison"]["errors"])
        )

    def test_a_real_scorer_change_is_visible_end_to_end(self):
        """Identical executor results scored under changed logic must expose a scorer mismatch."""
        import json
        import os
        import shutil
        import subprocess
        import sys
        import tempfile

        scripts = os.path.dirname(os.path.abspath(__file__))
        gates = [
            {"step": "phase1_to_phase2", "judge": "self", "result": "pass",
             "ts": "2026-01-01T00:00:00Z"},
            {"step": "phase2_to_phase3", "judge": "self", "result": "pass",
             "ts": "2026-01-01T00:01:00Z"},
            {"step": "malformed"},
        ]
        results = {"results": [
            {
                "test_id": f"t{i}",
                "test_profile": "execution",
                "agent_response": "## Step 1\nDone.",
                "execution_timeline": [
                    {"step_type": "tool_use", "tool_name": "Read",
                     "tool_input": {"file_path": "/tmp/artifact.md"}},
                    {"step_type": "tool_use", "tool_name": "Write",
                     "tool_input": {"file_path": "/tmp/workflow-1.json",
                                    "content": json.dumps({"gates": gates})}},
                    {"step_type": "text", "text": "Phase 1 complete."},
                ],
            }
            for i in range(6)
        ]}

        def _score(round_dir, floor):
            os.makedirs(round_dir)
            for name in ("score_execution.py", "hone_common.py"):
                shutil.copy(os.path.join(scripts, name),
                            os.path.join(round_dir, name))
            scorer = os.path.join(round_dir, "score_execution.py")
            with open(scorer, encoding="utf-8") as handle:
                source = handle.read()
            with open(scorer, "w", encoding="utf-8") as handle:
                handle.write(source.replace("GATE_EVIDENCE_FLOOR = 4",
                                            f"GATE_EVIDENCE_FLOOR = {floor}", 1))
            results_path = os.path.join(round_dir, "results.json")
            with open(results_path, "w") as handle:
                json.dump(results, handle)
            proc = subprocess.run(
                [sys.executable, scorer, results_path, "--type", "skill", "--json"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            path = os.path.join(round_dir, "deterministic_scores.json")
            with open(path, "w") as handle:
                handle.write(proc.stdout)
            return path, json.loads(proc.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            criteria = _write_criteria(tmp, self.CASES)
            before, before_report = _score(os.path.join(tmp, "r1"), 4)
            after, after_report = _score(os.path.join(tmp, "r2"), 8)
            proc = _run_cli(criteria, "--before", before, "--after", after, "--json")

        # The two scorers really do disagree about the same execution trace.
        self.assertNotEqual(
            before_report["aggregate_dimensions"]["gate_compliance"],
            after_report["aggregate_dimensions"]["gate_compliance"],
        )
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertIn("scoring code changed",
                      " ".join(report["comparison"]["errors"]))


if __name__ == "__main__":
    unittest.main()
