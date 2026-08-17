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
