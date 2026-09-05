#!/usr/bin/env python3
"""Tests for hone_common.py shared helpers.

Covers the null-tolerant getter (explicit-null cases especially), the
canonical score fallback chain, frontmatter extraction (block scalars
included), and cross-consumer consistency of the shared side-effect
patterns and thresholds.
"""

from __future__ import annotations

import itertools
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
    HALT_REASONS,
    IN_PLACE_REPAIR_REASONS,
    RESTART_AUTHORIZING_REASONS,
    RESUMABLE_STEPS,
    PHASE1_STEPS,
    PHASE23_STEPS,
    PHASE3_HALT_SEQUENCE,
    REPEATABLE_STEPS,
    RUN_SHAPE_ACTIVE_STEPS,
    derive_gate_mode,
    derive_run_shape,
    declared_halt_reason,
    fail_is_accounted,
    fail_orders_halt,
    find_slash_invocations,
    frontmatter_field,
    get,
    halt_tail_vocabulary,
    is_halt_tail,
    is_authorized_restart,
    is_repeatable_step,
    is_settled_by_retry,
    load_deterministic_scores,
    load_inconclusive_ids,
    match_frontmatter,
    resolve_score,
    split_frontmatter,
    step_declares_reason,
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


def _g(step, result="pass", reason=None):
    event = {"step": step, "judge": "self-check", "result": result}
    if reason is not None:
        event["reason"] = reason
    return event


class TestRepeatableSteps(unittest.TestCase):
    """Membership is documented retry semantics, not mere recurrence."""

    def test_convergence_and_handoffs_are_repeatable(self):
        self.assertTrue(is_repeatable_step("convergence"))
        self.assertTrue(is_repeatable_step("handoff_round_1_scores"))

    def test_per_round_steps_without_retry_semantics_are_not(self):
        for step in ("phase3_exit", "phase2_to_phase3", "phase1_to_phase2",
                     "fixonly_entry", "resume", "workflow_exit"):
            with self.subTest(step=step):
                self.assertFalse(is_repeatable_step(step))

    def test_a_non_string_step_is_not_repeatable(self):
        for step in (None, 3, ["convergence"]):
            with self.subTest(step=step):
                self.assertFalse(is_repeatable_step(step))

    def test_phase3_exit_is_not_in_the_set(self):
        """A phase3_exit fail is a mandated halt, so a later one is a violation."""
        self.assertNotIn("phase3_exit", REPEATABLE_STEPS)


class TestFailKinds(unittest.TestCase):
    """What a `fail` MEANS is the axis every settlement rule turns on."""

    def test_a_handoff_fail_is_a_validation_verdict(self):
        self.assertFalse(fail_orders_halt("handoff_round_1_scores"))

    def test_every_other_gate_fail_is_a_halt_order(self):
        for step in ("convergence", "phase3_exit", "phase2_to_phase3",
                     "phase1_to_phase2", "fixonly_entry", "resume",
                     "workflow_exit"):
            with self.subTest(step=step):
                self.assertTrue(fail_orders_halt(step))

    def test_an_unrecognised_step_reads_as_a_halt_order(self):
        """The strict default: a new gate must not be settleable by running on."""
        for step in ("some_future_gate", None, 3):
            with self.subTest(step=step):
                self.assertTrue(fail_orders_halt(step))


class TestSettledByRetry(unittest.TestCase):
    """A documented retry settles a fail, on terms set by what the fail meant.

    The exit-2 ledger repair emits `convergence:fail`, rewrites the ledger,
    re-runs, and emits a second `convergence` that fails whenever the re-run
    returns escalate or capped. The first fail is then neither terminal (the
    halt tail slice is exclusive of the failing step) nor followed by a pass,
    so a correct repair scored exactly as an ignored halt did.
    """

    EXIT_2_REPAIR = [_g("convergence", "fail"), _g("workflow_exit")]

    def test_an_adjacent_retry_settles_a_declared_ledger_repair(self):
        """The exit-2 repair: re-run adjacent, then the halt it reported."""
        self.assertTrue(is_settled_by_retry(
            self.EXIT_2_REPAIR, "convergence", "ledger_missing"))

    def test_the_same_repair_undeclared_settles_nothing(self):
        """The migration cost, asserted rather than assumed.

        `convergence` has a vocabulary, so an event that declares nothing
        cannot say it was the repairable exit-2 failure, and "cannot tell"
        resolves to "refuse" here as everywhere. Every state file written
        before `reason` existed is in this row and loses this settlement on a
        repair that was correct. The alternative -- letting the undeclared
        event keep the retry -- is what made a truthful `escalate` score
        WORSE than silence, since `escalate` forfeits it.
        """
        self.assertFalse(is_settled_by_retry(self.EXIT_2_REPAIR,
                                             "convergence"))

    def test_a_retry_with_no_halt_behind_it_settles_nothing(self):
        """A run that stopped emitting before its mandated exit is not a halt.

        The adjacent retry alone used to be enough. It is not: the halt has
        to be recorded, and `workflow_exit` is what records it.
        """
        self.assertFalse(is_settled_by_retry([_g("convergence", "fail")],
                                             "convergence"))

    def test_a_halt_orders_retry_is_fail_closed_on_whatever_follows_it(self):
        """The property, not one ordering of it.

        For a halt order, an adjacent retry settles the fail ONLY when
        everything after the retry is that halt's own tail. Any step the halt
        vocabulary does not admit -- whatever it is, wherever the reviewer
        finds the next one -- makes the fail unaccounted. This is the
        asymmetry the restriction is for: over-strict is a lost quarter-point
        on `gate_compliance`, permissive is a run credited for ignoring a
        halt.
        """
        vocabulary = halt_tail_vocabulary("convergence")
        intruders = [
            step for step in (
                "phase1_to_phase2", "phase2_to_phase3", "phase3_exit",
                "fixonly_entry", "handoff_input", "resume",
            ) if step not in vocabulary
        ]
        self.assertTrue(intruders, "the test needs steps outside the tail")
        for intruder in intruders:
            for result in ("pass", "fail"):
                with self.subTest(step=intruder, result=result):
                    self.assertFalse(is_settled_by_retry(
                        [_g("convergence", "pass"), _g(intruder, result),
                         _g("workflow_exit")],
                        "convergence"))

    def test_the_reported_post_retry_laundering_shape(self):
        """The exact sequence round 6 found: the extra round moved AFTER the
        retry instead of before it."""
        self.assertFalse(fail_is_accounted(
            [_g("convergence", "pass"), _g("phase2_to_phase3"),
             _g("phase3_exit"), _g("convergence"), _g("workflow_exit")],
            "convergence"))

    def test_a_validation_verdict_retry_is_not_constrained_from_behind(self):
        """The restriction is scoped to halt orders.

        A `handoff_<name>` fail ordered nothing about the run, so work after
        its retry was never forbidden and must not be read as laundering.
        """
        self.assertTrue(is_settled_by_retry(
            [_g("handoff_input", "fail"), _g("phase2_to_phase3"),
             _g("workflow_exit")],
            "handoff_input"))

    def test_a_non_repeatable_step_is_never_settled_by_retry(self):
        self.assertFalse(is_settled_by_retry([_g("phase3_exit", "fail")],
                                             "phase3_exit"))

    def test_forward_progress_between_the_attempts_does_not_settle(self):
        """The bypass: ignore the halt, do another round, fail again."""
        tail = [_g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence", "fail"), _g("workflow_exit", "fail")]
        self.assertFalse(is_settled_by_retry(tail, "convergence"))

    def test_a_later_pass_does_not_settle_a_halt_order_across_a_gap(self):
        """The same bypass, reached through the repaired path.

        `convergence` is emitted every round, so the next round's `pass` was
        affirmative evidence for a halt order it had no business excusing.
        """
        tail = [_g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence", "pass"), _g("workflow_exit")]
        self.assertFalse(is_settled_by_retry(tail, "convergence"))

    def test_a_later_pass_settles_a_validation_verdict_across_any_gap(self):
        """The handoff repair loop: nothing in between was forbidden."""
        tail = [_g("phase2_to_phase3"), _g("handoff_interfaces", "pass")]
        self.assertTrue(is_settled_by_retry(tail, "handoff_interfaces"))

    def test_no_later_attempt_does_not_settle(self):
        self.assertFalse(is_settled_by_retry([_g("workflow_exit")],
                                             "convergence"))

    def test_a_non_list_tail_is_not_settled(self):
        self.assertFalse(is_settled_by_retry("gates", "convergence"))


class TestAuthorizedRestart(unittest.TestCase):
    """A recorded halt plus a recorded `resume` is the one way past a halt order."""

    def test_a_recorded_halt_then_resume_authorizes_the_restart(self):
        """`capped` in --confirm mode: the loop stops, the human restarts it.

        `workflow_exit` comes BEFORE the `resume`, because the reference puts
        the human gate outside and after the FORCED halt: asking is what
        happens once the loop has stopped. The halt declares `capped`, which
        is what says it is the one `convergence:fail` a human may restart.
        """
        tail = [_g("workflow_exit"), _g("resume"),
                _g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence", "fail", "capped"), _g("workflow_exit")]
        self.assertTrue(
            is_authorized_restart(tail, "convergence", "capped"))

    def test_a_resume_with_no_halt_in_front_of_it_does_not(self):
        """A restart with no recorded exit is a run that never stopped."""
        tail = [_g("resume"), _g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence", "fail"), _g("workflow_exit", "fail")]
        self.assertFalse(is_authorized_restart(tail, "convergence"))

    def test_forward_progress_before_the_resume_does_not(self):
        """The gap before the restart has to be a halt, not another round."""
        tail = [_g("phase2_to_phase3"), _g("workflow_exit"),
                _g("resume"), _g("phase3_exit"),
                _g("convergence", "fail"), _g("workflow_exit", "fail")]
        self.assertFalse(is_authorized_restart(tail, "convergence"))

    def test_a_failed_exit_may_be_resumed_immediately(self):
        """`workflow_exit:fail` IS the stop, so nothing need precede the resume."""
        tail = [_g("resume"), _g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence"), _g("workflow_exit")]
        self.assertTrue(is_authorized_restart(tail, "workflow_exit"))

    def test_no_resume_at_all_does_not(self):
        self.assertFalse(is_authorized_restart([_g("workflow_exit")],
                                               "convergence"))

    def test_a_non_list_tail_is_not_a_restart(self):
        self.assertFalse(is_authorized_restart("gates", "convergence"))

    def test_a_failed_resume_authorizes_nothing(self):
        """`resume:fail` is a restart that did not happen.

        The predicate read the event's presence and never its `result`, so a
        failed restart settled the halt exactly as a granted one did.
        """
        tail = [_g("workflow_exit"), _g("resume", "fail"),
                _g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence"), _g("workflow_exit")]
        self.assertFalse(is_authorized_restart(tail, "convergence"))

    def test_only_documented_restarts_are_authorized(self):
        """A `resume` is not a universal laundering suffix.

        `convergence` has the `capped` human gate and `workflow_exit` has the
        cross-session resume; no other gate has a documented way back from
        its own halt, so appending an exit and a `resume` behind one settles
        nothing.
        """
        for step in ("phase3_exit", "phase1_to_phase2", "phase2_to_phase3"):
            with self.subTest(step=step):
                tail = [_g("workflow_exit"), _g("resume"),
                        _g("phase2_to_phase3"), _g("phase3_exit"),
                        _g("convergence"), _g("workflow_exit")]
                self.assertFalse(is_authorized_restart(tail, step))
                self.assertFalse(fail_is_accounted(tail, step))


class TestFailIsAccounted(unittest.TestCase):
    """The one predicate both callers now share."""

    def test_the_documented_exit_2_repair_is_accounted(self):
        """convergence:fail (ledger_missing) -> re-run -> convergence:fail.

        The re-run's own event declares the verdict it came back with, which
        is `capped` here; the docs re-attempt neither halt verdict, so that
        second fail is accounted for by its halt tail rather than by a retry.
        """
        gates = [
            _g("phase1_to_phase2"), _g("phase2_to_phase3"), _g("phase3_exit"),
            _g("convergence", "fail", "ledger_missing"),
            _g("convergence", "fail", "capped"),
            _g("workflow_exit", "fail"),
        ]
        for index, gate in enumerate(gates):
            if gate["result"] != "fail":
                continue
            with self.subTest(index=index):
                self.assertTrue(fail_is_accounted(
                    gates[index + 1:], gate["step"], gate.get("reason")))

    def test_the_repaired_handoff_chain_is_accounted(self):
        gates = [
            _g("handoff_interfaces", "fail"), _g("handoff_interfaces", "fail"),
            _g("handoff_interfaces", "pass"),
        ]
        for index in (0, 1):
            with self.subTest(index=index):
                self.assertTrue(
                    fail_is_accounted(gates[index + 1:], "handoff_interfaces"))

    def test_a_handoff_that_never_repairs_and_never_halts_is_not(self):
        """The same hole as convergence, on the step the docs already retry."""
        gates = [
            _g("handoff_interfaces", "fail"), _g("handoff_interfaces", "fail"),
            _g("phase2_to_phase3"), _g("workflow_exit"),
        ]
        self.assertFalse(fail_is_accounted(gates[2:], "handoff_interfaces"))

    def test_a_run_that_ignored_the_convergence_halt_is_not_accounted(self):
        gates = [
            _g("convergence", "fail"), _g("phase2_to_phase3"),
            _g("phase3_exit"), _g("convergence", "fail"),
            _g("workflow_exit", "fail"),
        ]
        self.assertFalse(fail_is_accounted(gates[1:], "convergence"))

    def test_the_documented_regression_halt_is_still_accounted(self):
        tail = [_g("convergence"), _g("workflow_exit")]
        self.assertTrue(fail_is_accounted(tail, "phase3_exit"))

    def test_a_later_phase3_exit_does_not_launder_the_auto_revert_halt(self):
        tail = [_g("phase2_to_phase3"), _g("phase3_exit", "fail"),
                _g("workflow_exit", "fail")]
        self.assertFalse(fail_is_accounted(tail, "phase3_exit"))

    def test_a_later_convergence_pass_does_not_launder_the_escalate_halt(self):
        """`convergence:fail` is escalate, a mandated immediate halt.

        The run did another whole round and emitted a passing `convergence`,
        which the unconstrained repaired path read as affirmative evidence.
        """
        gates = [
            _g("phase1_to_phase2"), _g("phase2_to_phase3"), _g("phase3_exit"),
            _g("convergence", "fail"), _g("phase2_to_phase3"),
            _g("phase3_exit"), _g("convergence", "pass"), _g("workflow_exit"),
        ]
        self.assertFalse(fail_is_accounted(gates[4:], "convergence"))

    def test_a_later_phase3_exit_pass_does_not_launder_the_auto_revert_halt(self):
        """The same class one door along: the auto-revert halt, then a pass."""
        gates = [
            _g("phase3_exit", "fail"), _g("phase2_to_phase3"),
            _g("phase3_exit", "pass"), _g("convergence"), _g("workflow_exit"),
        ]
        self.assertFalse(fail_is_accounted(gates[1:], "phase3_exit"))

    def test_a_later_pass_does_not_launder_any_halt_ordering_gate(self):
        """Stated once, so every gate whose fail is a halt order is covered."""
        for step in ("phase1_to_phase2", "phase2_to_phase3", "fixonly_entry",
                     "phase3_exit", "convergence"):
            with self.subTest(step=step):
                tail = [_g("phase2_to_phase3"), _g(step, "pass"),
                        _g("workflow_exit")]
                self.assertFalse(fail_is_accounted(tail, step))

    def test_the_authorized_restart_is_still_accounted(self):
        """The documented capped -> --confirm -> resume sequence, end to end."""
        gates = [
            _g("convergence", "fail", "capped"), _g("workflow_exit", "fail"),
            _g("resume"), _g("phase2_to_phase3"), _g("phase3_exit"),
            _g("convergence", "fail", "capped"), _g("workflow_exit", "fail"),
        ]
        for index, gate in enumerate(gates):
            if gate["result"] != "fail":
                continue
            with self.subTest(index=index):
                self.assertTrue(fail_is_accounted(
                    gates[index + 1:], gate["step"], gate.get("reason")))


# Every way an event can decline to declare a usable reason. All four are one
# answer -- "not declared" -- and every predicate below asserts that answer is
# the conservative one. This tuple is shared so a new non-answer added here is
# checked against every concession at once.
NON_DECLARATIONS = (
    ("absent", None),
    ("empty string", ""),
    ("whitespace only", "   "),
    ("unknown string", "definitely_not_a_verdict"),
    ("near miss (case)", "Capped"),
    ("near miss (padding)", " capped "),
    ("a verdict name that is not a fail", "in_progress"),
    ("integer", 7),
    ("boolean", True),
    ("list", ["capped"]),
    ("dict", {"reason": "capped"}),
)


class TestHaltReasonVocabulary(unittest.TestCase):
    """The closed vocabulary, stated once and mirrored nowhere in code.

    `convergence:fail` is three different events wearing one name, and the
    settlement rules for them are opposites. The vocabulary is what the
    declaration is read against, and it lives in hone_common because a
    security predicate must not change behaviour when a data file is missing.
    """

    def test_the_vocabulary_is_the_three_documented_failures(self):
        self.assertEqual(
            HALT_REASONS["convergence"],
            frozenset({"ledger_missing", "escalate", "capped"}))

    def test_only_convergence_has_a_vocabulary(self):
        """A step earns one exactly when a predicate reads its declaration.

        `reason` is free-form annotation everywhere else (SKILL.md puts
        `corrupt_state_file` on a `workflow_exit` and "prior evaluation
        reused" on a `fixonly_entry`), so closing the enum over every event
        would invalidate documented examples for no gain.
        """
        self.assertEqual(set(HALT_REASONS), {"convergence"})
        for step in ("workflow_exit", "phase3_exit", "phase2_to_phase3",
                     "handoff_interfaces", "scope_verify", "resume"):
            with self.subTest(step=step):
                self.assertFalse(step_declares_reason(step))
                self.assertIsNone(declared_halt_reason(step, "capped"))

    def test_each_concession_is_a_subset_of_the_vocabulary(self):
        """A concession keyed on a word the vocabulary does not define is dead
        code that reads as a live rule."""
        vocabulary = set().union(*HALT_REASONS.values())
        self.assertLessEqual(RESTART_AUTHORIZING_REASONS, vocabulary)
        self.assertLessEqual(IN_PLACE_REPAIR_REASONS, vocabulary)

    def test_the_concessions_are_disjoint_and_escalate_is_in_neither(self):
        """`escalate` is named precisely so it can be refused both ways."""
        self.assertFalse(RESTART_AUTHORIZING_REASONS & IN_PLACE_REPAIR_REASONS)
        self.assertNotIn("escalate", RESTART_AUTHORIZING_REASONS)
        self.assertNotIn("escalate", IN_PLACE_REPAIR_REASONS)
        self.assertIn("escalate", HALT_REASONS["convergence"])

    def test_the_published_schema_mirrors_the_vocabulary(self):
        """references/gate-event-schema.json is what executors read.

        hone_common is authoritative and the schema mirrors it; a drift
        between them is an executor told to write a word no predicate accepts,
        which is the fail-closed direction but still a bug.
        """
        schema_path = (Path(__file__).resolve().parent.parent
                       / "references" / "gate-event-schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("reason", schema["properties"])
        branches = [
            branch for branch in schema.get("allOf", [])
            if branch.get("if", {}).get("properties", {})
            .get("step", {}).get("const") == "convergence"
        ]
        self.assertEqual(len(branches), 1, "one convergence branch expected")
        enum = branches[0]["then"]["properties"]["reason"]["enum"]
        self.assertEqual(set(enum), set(HALT_REASONS["convergence"]))

    def test_a_declaration_in_the_vocabulary_reads_back(self):
        for reason in sorted(HALT_REASONS["convergence"]):
            with self.subTest(reason=reason):
                self.assertEqual(
                    declared_halt_reason("convergence", reason), reason)

    def test_every_non_declaration_reads_as_undeclared(self):
        """Absent, empty, unknown, wrong-typed: one answer, and it is None.

        Stated once here so the predicate tests below can assert the
        consequence rather than re-enumerate the causes.
        """
        for label, value in NON_DECLARATIONS:
            with self.subTest(case=label):
                self.assertIsNone(declared_halt_reason("convergence", value))

    def test_a_non_string_step_declares_nothing(self):
        for step in (None, 7, ["convergence"], {"step": "convergence"}):
            with self.subTest(step=repr(step)):
                self.assertFalse(step_declares_reason(step))


class TestDeclarationGatedRestart(unittest.TestCase):
    """The KNOWN GAP closed: only `capped` reaches the human gate.

    `escalate` and `capped` produce identical event sequences -- the halt, the
    exit, the grant, another round -- so no predicate over the list could tell
    them apart. The reference is explicit that continuing after `escalate` is
    a fresh `/hone` invocation, so the event says which verdict it carried and
    the predicate reads it.
    """

    TAIL = [_g("workflow_exit"), _g("resume"), _g("phase2_to_phase3"),
            _g("phase3_exit"), _g("convergence", "fail", "capped"),
            _g("workflow_exit")]

    def test_a_capped_halt_may_be_restarted(self):
        self.assertTrue(
            is_authorized_restart(self.TAIL, "convergence", "capped"))
        self.assertTrue(
            fail_is_accounted(self.TAIL, "convergence", "capped"))

    def test_the_same_shape_after_an_escalate_may_not(self):
        """The whole point: same events, different declaration, opposite answer."""
        self.assertFalse(
            is_authorized_restart(self.TAIL, "convergence", "escalate"))
        self.assertFalse(
            fail_is_accounted(self.TAIL, "convergence", "escalate"))

    def test_a_ledger_repair_is_not_a_restart(self):
        """`ledger_missing` is repaired in place, not granted by a human."""
        self.assertFalse(
            is_authorized_restart(self.TAIL, "convergence", "ledger_missing"))

    def test_no_usable_declaration_authorizes_no_restart(self):
        """Fail-closed over every non-answer, including the legacy one.

        A `convergence:fail` written before this field existed carries no
        reason and is no longer resumable. That is the conservative direction:
        it costs gate score on a correct legacy capped-and-resumed run and
        cannot credit one that ignored an escalate.
        """
        for label, value in NON_DECLARATIONS:
            with self.subTest(case=label):
                self.assertFalse(
                    is_authorized_restart(self.TAIL, "convergence", value))
                self.assertFalse(
                    fail_is_accounted(self.TAIL, "convergence", value))

    def test_a_step_with_no_vocabulary_needs_no_declaration(self):
        """`workflow_exit:fail` has exactly one meaning, so nothing to declare.

        The requirement lands where the ambiguity is. Widening it to every
        resumable step would break the documented cross-session resume for no
        gain, because that step's `fail` is not two halts sharing a name.
        """
        tail = [_g("resume"), _g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence"), _g("workflow_exit")]
        self.assertTrue(is_authorized_restart(tail, "workflow_exit"))
        self.assertTrue(is_authorized_restart(tail, "workflow_exit", None))

    def test_the_declaration_does_not_excuse_the_other_three_conditions(self):
        """`capped` unlocks the restart; it does not replace it."""
        no_halt = [_g("resume"), _g("phase2_to_phase3"), _g("workflow_exit")]
        self.assertFalse(
            is_authorized_restart(no_halt, "convergence", "capped"))
        failed_resume = [_g("workflow_exit"), _g("resume", "fail"),
                         _g("phase2_to_phase3"), _g("workflow_exit")]
        self.assertFalse(
            is_authorized_restart(failed_resume, "convergence", "capped"))
        forward_first = [_g("phase2_to_phase3"), _g("workflow_exit"),
                         _g("resume"), _g("workflow_exit")]
        self.assertFalse(
            is_authorized_restart(forward_first, "convergence", "capped"))
        self.assertFalse(
            is_authorized_restart("gates", "convergence", "capped"))

    def test_a_declaration_cannot_make_an_unresumable_step_resumable(self):
        """`RESUMABLE_STEPS` is checked first and is not negotiable."""
        for step in ("phase3_exit", "phase2_to_phase3", "phase1_to_phase2"):
            with self.subTest(step=step):
                self.assertNotIn(step, RESUMABLE_STEPS)
                self.assertFalse(
                    is_authorized_restart(self.TAIL, step, "capped"))


class TestDeclarationGatedRetry(unittest.TestCase):
    """The CONSERVATIVE ON PURPOSE restriction, which applies to every retry.

    A revision of `is_settled_by_retry` lifted the rule for a declared
    `ledger_missing`, on the reasoning that the exit-2 repair's re-run drives
    the round and owes nothing behind it. The reasoning about the repair is
    right and the concession was still a bypass: the honest repair's event
    sequence is byte-identical to the round 6 laundering shape, `reason` is
    written by the run being scored, and nothing corroborates it -- so the
    declaration separated the two only for an executor that chose to tell the
    truth. These tests pin the withdrawal: `ledger_missing` is read only as
    the one reason a retry may settle AT ALL, under the same blunt rule an
    undeclared fail gets.
    """

    # The exit-2 repair whose re-run came back `in_progress`: the second
    # `convergence` passes and the run goes on to another round. Also, step
    # for step and result for result, the round 6 laundering shape.
    CONTINUING_REPAIR = [
        _g("convergence", "pass"), _g("phase2_to_phase3"), _g("phase3_exit"),
        _g("convergence", "pass"), _g("workflow_exit"),
    ]

    def test_declaring_the_repair_does_not_buy_the_extra_round(self):
        """The exploit, closed. Lying must not beat telling the truth.

        This shape is the round 6 laundering sequence. Every reading of it
        refuses, so an executor that ignored an `escalate` cannot launder the
        halt by writing `ledger_missing` into the event it already controls.
        """
        for reason in (None, "ledger_missing", "escalate", "capped"):
            with self.subTest(reason=reason):
                self.assertFalse(fail_is_accounted(
                    self.CONTINUING_REPAIR, "convergence", reason))

    def test_no_usable_declaration_changes_the_blunt_rule(self):
        """Fail-closed over every non-answer, and over every real one too."""
        for label, value in NON_DECLARATIONS:
            with self.subTest(case=label):
                self.assertFalse(fail_is_accounted(
                    self.CONTINUING_REPAIR, "convergence", value))

    def test_the_declared_repair_still_owes_a_halt_tail_behind_the_retry(self):
        """The `workflow_exit` requirement is not dropped by declaring.

        `[convergence:pass]` alone is a run that repaired the ledger and then
        stopped emitting gates before its mandated exit. It refuses whatever
        the fail declared, which is the shape `is_halt_tail` exists to catch.
        """
        for reason in (None, "ledger_missing"):
            with self.subTest(reason=reason):
                self.assertFalse(fail_is_accounted(
                    [_g("convergence", "pass")], "convergence", reason))
        # The same repair that DOES reach its exit is still accounted, so the
        # rule costs the honest halted repair nothing.
        self.assertTrue(fail_is_accounted(
            [_g("convergence", "pass"), _g("workflow_exit")],
            "convergence", "ledger_missing"))

    def test_a_chain_of_declared_repairs_cannot_launder_itself(self):
        """references/phase3-reevaluation.md: a second exit 2 is an error halt.

        Two `ledger_missing` fails in a row followed by more rounds used to
        settle both with no warning and no error at all.
        """
        chain = [
            _g("convergence", "fail", "ledger_missing"),
            _g("convergence", "pass"), _g("phase2_to_phase3"),
            _g("phase3_exit"), _g("convergence", "pass"),
            _g("workflow_exit"),
        ]
        self.assertFalse(fail_is_accounted(chain, "convergence",
                                           "ledger_missing"))

    def test_a_declared_forced_halt_has_no_retry_at_all(self):
        """Stricter than the rule it replaces, and deliberately.

        Neither `escalate` nor `capped` is re-attempted anywhere in the docs,
        so a second `convergence` behind one is not a repair. Such a fail is
        accounted for by its halt tail, or for `capped` by a restart, or not
        at all.
        """
        for reason in ("escalate", "capped"):
            for tail in (self.CONTINUING_REPAIR,
                         [_g("convergence", "fail"), _g("workflow_exit")]):
                with self.subTest(reason=reason, tail=len(tail)):
                    self.assertFalse(
                        is_settled_by_retry(tail, "convergence", reason))

    def test_a_declared_repair_still_needs_an_empty_gap(self):
        """The retry is in place, so a round between the attempts is not it."""
        tail = [_g("phase2_to_phase3"), _g("phase3_exit"),
                _g("convergence", "pass"), _g("workflow_exit")]
        self.assertFalse(
            is_settled_by_retry(tail, "convergence", "ledger_missing"))

    def test_the_re_run_still_has_to_account_for_itself(self):
        """A repair whose re-run halts is settled; one that carries on is not.

        The re-run is a fresh verdict. When it comes back `escalate` and the
        run then halts, both events are accounted -- the honest repair costs
        nothing. When the run instead does another round, neither is.
        """
        halted = [
            _g("convergence", "fail", "ledger_missing"),
            _g("convergence", "fail", "escalate"),
            _g("workflow_exit", "fail"),
        ]
        self.assertTrue(fail_is_accounted(halted[1:], "convergence",
                                          "ledger_missing"))
        self.assertTrue(fail_is_accounted(halted[2:], "convergence",
                                          "escalate"))
        ignored = [
            _g("convergence", "fail", "ledger_missing"),
            _g("convergence", "fail", "escalate"),
            _g("phase2_to_phase3"), _g("phase3_exit"),
            _g("convergence", "pass"), _g("workflow_exit"),
        ]
        self.assertFalse(fail_is_accounted(ignored[1:], "convergence",
                                           "ledger_missing"))
        self.assertFalse(fail_is_accounted(ignored[2:], "convergence",
                                           "escalate"))

    def test_a_declaration_cannot_make_a_non_repeatable_step_retryable(self):
        for step in ("phase3_exit", "phase2_to_phase3"):
            with self.subTest(step=step):
                self.assertFalse(is_settled_by_retry(
                    [_g(step, "pass"), _g("workflow_exit")], step,
                    "ledger_missing"))


class TestDeclarationDoesNotRegressTheKnownShapes(unittest.TestCase):
    """Every shape the four previous rounds settled, re-asserted here.

    Restated in one place so the next change to this area has a single list
    to run. All but one read exactly as they did before the declaration
    existed, because their step has no vocabulary (`phase3_exit`,
    `handoff_<name>`) or because the shape was refused either way.

    THE ONE THAT MOVED is the undeclared exit-2 in-place repair, and it moved
    on purpose. A `convergence:fail` that declares nothing forfeits the retry
    along with the restart, so this shape is no longer accounted. That costs
    gate score on a correct legacy run, and it is the price of the invariant:
    while the undeclared event kept the retry, a truthful `escalate` -- which
    forfeits it -- scored strictly worse than silence or a typo.
    """

    def test_the_documented_shapes_hold(self):
        EXIT_2_REPAIR = [_g("convergence", "fail"),
                         _g("workflow_exit", "fail")]
        cases = [
            ("round 6 laundering: the extra round after the retry",
             [_g("convergence", "pass"), _g("phase2_to_phase3"),
              _g("phase3_exit"), _g("convergence"), _g("workflow_exit")],
             "convergence", None, False),
            ("the documented auto-revert halt",
             [_g("convergence"), _g("workflow_exit")],
             "phase3_exit", None, True),
            ("the exit-2 in-place repair, declared",
             EXIT_2_REPAIR, "convergence", "ledger_missing", True),
            ("the exit-2 in-place repair, undeclared: the migration cost",
             EXIT_2_REPAIR, "convergence", None, False),
            ("a handoff verdict repaired across a gap",
             [_g("phase2_to_phase3"), _g("handoff_interfaces", "pass")],
             "handoff_interfaces", None, True),
            ("an exit and a resume behind a failed phase3_exit",
             [_g("workflow_exit"), _g("resume"), _g("phase2_to_phase3"),
              _g("phase3_exit"), _g("convergence"), _g("workflow_exit")],
             "phase3_exit", None, False),
            ("a capped halt with a halt tail and a passing resume",
             [_g("workflow_exit", "fail"), _g("resume"),
              _g("phase2_to_phase3"), _g("phase3_exit"),
              _g("convergence"), _g("workflow_exit")],
             "convergence", "capped", True),
            ("the same shape declared escalate",
             [_g("workflow_exit", "fail"), _g("resume"),
              _g("phase2_to_phase3"), _g("phase3_exit"),
              _g("convergence"), _g("workflow_exit")],
             "convergence", "escalate", False),
        ]
        for label, tail, step, reason, expected in cases:
            with self.subTest(case=label):
                self.assertIs(fail_is_accounted(tail, step, reason), expected)

    def test_the_default_argument_is_the_conservative_one(self):
        """A caller that passes no declaration gets the undeclared reading.

        The signature could have defaulted the other way and nothing would
        have failed loudly; this asserts it did not.
        """
        tail = TestDeclarationGatedRetry.CONTINUING_REPAIR
        self.assertEqual(fail_is_accounted(tail, "convergence"),
                         fail_is_accounted(tail, "convergence", None))
        self.assertFalse(fail_is_accounted(tail, "convergence"))


class TestNoDeclarationBeatsTheTruth(unittest.TestCase):
    """The invariant as a property, over the full cross product.

    Three sets of hand-picked cases missed this twice, in opposite
    directions, so it is asserted here as a property over generated event
    tails rather than as more examples. The two halves:

    NO DECLARATION MAY EXCEED THE PRE-`reason` BASELINE. Round 1 broke this:
    a declared `ledger_missing` reached a retry the same events could not
    otherwise reach, so lying paid. The baseline is what the same tail scored
    when `reason` was read by nothing -- halt tail OR restart OR blunt retry,
    all ungated.

    NO NON-DECLARATION MAY BEAT A DECLARATION. Round 2 broke this: the retry
    guard subtracted only when the reason RESOLVED, so absent, empty and
    misspelled values kept a retry that a truthful `escalate` or `capped`
    forfeited, and telling the truth cost 0.1667 of gate_compliance. Both
    guards are now written `declared_halt_reason(...) not in <SET>` so that
    `None` fails the membership test, which is the shape that satisfies both
    halves at once.

    The non-answers cover the reviewer's whole list: each vocabulary value,
    absent, empty, whitespace, unknown, near-miss (case and padding), and the
    wrong types.
    """

    NON_ANSWERS = (
        None, "", "   ", "\t\n", "nope", "escalate: f1,f2 open 4 rounds",
        "Capped", " capped ", "CAPPED", "ledger-missing", 3, 0, True, False,
        3.5, ["capped"], {"reason": "capped"}, (), object(),
    )
    STEP_POOL = ("convergence", "workflow_exit", "phase2_to_phase3",
                 "phase3_exit", "resume", "scope_verify", "handoff_x")

    @staticmethod
    def _baseline(tail, step):
        """What this tail settled before `reason` was read at all.

        A FROZEN COPY of the two predicates as they stood on main, not a
        re-derivation from the live ones. Deriving the baseline by feeding
        each gated predicate the reason that does not gate it looks
        equivalent and is not: the baseline then moves whenever the predicate
        does, so a change that loosened the rule for `ledger_missing` -- the
        round-1 bug, exactly -- would raise the baseline with it and pass.
        Verified by mutation: the derived form missed that; this one catches
        it. Update this copy only when `is_halt_tail` itself changes, and
        never to make a failing assertion pass.
        """
        def restart(gates):
            if step not in RESUMABLE_STEPS:
                return False
            if not isinstance(gates, (list, tuple)):
                return False
            for index, event in enumerate(gates):
                if isinstance(event, dict) and event.get("step") == "resume":
                    if event.get("result") != "pass":
                        return False
                    return is_halt_tail(gates[:index], step)
            return False

        def retry(gates):
            if not is_repeatable_step(step):
                return False
            if not isinstance(gates, (list, tuple)):
                return False
            if not fail_orders_halt(step) and any(
                isinstance(later, dict)
                and later.get("step") == step
                and later.get("result") == "pass"
                for later in gates
            ):
                return True
            for offset, later in enumerate(gates):
                if isinstance(later, dict) and later.get("step") == step:
                    if gates[:offset]:
                        return False
                    if fail_orders_halt(step):
                        return is_halt_tail(gates[offset + 1:], step)
                    return True
            return False

        return is_halt_tail(tail, step) or restart(tail) or retry(tail)

    def _tails(self):
        """Every tail up to length 3 over a pool of steps and both results.

        Exhaustive rather than random: at these lengths the cross product is
        small enough to enumerate, and an enumerated property does not depend
        on a seed to stay reproducible.
        """
        events = [_g(step, result)
                  for step in self.STEP_POOL for result in ("pass", "fail")]
        for length in range(4):
            for combo in itertools.product(events, repeat=length):
                yield list(combo)

    def test_no_declaration_reaches_more_than_the_undeclared_baseline(self):
        for step in self.STEP_POOL:
            for tail in self._tails():
                baseline = self._baseline(tail, step)
                if baseline:
                    continue
                for value in tuple(HALT_REASONS.get(step, ())) \
                        + self.NON_ANSWERS:
                    if fail_is_accounted(tail, step, value):
                        self.fail(
                            f"{value!r} on a {step} fail reached a settlement "
                            f"the pre-reason baseline refused, tail={tail}")

    def test_no_non_answer_beats_any_vocabulary_value(self):
        for step in self.STEP_POOL:
            vocabulary = tuple(HALT_REASONS.get(step, ()))
            if not vocabulary:
                continue
            for tail in self._tails():
                truths = {v: fail_is_accounted(tail, step, v)
                          for v in vocabulary}
                for value in self.NON_ANSWERS:
                    if not fail_is_accounted(tail, step, value):
                        continue
                    for word, accounted in truths.items():
                        if not accounted:
                            self.fail(
                                f"{value!r} was accounted on a {step} fail "
                                f"where the truthful {word!r} was not, "
                                f"tail={tail}")

    def test_a_step_with_no_vocabulary_ignores_the_field_entirely(self):
        """`reason` is free-form annotation everywhere outside HALT_REASONS.

        Closing the enum globally would invalidate SKILL.md's own examples
        (`corrupt_state_file` on a `workflow_exit`), so the predicates must
        read nothing at all on those steps -- which also means no value can
        change an outcome there, in either direction.
        """
        for step in self.STEP_POOL:
            if step_declares_reason(step):
                continue
            for tail in self._tails():
                expected = fail_is_accounted(tail, step, None)
                for value in tuple(self.NON_ANSWERS) + ("capped", "escalate"):
                    self.assertIs(
                        fail_is_accounted(tail, step, value), expected,
                        f"{value!r} changed the outcome on {step}")
