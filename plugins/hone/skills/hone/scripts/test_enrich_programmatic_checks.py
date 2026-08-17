#!/usr/bin/env python3
"""Tests for enrich_programmatic_checks.py."""

from __future__ import annotations

import unittest

from enrich_programmatic_checks import (
    apply_enrichment,
    enrich_test_case,
    extract_identifiers,
    filter_candidates,
    get_check_texts,
    rank_and_select,
    strip_stale_present,
)

SAMPLE_ARTIFACT = """
## Parse Arguments

- `{type}` — `skill`, `command`, `hook`, or `script`
- `{name}` — artifact name (required)
- `{mode}` — `--auto` (default) or `--confirm`
- `{max_rounds}` — `--rounds N` (default 3)
- `{target_score}` — `--target N.N` (default: none)
- `{no_visualize}` — `--no-visualize` to skip HTML report

## Structural Audit

Checks 11 structural pillars: progress_gates, handoff_interfaces,
state_persistence, schema_validation, anti_laziness, research_depth,
complexity_aware, data_provenance, security, description_guardrails,
script_quality.

## Dimensions

Default dimensions: task_completion (0.3), invocation (0.2), efficiency (0.2),
best_practices (0.15), business_impact (0.15).
"""


class TestExtractIdentifiers(unittest.TestCase):
    def test_extracts_underscore_identifiers(self) -> None:
        text = (
            "Correctly identifies max_rounds, task_completion, and progress_gates "
            "from the artifact"
        )
        result = extract_identifiers(text)
        self.assertIn("max_rounds", result)
        self.assertIn("task_completion", result)
        self.assertIn("progress_gates", result)

    def test_ignores_single_words(self) -> None:
        text = "Correctly identifies the score and name from output"
        result = extract_identifiers(text)
        self.assertEqual(result, [])

    def test_deduplicates(self) -> None:
        text = "max_rounds and max_rounds again and max_rounds once more"
        result = extract_identifiers(text)
        self.assertEqual(result.count("max_rounds"), 1)

    def test_ignores_uppercase(self) -> None:
        text = "Contains MAX_ROUNDS and Task_Completion"
        result = extract_identifiers(text)
        self.assertNotIn("MAX_ROUNDS", result)
        self.assertNotIn("Task_Completion", result)


class TestFilterCandidates(unittest.TestCase):
    def test_excludes_common_words(self) -> None:
        candidates = ["test_case", "file_path", "max_rounds"]
        result = filter_candidates(candidates, SAMPLE_ARTIFACT, [], [])
        identifiers = [item["identifier"] for item in result]
        self.assertNotIn("test_case", identifiers)
        self.assertNotIn("file_path", identifiers)
        self.assertIn("max_rounds", identifiers)

    def test_excludes_absent_conflicts(self) -> None:
        candidates = ["max_rounds", "task_completion"]
        result = filter_candidates(candidates, SAMPLE_ARTIFACT, ["max_rounds"], [])
        identifiers = [item["identifier"] for item in result]
        self.assertNotIn("max_rounds", identifiers)
        self.assertIn("task_completion", identifiers)

    def test_requires_artifact_presence(self) -> None:
        candidates = ["max_rounds", "nonexistent_identifier"]
        result = filter_candidates(candidates, SAMPLE_ARTIFACT, [], [])
        identifiers = [item["identifier"] for item in result]
        self.assertIn("max_rounds", identifiers)
        self.assertNotIn("nonexistent_identifier", identifiers)

    def test_excludes_already_present(self) -> None:
        candidates = ["max_rounds", "task_completion"]
        result = filter_candidates(candidates, SAMPLE_ARTIFACT, [], ["max_rounds"])
        identifiers = [item["identifier"] for item in result]
        self.assertNotIn("max_rounds", identifiers)
        self.assertIn("task_completion", identifiers)

    def test_excludes_long_identifiers(self) -> None:
        long_id = "this_is_a_very_long_identifier_name_that_exceeds_thirty_chars"
        artifact_with_long = SAMPLE_ARTIFACT + f"\n{long_id}\n"
        candidates = [long_id]
        result = filter_candidates(candidates, artifact_with_long, [], [])
        self.assertEqual(result, [])


class TestRankAndSelect(unittest.TestCase):
    def test_ranks_by_specificity(self) -> None:
        candidates = [
            {"identifier": "common_one", "occurrences": 10},
            {"identifier": "rare_one", "occurrences": 1},
            {"identifier": "medium_one", "occurrences": 5},
        ]
        result = rank_and_select(candidates, 3)
        self.assertEqual(result[0], "rare_one")
        self.assertEqual(result[1], "medium_one")
        self.assertEqual(result[2], "common_one")

    def test_caps_at_max(self) -> None:
        candidates = [
            {"identifier": f"id_{index}", "occurrences": index} for index in range(10)
        ]
        result = rank_and_select(candidates, 3)
        self.assertEqual(len(result), 3)


class TestEnrichTestCase(unittest.TestCase):
    def test_skips_error_handling(self) -> None:
        test_case = {
            "id": "TC-007",
            "category": "error-handling",
            "checks": [{"description": "Recognized max_rounds as invalid"}],
        }
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["added"], [])

    def test_enriches_knowledge_test(self) -> None:
        test_case = {
            "id": "TC-001",
            "category": "invocation",
            "checks": [
                {
                    "description": (
                        "Correctly identifies max_rounds, no_visualize, "
                        "and target_score from the argument list"
                    ),
                    "rubric": {
                        "1": "Misses most variables",
                        "5": "Lists all parsed variables",
                    },
                }
            ],
        }
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        self.assertFalse(result["skipped"])
        self.assertGreater(len(result["added"]), 0)
        for added_id in result["added"]:
            self.assertIn(added_id, SAMPLE_ARTIFACT.lower())

    def test_no_checks(self) -> None:
        test_case = {"id": "TC-X", "category": "quality", "checks": []}
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        self.assertTrue(result["skipped"])

    def test_only_scans_description_field(self) -> None:
        test_case = {
            "id": "TC-X",
            "category": "quality",
            "checks": [
                {
                    "description": "Correctly identifies progress_gates",
                    "rubric": {
                        "1": "Missing artifact_before_snapshot entirely",
                        "5": "Full answer with eval_results and open_questions",
                    },
                }
            ],
        }
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        self.assertIn("progress_gates", result["added"])
        # rubric-only identifiers should NOT appear
        self.assertNotIn("artifact_before_snapshot", result["added"])
        self.assertNotIn("eval_results", result["added"])
        self.assertNotIn("open_questions", result["added"])


class TestIdempotency(unittest.TestCase):
    # A 12-identifier description, well over the default cap of 5. The original
    # test used 2 candidates against a cap of 5, so the pool never exceeded the
    # cap and the per-invocation budget bug was invisible.
    OVERSUBSCRIBED_CHECK = (
        "Covers max_rounds, target_score, progress_gates, handoff_interfaces, "
        "state_persistence, schema_validation, anti_laziness, research_depth, "
        "complexity_aware, data_provenance, description_guardrails, "
        "script_quality"
    )

    def _run(self, test_case: dict, max_per_test: int = 5) -> list[str]:
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, max_per_test)
        apply_enrichment(test_case, result["added"])
        return result["added"]

    def test_second_run_adds_nothing(self) -> None:
        test_case = {
            "id": "TC-001",
            "category": "invocation",
            "checks": [{"description": "Identifies max_rounds and target_score"}],
        }
        # First enrichment
        result1 = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        apply_enrichment(test_case, result1["added"])

        # Second enrichment on same (now-enriched) test case
        result2 = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        self.assertEqual(result2["added"], [])

    def test_cap_is_per_test_case_not_per_invocation(self) -> None:
        # The Phase 1 / Phase 3 hazard: both phases enrich the same criteria,
        # so a per-invocation cap grew required_present 5 -> 10 -> 12 and
        # Phase 3 scored against strictly more anchors than Phase 1 did.
        test_case = {
            "id": "TC-001",
            "category": "invocation",
            "checks": [{"description": self.OVERSUBSCRIBED_CHECK}],
        }
        self.assertEqual(len(self._run(test_case)), 5)
        self.assertEqual(self._run(test_case), [])
        self.assertEqual(self._run(test_case), [])
        self.assertEqual(len(test_case["required_present"]), 5)

    def test_hand_written_phrases_do_not_consume_the_budget(self) -> None:
        # Phrases assert on the agent response, not the artifact text, so they
        # are not enrichment-owned and must not starve a test case of anchors.
        test_case = {
            "id": "TC-002",
            "category": "invocation",
            "checks": [{"description": self.OVERSUBSCRIBED_CHECK}],
            "required_present": [
                "I cannot proceed without a name",
                "Provide a target score",
            ],
        }
        self.assertEqual(len(self._run(test_case)), 5)
        self.assertEqual(len(test_case["required_present"]), 7)
        self.assertEqual(self._run(test_case), [])

    def test_raised_cap_admits_more_anchors(self) -> None:
        # Converging is not the same as freezing: raising the cap between runs
        # must still let the next most specific identifiers in.
        test_case = {
            "id": "TC-003",
            "category": "invocation",
            "checks": [{"description": self.OVERSUBSCRIBED_CHECK}],
        }
        self.assertEqual(len(self._run(test_case, 5)), 5)
        self.assertEqual(len(self._run(test_case, 8)), 3)
        self.assertEqual(len(test_case["required_present"]), 8)


class TestStripStalePresent(unittest.TestCase):
    def test_strips_identifier_no_longer_in_artifact(self) -> None:
        test_case = {
            "id": "TC-001",
            "required_present": ["max_rounds", "renamed_old_flag"],
        }
        removed = strip_stale_present(test_case, SAMPLE_ARTIFACT)
        self.assertEqual(removed, ["renamed_old_flag"])
        self.assertEqual(test_case["required_present"], ["max_rounds"])

    def test_keeps_hand_written_phrases(self) -> None:
        # Phrases assert on the agent response, not the artifact text; they
        # must survive even though they never occur in the artifact.
        test_case = {
            "id": "TC-002",
            "required_present": ["I cannot proceed without a name", "max_rounds"],
        }
        removed = strip_stale_present(test_case, SAMPLE_ARTIFACT)
        self.assertEqual(removed, [])
        self.assertEqual(
            test_case["required_present"],
            ["I cannot proceed without a name", "max_rounds"],
        )

    def test_noop_on_missing_or_invalid_field(self) -> None:
        self.assertEqual(strip_stale_present({"id": "TC-003"}, SAMPLE_ARTIFACT), [])
        self.assertEqual(
            strip_stale_present(
                {"id": "TC-004", "required_present": "not-a-list"}, SAMPLE_ARTIFACT
            ),
            [],
        )

    def test_refresh_after_rename_is_idempotent(self) -> None:
        # Simulates the Phase 3 refresh: an identifier enriched earlier is
        # renamed by Phase 2; the stale name must go, the new one can come in.
        test_case = {
            "id": "TC-005",
            "category": "invocation",
            "checks": [{"description": "Honors target_score"}],
            "required_present": ["old_target_flag"],
        }
        removed = strip_stale_present(test_case, SAMPLE_ARTIFACT)
        self.assertEqual(removed, ["old_target_flag"])
        result = enrich_test_case(test_case, SAMPLE_ARTIFACT, 5)
        apply_enrichment(test_case, result["added"])
        self.assertIn("target_score", test_case["required_present"])
        self.assertNotIn("old_target_flag", test_case["required_present"])


class TestGetCheckTexts(unittest.TestCase):
    def test_checks_layout(self) -> None:
        test_case = {
            "checks": [
                {"description": "First check"},
                {"description": "Second check"},
            ]
        }
        texts = get_check_texts(test_case)
        self.assertEqual(texts, ["First check", "Second check"])

    def test_string_checks(self) -> None:
        test_case = {
            "checks": ["Q1", "Q2"]
        }
        texts = get_check_texts(test_case)
        self.assertEqual(texts, ["Q1", "Q2"])


if __name__ == "__main__":
    unittest.main()
