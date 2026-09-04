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
    PHASE1_STEPS,
    PHASE23_STEPS,
    PHASE3_HALT_SEQUENCE,
    RUN_SHAPE_ACTIVE_STEPS,
    derive_gate_mode,
    derive_run_shape,
    find_slash_invocations,
    frontmatter_field,
    get,
    halt_tail_vocabulary,
    is_halt_tail,
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

    def test_non_numeric_score_falls_through_like_null(self):
        # A stringified score ("0.85") passed straight through crashed
        # analyze_results on round(score, 4) / threshold comparisons; it
        # must fall through to the deterministic composite/default exactly
        # as an explicit null does.
        self.assertEqual(resolve_score({"test_id": "T1", "score": "0.85"}, {}), 0.0)
        self.assertEqual(
            resolve_score(
                {"test_id": "T1", "score": "0.85"},
                {"T1": 0.4},
                prefer_deterministic=False,
            ),
            0.4,
        )

    def test_bool_and_list_scores_fall_through(self):
        # bool is an int subclass but a JSON boolean is not a number.
        self.assertEqual(resolve_score({"test_id": "T1", "score": True}, {}), 0.0)
        self.assertEqual(resolve_score({"test_id": "T1", "final_score": [1]}, {}), 0.0)


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


class TestNonStringTestId(unittest.TestCase):
    """A non-string test_id must degrade, not raise TypeError.

    dict.get hashes its key even on an empty dict, and set.add hashes its
    member, so an unhashable test_id (list, dict) crashed resolve_score and
    both loaders with a raw traceback and no JSON on stdout — starving the
    Phase 2 triage gate.
    """

    def test_resolve_score_tolerates_unhashable_test_id(self):
        result = {"test_id": ["a"], "score": 0.7}
        self.assertEqual(resolve_score(result, {}), 0.7)
        # With no usable score anywhere, the default applies.
        self.assertEqual(resolve_score({"test_id": {"x": 1}}, {}), 0.0)

    def test_loaders_drop_non_string_test_id_entries(self):
        tmpdir = tempfile.mkdtemp()
        results_path = os.path.join(tmpdir, "results.json")
        with open(results_path, "w") as f:
            json.dump({"results": []}, f)
        with open(os.path.join(tmpdir, "deterministic_scores.json"), "w") as f:
            json.dump(
                {
                    "per_test": [
                        {"test_id": ["a"], "composite": 0.8},
                        {"test_id": {"x": 1}, "composite": None},
                        {"test_id": "T1", "composite": 0.6},
                    ]
                },
                f,
            )
        self.assertEqual(load_deterministic_scores(results_path), {"T1": 0.6})
        self.assertEqual(load_inconclusive_ids(results_path), set())


class TestSlashInvocationDetection(unittest.TestCase):
    """The shared delegation detector (sandboxer and auditor must agree)."""

    def test_trailing_punctuation_and_backticks_match(self):
        # The auditor's old local regex required whitespace/EOL after the
        # command, so these shapes were sandboxed but never drew the
        # missing_skill_tool repair.
        self.assertEqual(find_slash_invocations("Run /forge."), ["forge"])
        self.assertEqual(
            find_slash_invocations("Invoke /hone, then report"), ["hone"]
        )
        self.assertEqual(find_slash_invocations("Use `/forge` now"), ["forge"])

    def test_paths_and_stoplist_heads_do_not_match(self):
        self.assertEqual(
            find_slash_invocations("see /tmp/x, factor/face, src/spbench"),
            [],
        )
        self.assertEqual(find_slash_invocations("a bare /tmp mention"), [])

    def test_dedup_preserves_first_appearance_order(self):
        self.assertEqual(
            find_slash_invocations("/forge then /hone then /forge again"),
            ["forge", "hone"],
        )


class TestRunShapes(unittest.TestCase):
    """derive_run_shape / derive_gate_mode / the active-steps table."""

    def test_shape_derivation(self):
        self.assertEqual(
            derive_run_shape({"phase1_evaluate": "skipped"}), "fix-only"
        )
        self.assertEqual(
            derive_run_shape(
                {"phase1_evaluate": "done", "phase2_improve": "skipped"}
            ),
            "no-improvement",
        )
        self.assertEqual(
            derive_run_shape(
                {"phase1_evaluate": "done", "phase2_improve": "done"}
            ),
            "normal",
        )
        # Tier-based skips of other Phase 1 steps do not change the shape.
        self.assertEqual(
            derive_run_shape(
                {
                    "phase1_structural_audit": "skipped",
                    "phase1_evaluate": "done",
                }
            ),
            "normal",
        )
        # Absent/unusable steps derive the strictest shape.
        self.assertEqual(derive_run_shape(None), "normal")
        self.assertEqual(derive_run_shape({}), "normal")

    def test_gate_mode_derivation(self):
        done = {step: "done" for step in PHASE1_STEPS + PHASE23_STEPS}
        self.assertEqual(derive_gate_mode(done), "normal")
        self.assertEqual(
            derive_gate_mode(dict(done, phase2_improve="in_progress")),
            "error-halt",
        )
        fixonly = {
            **{step: "skipped" for step in PHASE1_STEPS},
            **{step: "done" for step in PHASE23_STEPS},
        }
        self.assertEqual(derive_gate_mode(fixonly), "fix-only")
        # Underivable: the caller falls back to --mode / "normal".
        self.assertIsNone(derive_gate_mode({}))
        self.assertIsNone(derive_gate_mode(None))

    def test_lax_shapes_partition_the_normal_shape(self):
        self.assertEqual(
            RUN_SHAPE_ACTIVE_STEPS["fix-only"]
            | RUN_SHAPE_ACTIVE_STEPS["no-improvement"],
            RUN_SHAPE_ACTIVE_STEPS["normal"],
        )
        self.assertEqual(
            RUN_SHAPE_ACTIVE_STEPS["fix-only"]
            & RUN_SHAPE_ACTIVE_STEPS["no-improvement"],
            frozenset(),
        )


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

    def test_crlf_frontmatter_parses(self):
        # CRLF line endings previously failed to parse entirely, silently
        # disabling side_effect_guard's allowed-tools filter and dropping
        # every frontmatter field in structural_audit.
        fm = split_frontmatter("---\r\nname: foo\r\n---\r\nbody\r\n")
        self.assertEqual(fm, "name: foo")
        self.assertEqual(frontmatter_field(fm, "name"), "foo")

    def test_crlf_frontmatter_closed_at_eof(self):
        self.assertEqual(split_frontmatter("---\r\nname: x\r\n---"), "name: x")

    def test_crlf_multi_field_extraction(self):
        fm = split_frontmatter(
            "---\r\nname: foo\r\ndescription: hi there\r\n---\r\nBody"
        )
        self.assertIsNotNone(fm)
        self.assertEqual(frontmatter_field(fm, "description"), "hi there")

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


class TestHaltTail(unittest.TestCase):
    """One definition of a halt tail, shared by validate_gates and the scorer.

    Both used to carry their own copy and the copies had drifted:
    validate_gates accepted a tail of `convergence` alone, and neither checked
    that the trailing `convergence` had actually failed. `convergence` has
    since left the halt vocabulary entirely -- hone never emitted it.
    """

    def _gate(self, step, result="fail"):
        return {"step": step, "judge": "self-check", "result": result, "ts": "t"}

    def test_empty_tail_is_a_halt_only_for_the_exit_event(self):
        """Nothing after the fail is a halt only when the fail IS the exit.

        SKILL.md mandates `workflow_exit` before any exit, so a run whose last
        recorded gate is a failing *other* step stopped emitting gates before
        the event it owed. Reading that as a halt scored an executor that
        emitted fewer events above one that emitted the honest tail.
        """
        self.assertTrue(is_halt_tail([], "workflow_exit"))
        self.assertFalse(is_halt_tail([], "phase3_exit"))
        self.assertFalse(is_halt_tail([], None))

    def test_workflow_exit_is_required(self):
        self.assertFalse(is_halt_tail([self._gate("phase3_exit")]))

    def test_the_documented_regression_halt_is_a_halt(self):
        """The auto-revert shape: phase3_exit fails, then the exit event."""
        self.assertTrue(is_halt_tail(
            [self._gate("workflow_exit", "pass")], "phase3_exit"
        ))

    def test_an_unreached_step_before_the_exit_is_not_a_halt(self):
        """Regression (#14): an invented event must not launder a fail.

        `convergence` sat in HALT_SEQUENCE_STEPS while hone emitted no such
        gate, so an executor that appended one turned every failed gate into a
        compliant halt and the eval paid for a step that never ran.

        Phase 3 now really does emit `convergence` -- but only Phase 3 does. A
        gate that failed BEFORE Phase 3 ran is still proof the run never
        reached it, so a `convergence` behind such a fail is still invented.
        This is the half of #14's fix that must survive; the halt the same
        emission order makes legitimate is asserted in TestHaltTailVocabulary.
        """
        for failed_step in ("phase1_to_phase2", "phase2_to_phase3", "handoff_x"):
            for result in ("pass", "fail"):
                for invented in ("convergence", "made_up_step"):
                    with self.subTest(step=failed_step, result=result,
                                      invented=invented):
                        self.assertFalse(is_halt_tail(
                            [
                                self._gate(invented, result),
                                self._gate("workflow_exit", "pass"),
                            ],
                            failed_step,
                        ))
        # A step outside the Phase 3 sequence stays inadmissible even behind a
        # fail that demonstrably DID reach Phase 3.
        for failed_step in ("phase3_exit", "convergence"):
            with self.subTest(step=failed_step):
                self.assertFalse(is_halt_tail(
                    [
                        self._gate("made_up_step", "pass"),
                        self._gate("workflow_exit", "pass"),
                    ],
                    failed_step,
                ))

    def test_passing_workflow_exit_is_still_a_halt(self):
        self.assertTrue(is_halt_tail([self._gate("workflow_exit", "pass")]))

    def test_a_fail_with_no_exit_after_it_is_not_a_halt(self):
        self.assertFalse(is_halt_tail(
            [self._gate("phase3_exit", "fail")], "phase2_to_phase3"
        ))

    def test_forward_progress_is_not_a_halt(self):
        self.assertFalse(is_halt_tail(
            [self._gate("phase2_improve", "pass"), self._gate("workflow_exit")]
        ))

    def test_non_dict_entries_do_not_read_as_a_halt(self):
        self.assertFalse(is_halt_tail(["workflow_exit"]))
        self.assertFalse(is_halt_tail(None))


class TestHaltTailVocabulary(unittest.TestCase):
    """The tail vocabulary depends on which gate failed, not on a flat set.

    Two properties have to hold at the same time, and a flat set can only ever
    hold one of them. Phase 3 emits `phase3_exit` (reference step 6) and THEN
    `convergence` (step 7), so:

      * behind a failed `phase3_exit` -- the documented regression auto-revert
        halt -- a `convergence` in the tail is an event the run really did
        emit, and the tail is a halt;
      * behind a gate that failed before Phase 3 ran, the same `convergence`
        was invented, and the tail is not a halt (#14's bypass).

    An earlier revision put `convergence` back in the flat HALT_SEQUENCE_STEPS
    to get the first property and silently reopened the second.
    """

    def _gate(self, step, result="pass"):
        return {"step": step, "judge": "self-check", "result": result, "ts": "t"}

    def test_convergence_in_the_tail_is_a_halt_after_a_failed_phase3_exit(self):
        """The documented auto-revert halt, in the order the docs emit it."""
        for result in ("pass", "fail"):
            with self.subTest(convergence_result=result):
                self.assertTrue(is_halt_tail(
                    [
                        self._gate("convergence", result),
                        self._gate("workflow_exit", "pass"),
                    ],
                    "phase3_exit",
                ))

    def test_convergence_in_the_tail_is_invented_before_phase3_runs(self):
        """#14's bypass: the laundering tail must still be rejected."""
        for failed_step in ("phase1_to_phase2", "phase2_to_phase3",
                            "fixonly_entry"):
            with self.subTest(step=failed_step):
                self.assertFalse(is_halt_tail(
                    [
                        self._gate("convergence", "pass"),
                        self._gate("workflow_exit", "pass"),
                    ],
                    failed_step,
                ))

    def test_the_exit_alone_is_still_a_halt_after_phase3_exit(self):
        """The vocabulary says what MAY follow, not what must."""
        self.assertTrue(is_halt_tail(
            [self._gate("workflow_exit", "pass")], "phase3_exit"
        ))

    def test_a_convergence_tail_without_the_exit_is_not_a_halt(self):
        """`workflow_exit` is mandated before ANY exit, so it is required."""
        self.assertFalse(is_halt_tail(
            [self._gate("convergence", "pass")], "phase3_exit"
        ))

    def test_the_failing_step_is_not_admitted_into_its_own_tail(self):
        """Phase 3 re-emits both events every round.

        Admitting the failing step back into its own tail would score a run
        that failed `phase3_exit`, ignored the mandated immediate halt, and
        looped through another whole round as though it had stopped.
        """
        self.assertFalse(is_halt_tail(
            [
                self._gate("phase3_exit", "fail"),
                self._gate("convergence", "pass"),
                self._gate("workflow_exit", "pass"),
            ],
            "phase3_exit",
        ))

    def test_a_failed_convergence_admits_only_the_exit(self):
        """A failed `convergence` IS the failing gate, not a tail behind one."""
        self.assertTrue(is_halt_tail(
            [self._gate("workflow_exit", "pass")], "convergence"
        ))
        self.assertEqual(halt_tail_vocabulary("convergence"),
                         frozenset({"workflow_exit"}))

    def test_the_vocabulary_matches_the_documented_emission_order(self):
        self.assertEqual(
            PHASE3_HALT_SEQUENCE, ("phase3_exit", "convergence", "workflow_exit")
        )
        self.assertEqual(
            halt_tail_vocabulary("phase3_exit"),
            frozenset({"convergence", "workflow_exit"}),
        )
        for unrelated in ("phase2_to_phase3", "phase1_to_phase2", None):
            with self.subTest(step=unrelated):
                self.assertEqual(halt_tail_vocabulary(unrelated),
                                 frozenset({"workflow_exit"}))


if __name__ == "__main__":
    unittest.main()
