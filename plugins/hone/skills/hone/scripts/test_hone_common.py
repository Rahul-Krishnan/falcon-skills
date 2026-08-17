#!/usr/bin/env python3
"""Tests for hone_common.py shared helpers.

Covers the null-tolerant getter (explicit-null cases especially), the
canonical score fallback chain, frontmatter extraction (block scalars
included), and cross-consumer consistency of the shared side-effect
patterns and thresholds.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hone_common import (
    ACCEPTANCE_THRESHOLD,
    ACTIONABLE_THRESHOLD,
    BASH_SIDE_EFFECT_PATTERNS,
    CRITERIA_BUG_THRESHOLD,
    FS_MUTATING_BASH_PATTERNS,
    frontmatter_field,
    get,
    load_deterministic_scores,
    load_inconclusive_ids,
    match_frontmatter,
    resolve_score,
    split_frontmatter,
)


class TestNullTolerantGet(unittest.TestCase):
    def test_missing_key_returns_default(self):
        self.assertEqual(get({}, "score", 0.0), 0.0)

    def test_explicit_null_returns_default(self):
        # The core bug this helper exists for: dict.get's default does not
        # apply to a key present with an explicit JSON null.
        self.assertEqual(get({"score": None}, "score", 0.0), 0.0)

    def test_explicit_null_dict_default(self):
        self.assertEqual(get({"details": None}, "details", {}), {})

    def test_present_value_wins(self):
        self.assertEqual(get({"score": 0.7}, "score", 0.0), 0.7)

    def test_falsy_values_are_not_treated_as_null(self):
        self.assertEqual(get({"score": 0.0}, "score", 1.0), 0.0)
        self.assertEqual(get({"s": ""}, "s", "x"), "")
        self.assertEqual(get({"l": []}, "l", ["x"]), [])

    def test_non_dict_container_returns_default(self):
        self.assertEqual(get(None, "score", 0.0), 0.0)
        self.assertEqual(get("oops", "score", 0.0), 0.0)

    def test_default_defaults_to_none(self):
        self.assertIsNone(get({}, "missing"))

    def test_expected_type_mismatch_returns_default(self):
        # Audit-path callers run on schema-invalid files by design; a
        # wrong-typed value must degrade like a null, not crash consumers.
        self.assertEqual(get({"runner_context": ["x"]}, "runner_context", "", expected=str), "")
        self.assertEqual(get({"allowed_tools": "Read"}, "allowed_tools", [], expected=list), [])

    def test_expected_type_match_returns_value(self):
        self.assertEqual(get({"prompt": "hi"}, "prompt", "", expected=str), "hi")


class TestResolveScore(unittest.TestCase):
    def test_llm_score_used_when_no_deterministic(self):
        self.assertEqual(resolve_score({"test_id": "T1", "score": 0.7}), 0.7)

    def test_explicit_null_score_falls_through(self):
        self.assertEqual(resolve_score({"test_id": "T1", "score": None}), 0.0)

    def test_final_score_alias_used_only_when_score_key_absent(self):
        self.assertEqual(
            resolve_score({"test_id": "T1", "final_score": 0.6}), 0.6
        )

    def test_explicit_null_score_skips_alias_and_uses_deterministic(self):
        # Consolidation regression pin: pre-consolidation,
        # criteria_self_repair's result.get("score", result.get("final_score"))
        # returned None for a present-but-null score (the judge errored), so
        # the test fell through to the deterministic composite. The alias
        # must not paper over an errored judge in either convention.
        result = {"test_id": "T1", "score": None, "final_score": 0.9}
        self.assertEqual(
            resolve_score(result, {"T1": 0.2}, prefer_deterministic=False), 0.2
        )
        self.assertEqual(
            resolve_score(result, {"T1": 0.2}, prefer_deterministic=True), 0.2
        )

    def test_explicit_null_score_with_alias_and_no_deterministic_is_default(self):
        result = {"test_id": "T1", "score": None, "final_score": 0.9}
        self.assertEqual(resolve_score(result, {}), 0.0)
        self.assertEqual(resolve_score(result, {}, prefer_deterministic=False), 0.0)

    def test_deterministic_preferred_by_default(self):
        result = {"test_id": "T1", "score": 0.2}
        self.assertEqual(resolve_score(result, {"T1": 0.9}), 0.9)

    def test_llm_preferred_when_prefer_deterministic_false(self):
        result = {"test_id": "T1", "score": 0.2}
        self.assertEqual(
            resolve_score(result, {"T1": 0.9}, prefer_deterministic=False), 0.2
        )

    def test_deterministic_fallback_when_no_llm_score(self):
        result = {"test_id": "T1", "score": None}
        self.assertEqual(
            resolve_score(result, {"T1": 0.4}, prefer_deterministic=False), 0.4
        )

    def test_default_when_nothing_available(self):
        self.assertEqual(resolve_score({"test_id": "T1"}, {}), 0.0)

    def test_zero_deterministic_composite_is_a_real_score(self):
        result = {"test_id": "T1", "score": 0.9}
        self.assertEqual(resolve_score(result, {"T1": 0.0}), 0.0)


class TestDeterministicLoaders(unittest.TestCase):
    def _write(self, det_data) -> str:
        tmpdir = tempfile.mkdtemp()
        results_path = os.path.join(tmpdir, "results.json")
        with open(results_path, "w") as f:
            json.dump({"results": []}, f)
        with open(os.path.join(tmpdir, "deterministic_scores.json"), "w") as f:
            json.dump(det_data, f)
        return results_path

    def test_missing_file_returns_empty(self):
        tmpdir = tempfile.mkdtemp()
        results_path = os.path.join(tmpdir, "results.json")
        self.assertEqual(load_deterministic_scores(results_path), {})
        self.assertEqual(load_inconclusive_ids(results_path), set())

    def test_null_composite_excluded_and_marked_inconclusive(self):
        results_path = self._write(
            {
                "per_test": [
                    {"test_id": "T1", "composite": 0.8},
                    {"test_id": "T2", "composite": None},
                    {"test_id": "T3", "composite": 0.5, "status": "inconclusive"},
                ]
            }
        )
        self.assertEqual(load_deterministic_scores(results_path), {"T1": 0.8, "T3": 0.5})
        self.assertEqual(load_inconclusive_ids(results_path), {"T2", "T3"})

    def test_non_dict_det_file_degrades_to_empty(self):
        # "{} on any failure" includes a file that parses as a non-object
        # (e.g. [] from truncation); returning it raw crashed both loaders.
        results_path = self._write([])
        self.assertEqual(load_deterministic_scores(results_path), {})
        self.assertEqual(load_inconclusive_ids(results_path), set())

    def test_non_dict_per_test_entries_are_ignored(self):
        results_path = self._write(
            {"per_test": [{"test_id": "T1", "composite": 0.8}, 7, "junk", None]}
        )
        self.assertEqual(load_deterministic_scores(results_path), {"T1": 0.8})
        self.assertEqual(load_inconclusive_ids(results_path), set())

    def test_null_per_test_degrades_to_empty(self):
        results_path = self._write({"per_test": None})
        self.assertEqual(load_deterministic_scores(results_path), {})
        self.assertEqual(load_inconclusive_ids(results_path), set())


class TestThresholdConsistency(unittest.TestCase):
    def test_threshold_ordering(self):
        self.assertLess(CRITERIA_BUG_THRESHOLD, ACCEPTANCE_THRESHOLD)
        self.assertLess(ACCEPTANCE_THRESHOLD, ACTIONABLE_THRESHOLD)

    def test_consumers_share_the_module_constants(self):
        import analyze_results

        self.assertIs(analyze_results.ACTIONABLE_THRESHOLD, ACTIONABLE_THRESHOLD)
        self.assertIs(analyze_results.CRITERIA_BUG_THRESHOLD, CRITERIA_BUG_THRESHOLD)


class TestSideEffectPatternConsistency(unittest.TestCase):
    """The guard's sandbox patterns and the validator's hygiene patterns
    previously drifted apart; both must derive from hone_common."""

    def test_guard_uses_shared_patterns(self):
        import side_effect_guard

        self.assertEqual(
            [(p, label) for p, label, _resp in side_effect_guard.BASH_SIDE_EFFECTS],
            BASH_SIDE_EFFECT_PATTERNS,
        )

    def test_guard_has_a_simulated_response_for_every_pattern(self):
        import side_effect_guard

        for _p, _label, response in side_effect_guard.BASH_SIDE_EFFECTS:
            self.assertTrue(response)

    def test_validator_uses_shared_fs_patterns(self):
        import validate_eval_criteria

        validator_pairs = [
            (compiled.pattern, label)
            for compiled, label in validate_eval_criteria._FS_MUTATING_PATTERNS
            if label != "SETUP: block"  # validator-specific extra pattern
        ]
        self.assertEqual(validator_pairs, FS_MUTATING_BASH_PATTERNS)

    def test_fs_patterns_are_a_subset_of_guard_patterns(self):
        self.assertTrue(
            set(FS_MUTATING_BASH_PATTERNS) <= set(BASH_SIDE_EFFECT_PATTERNS)
        )


class TestFrontmatterExtraction(unittest.TestCase):
    def test_no_frontmatter(self):
        self.assertIsNone(split_frontmatter("# Just a doc\n"))

    def test_basic_split(self):
        fm = split_frontmatter("---\nname: x\ndescription: hi\n---\nBody")
        self.assertEqual(fm, "name: x\ndescription: hi")

    def test_frontmatter_closed_at_eof(self):
        # A file ending exactly at the closing delimiter previously failed
        # open in side_effect_guard's local regex (required a trailing \n).
        fm = split_frontmatter("---\nname: x\n---")
        self.assertEqual(fm, "name: x")

    def test_delimiter_inside_body_not_matched(self):
        self.assertIsNone(split_frontmatter("Intro\n---\nname: x\n---\n"))

    def test_match_frontmatter_end_is_body_offset(self):
        content = "---\nname: x\n---\nBody line"
        m = match_frontmatter(content)
        self.assertEqual(content[m.end():], "Body line")

    def test_inline_field(self):
        self.assertEqual(frontmatter_field("name: my-skill", "name"), "my-skill")

    def test_missing_field(self):
        self.assertIsNone(frontmatter_field("name: x", "description"))

    def test_inline_flow_list(self):
        fm = "allowed-tools: [Read, Grep]"
        self.assertEqual(frontmatter_field(fm, "allowed-tools"), "[Read, Grep]")

    def test_block_list(self):
        fm = "allowed-tools:\n  - Read\n  - Grep"
        self.assertEqual(frontmatter_field(fm, "allowed-tools"), "- Read\n- Grep")

    def test_block_scalar_pipe(self):
        fm = "description: |\n  Line one.\n  Line two.\nname: x"
        self.assertEqual(
            frontmatter_field(fm, "description"), "Line one.\nLine two."
        )

    def test_block_scalar_folded_with_chomping(self):
        fm = "description: >-\n  Folded line one\n  and two."
        self.assertEqual(
            frontmatter_field(fm, "description"), "Folded line one\nand two."
        )

    def test_block_scalar_with_blank_interior_line(self):
        fm = "description: |\n  Para one.\n\n  Para two."
        self.assertEqual(
            frontmatter_field(fm, "description"), "Para one.\n\nPara two."
        )

    def test_bare_key_without_block_returns_none(self):
        self.assertIsNone(frontmatter_field("allowed-tools:\nname: x", "allowed-tools"))

    def test_block_ends_at_next_top_level_key(self):
        fm = "description: |\n  Text.\nallowed-tools: [Read]"
        self.assertEqual(frontmatter_field(fm, "description"), "Text.")
        self.assertEqual(frontmatter_field(fm, "allowed-tools"), "[Read]")


if __name__ == "__main__":
    unittest.main()
