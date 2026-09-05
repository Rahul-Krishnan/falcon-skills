#!/usr/bin/env python3
"""Tests for validate_gates.py."""

from __future__ import annotations

import unittest

from validate_gates import derive_edits_applied, validate_gates


def gate(step, result="pass", judge="self-check", reason=None):
    event = {"step": step, "judge": judge, "result": result,
             "ts": "2026-08-16T00:00:00Z"}
    if reason is not None:
        event["reason"] = reason
    return event


NORMAL_RUN = [
    gate("phase1_to_phase2"),
    gate("phase2_to_phase3"),
    gate("phase3_exit"),
    gate("convergence"),
    gate("workflow_exit"),
]


class TestSchema(unittest.TestCase):
    def test_complete_normal_run_is_valid(self):
        report = validate_gates(NORMAL_RUN, "normal")
        self.assertTrue(report["valid"])
        self.assertEqual(report["errors"], [])

    def test_invalid_result_value_is_error(self):
        gates = NORMAL_RUN[:-1] + [gate("workflow_exit", result="enter_phase2")]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("only 'pass' or 'fail'" in e for e in report["errors"]))

    def test_missing_key_is_error(self):
        gates = NORMAL_RUN[:-1] + [{"step": "workflow_exit", "result": "pass"}]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("missing required key 'judge'" in e for e in report["errors"]))

    def test_unknown_judge_is_error(self):
        # The judge vocabulary is a closed enum owned by
        # gate-event-schema.json; a state file violating the published
        # schema must not be blessed by the deterministic gate check.
        gates = NORMAL_RUN[:-1] + [gate("workflow_exit", judge="vibes")]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("judge 'vibes'" in e for e in report["errors"]))

    def test_valid_rubric_passes(self):
        rubric = [
            {"severity": "CRITICAL", "item": "0 CRITICAL findings", "result": "pass"},
            {"severity": "LOW", "item": "style check", "result": "warn"},
        ]
        gates = NORMAL_RUN[:-1] + [dict(gate("workflow_exit"), rubric=rubric)]
        self.assertTrue(validate_gates(gates, "normal")["valid"])

    def test_rubric_result_enum_violation_is_error(self):
        rubric = [{"severity": "HIGH", "item": "x", "result": "maybe"}]
        gates = NORMAL_RUN[:-1] + [dict(gate("workflow_exit"), rubric=rubric)]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("rubric[0] result" in e for e in report["errors"]))

    def test_rubric_missing_keys_and_bad_severity_are_errors(self):
        rubric = [{"severity": "SEVERE", "result": "pass"}, "not-an-object"]
        gates = NORMAL_RUN[:-1] + [dict(gate("workflow_exit"), rubric=rubric)]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("missing required key 'item'" in e for e in report["errors"]))
        self.assertTrue(any("severity 'SEVERE'" in e for e in report["errors"]))
        self.assertTrue(any("rubric[1] is not an object" in e for e in report["errors"]))

    def test_null_rubric_is_schema_error(self):
        # gate-event-schema.json declares rubric as "type": "array", and
        # draft-07 rejects an explicit null for a present property; this
        # script must not bless a state file the schema rejects.
        gates = NORMAL_RUN[:-1] + [dict(gate("workflow_exit"), rubric=None)]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(
            any("rubric must be an array" in e for e in report["errors"])
        )

    def test_gates_not_a_list(self):
        report = validate_gates("nope", "normal")
        self.assertFalse(report["valid"])

    def test_non_string_step_is_error_not_crash(self):
        # An unhashable step value crashed the emitted-set build with
        # TypeError instead of reporting a schema error.
        gates = NORMAL_RUN + [dict(gate("x"), step=["x"])]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("step must be a string" in e for e in report["errors"]))

    def test_schema_declared_types_are_enforced(self):
        # null step / numeric ts / dict findings / numeric rubric item were
        # all blessed as VALID despite the published schema's declarations.
        bad = {
            "step": None,
            "judge": "self-check",
            "result": "pass",
            "ts": 12345,
            "findings": {"oops": 1},
            "rubric": [{"severity": "HIGH", "item": 42, "result": "pass"}],
        }
        report = validate_gates(NORMAL_RUN + [bad], "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("step must be a string" in e for e in report["errors"]))
        self.assertTrue(any("ts must be a string" in e for e in report["errors"]))
        self.assertTrue(any("findings must be an array" in e for e in report["errors"]))
        self.assertTrue(any("item must be a string" in e for e in report["errors"]))

    def test_non_string_finding_items_are_errors(self):
        gates = NORMAL_RUN + [dict(gate("extra"), findings=["ok", 3])]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(
            any("findings[1] is not a string" in e for e in report["errors"])
        )

    def test_null_ts_and_null_findings_are_schema_errors(self):
        # The published schema declares ts as string and findings as array;
        # a present null violates both under draft-07, so the previous
        # `is not None` guards blessed exactly what jsonschema rejects.
        gates = NORMAL_RUN[:-1] + [
            dict(gate("workflow_exit"), ts=None, findings=None)
        ]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertTrue(any("ts must be a string" in e for e in report["errors"]))
        self.assertTrue(
            any("findings must be an array" in e for e in report["errors"])
        )


class TestCompleteness(unittest.TestCase):
    def test_missing_step_reported(self):
        report = validate_gates([gate("phase1_to_phase2")], "normal")
        self.assertFalse(report["valid"])
        self.assertIn("workflow_exit", report["missing_steps"])

    def test_fix_only_mode_expects_fixonly_entry(self):
        gates = [
            gate("fixonly_entry"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        self.assertTrue(validate_gates(gates, "fix-only")["valid"])
        self.assertFalse(validate_gates(NORMAL_RUN, "fix-only")["valid"])

    def test_error_halt_needs_only_workflow_exit(self):
        gates = [gate("workflow_exit", result="fail")]
        self.assertTrue(validate_gates(gates, "error-halt")["valid"])

    def test_no_improvement_mode(self):
        gates = [gate("phase1_to_phase2"), gate("workflow_exit")]
        self.assertTrue(validate_gates(gates, "no-improvement")["valid"])


class TestFailSemantics(unittest.TestCase):
    def test_terminal_fail_is_accepted(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_fail_then_pass_same_step_is_accepted(self):
        gates = [
            gate("handoff_eval_results", result="fail"),
            gate("handoff_eval_results", result="pass"),
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_fail_then_unrelated_progress_warns(self):
        gates = [
            gate("phase1_to_phase2", result="fail"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("continued" in w for w in report["warnings"]))

    def test_error_halt_shape_is_terminal(self):
        # SKILL.md mandates a final workflow_exit event before ANY exit, so
        # an honest error halt is [<step> fail, workflow_exit fail] — the
        # halting fail is never the literal last element. Flagging it
        # invited the executor to fabricate a repair pass to silence the
        # warning, corrupting the failure record being measured.
        gates = [
            gate("phase1_to_phase2", result="fail"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "error-halt")
        self.assertEqual(report["warnings"], [])

    def test_regression_auto_revert_shape_is_terminal(self):
        # Regression auto-revert: phase3_exit fail followed by the mandated
        # workflow_exit pass. The fail is terminal, not "run continued".
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(report["warnings"], [])


class TestModeDerivation(unittest.TestCase):
    """The expected-event mode comes from steps{}, not the caller's flag."""

    def _run(self, state: dict, *extra: str):
        import json as _json
        import subprocess
        import sys as _sys
        import tempfile
        from pathlib import Path

        script = str(Path(__file__).parent / "validate_gates.py")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            _json.dump(state, handle)
            tmp_path = handle.name
        return subprocess.run(
            [_sys.executable, script, tmp_path, "--json", *extra],
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _steps(phase1: str, phase23: str) -> dict:
        from hone_common import PHASE1_STEPS, PHASE23_STEPS

        return {
            **{step: phase1 for step in PHASE1_STEPS},
            **{step: phase23 for step in PHASE23_STEPS},
        }

    def test_fix_only_mode_derived_from_steps(self):
        import json as _json

        state = {
            "steps": self._steps("skipped", "done"),
            "gates": [
                gate("fixonly_entry"),
                gate("phase2_to_phase3"),
                gate("phase3_exit"),
                gate("convergence"),
                gate("workflow_exit"),
            ],
        }
        result = self._run(state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_json.loads(result.stdout)["mode"], "fix-only")

    def test_no_improvement_mode_derived_from_steps(self):
        import json as _json

        state = {
            "steps": self._steps("done", "skipped"),
            "gates": [gate("phase1_to_phase2"), gate("workflow_exit")],
        }
        result = self._run(state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_json.loads(result.stdout)["mode"], "no-improvement")

    def test_error_halt_mode_derived_from_in_flight_steps(self):
        import json as _json

        steps = dict(self._steps("done", "done"), phase2_improve="in_progress")
        state = {
            "steps": steps,
            "gates": [
                gate("phase1_to_phase2"),
                gate("workflow_exit", result="fail"),
            ],
        }
        result = self._run(state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_json.loads(result.stdout)["mode"], "error-halt")

    def test_incomplete_normal_run_cannot_claim_error_halt(self):
        # Round-3 exploit: steps{} shows a completed normal run but gates[]
        # lacks phase2_to_phase3/phase3_exit; a caller-supplied
        # --mode error-halt blessed it. Derivation closes the hole.
        import json as _json

        state = {
            "steps": self._steps("done", "done"),
            "gates": [gate("phase1_to_phase2"), gate("workflow_exit")],
        }
        result = self._run(state)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        report = _json.loads(result.stdout)
        self.assertEqual(report["mode"], "normal")
        self.assertIn("phase2_to_phase3", report["missing_steps"])

    def test_explicit_mode_override_honored_with_mismatch_warning(self):
        import json as _json

        state = {
            "steps": self._steps("done", "done"),
            "gates": [gate("workflow_exit")],
        }
        result = self._run(state, "--mode", "error-halt")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = _json.loads(result.stdout)
        self.assertEqual(report["mode"], "error-halt")
        self.assertTrue(any("contradicts" in w for w in report["warnings"]))

    def test_missing_steps_map_defaults_to_normal(self):
        import json as _json

        state = {"gates": NORMAL_RUN}
        result = self._run(state)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_json.loads(result.stdout)["mode"], "normal")


class TestCliStateFileGuards(unittest.TestCase):
    def _run(self, content: str):
        import subprocess
        import sys as _sys
        import tempfile
        from pathlib import Path

        script = str(Path(__file__).parent / "validate_gates.py")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            handle.write(content)
            tmp_path = handle.name
        return subprocess.run(
            [_sys.executable, script, tmp_path, "--json"],
            capture_output=True,
            text=True,
        )

    def test_null_root_is_usage_error_not_traceback(self):
        # A null/array root (truncation, bad repair) crashed with
        # AttributeError and exit 1, masquerading as "gates invalid".
        result = self._run("null")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("must be a JSON object", result.stderr)

    def test_array_root_is_usage_error(self):
        result = self._run("[]")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class TestConvergenceIsRequired(unittest.TestCase):
    """SKILL.md's gate table marks `convergence` mandatory; so does this script."""

    def test_normal_run_without_convergence_is_invalid(self):
        gates = [g for g in NORMAL_RUN if g["step"] != "convergence"]
        report = validate_gates(gates, "normal")
        self.assertFalse(report["valid"])
        self.assertIn("convergence", report["missing_steps"])

    def test_fix_only_run_without_convergence_is_invalid(self):
        gates = [
            gate("fixonly_entry"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("workflow_exit"),
        ]
        self.assertIn("convergence", validate_gates(gates, "fix-only")["missing_steps"])

    def test_phase_3_never_ran_so_convergence_is_not_expected(self):
        gates = [gate("phase1_to_phase2"), gate("workflow_exit")]
        self.assertTrue(validate_gates(gates, "no-improvement")["valid"])
        self.assertTrue(
            validate_gates([gate("workflow_exit", result="fail")], "error-halt")["valid"]
        )


class TestScopeVerifyIsConditional(unittest.TestCase):
    """`scope_verify` is mandatory exactly when the run applied edits."""

    def test_required_when_edits_were_applied(self):
        report = validate_gates(NORMAL_RUN, "normal", edits_applied=True)
        self.assertFalse(report["valid"])
        self.assertIn("scope_verify", report["missing_steps"])

    def test_an_error_halt_is_not_failed_for_the_missing_event(self):
        """Phase 2 writes edit_count at Step 6; scope_verify lands at Step 6a.

        A crash between the two is a legitimate halt, and reporting it as a
        missing required event turned the halt itself into a gate violation.
        SKILL.md's resume note says the same: in error-halt the only required
        event is the workflow_exit.
        """
        report = validate_gates(
            [gate("workflow_exit", result="fail")], "error-halt", edits_applied=True
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["missing_steps"], [])

    def test_an_error_halt_still_warns_about_the_unverified_edits(self):
        report = validate_gates(
            [gate("workflow_exit", result="fail")], "error-halt", edits_applied=True
        )
        self.assertTrue(any("scope_verify" in w for w in report["warnings"]))

    def test_an_error_halt_that_verified_scope_does_not_warn(self):
        gates = [gate("scope_verify"), gate("workflow_exit", result="fail")]
        report = validate_gates(gates, "error-halt", edits_applied=True)
        self.assertEqual(report["warnings"], [])

    def test_satisfied_by_the_event(self):
        gates = NORMAL_RUN + [gate("scope_verify")]
        self.assertTrue(validate_gates(gates, "normal", edits_applied=True)["valid"])

    def test_not_required_when_no_edits_were_applied(self):
        self.assertTrue(validate_gates(NORMAL_RUN, "normal")["valid"])


class TestDeriveEditsApplied(unittest.TestCase):
    """The condition comes from the state file, never from a caller flag."""

    def test_positive_edit_count(self):
        self.assertTrue(derive_edits_applied({"applied_edits": {"edit_count": 2}}))

    def test_zero_edit_count(self):
        self.assertFalse(derive_edits_applied({"applied_edits": {"edit_count": 0}}))

    def test_absent_or_malformed(self):
        for state in ({}, {"applied_edits": None}, {"applied_edits": {}},
                      {"applied_edits": {"edit_count": "2"}},
                      {"applied_edits": {"edit_count": True}}, None, []):
            self.assertFalse(derive_edits_applied(state), state)


class TestHaltSequenceTail(unittest.TestCase):
    """A fail followed only by workflow_exit is a halt, not progress."""

    def test_exit_after_the_fail_does_not_warn(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
            gate("convergence"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_an_unemitted_step_in_the_tail_still_warns(self):
        """Regression: a step Phase 3 never reached must not excuse the failure.

        `convergence` is emitted, but only by Phase 3. A failed
        `phase2_to_phase3` means Phase 3 never ran, so a `convergence` event
        after it is invented. It was in HALT_SEQUENCE_STEPS anyway, so
        appending one silenced this warning for any failed gate.
        """
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3", result="fail"),
            gate("convergence", result="fail"),
            gate("workflow_exit", result="pass"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("failed but the run continued" in w
                    for w in report["warnings"]))

    def test_the_auto_revert_halt_in_emission_order_does_not_warn(self):
        """Regression: the documented auto-revert halt warned.

        Phase 3 emits `phase3_exit` (step 6) before `convergence` (step 7), so
        a regression auto-revert halt has `convergence` in the tail behind the
        failed `phase3_exit`. Every other fixture in this file used to list
        the two the other way round, which is why nothing caught it: under the
        real order `is_halt_tail` rejected the tail, `validate_gates` warned on
        a correct halt, and `score_gate_compliance` scored it non-compliant.
        `convergence` is mandatory, so the run cannot drop it to get its halt
        shape back.
        """
        for convergence_result in ("pass", "fail"):
            with self.subTest(convergence=convergence_result):
                gates = [
                    gate("phase1_to_phase2"),
                    gate("phase2_to_phase3"),
                    gate("phase3_exit", result="fail"),
                    gate("convergence", result=convergence_result,
                         reason="escalate" if convergence_result == "fail"
                         else None),
                    gate("workflow_exit", result="pass"),
                ]
                report = validate_gates(gates, "normal")
                self.assertTrue(report["valid"])
                self.assertEqual(report["warnings"], [])


class TestResumedRuns(unittest.TestCase):
    """A resumed run must record that it resumed (regression: TC-011).

    The compaction-resume path was the one documented recovery path with no
    gate event, so a correct resume left no trace and scored 0.0 on
    gate_compliance.
    """

    def _normal(self):
        return [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit"),
        ]

    def test_resumed_without_resume_event_is_invalid(self):
        report = validate_gates(self._normal(), "normal", resumed=True)
        self.assertFalse(report["valid"])
        self.assertIn("resume", report["missing_steps"])

    def test_resumed_with_resume_event_is_valid(self):
        report = validate_gates([gate("resume")] + self._normal(), "normal", resumed=True)
        self.assertTrue(report["valid"])

    def test_resume_not_required_when_not_resumed(self):
        self.assertTrue(validate_gates(self._normal(), "normal", resumed=False)["valid"])

    def test_resumed_is_orthogonal_to_mode(self):
        gates = [gate("resume"), gate("fixonly_entry"), gate("phase2_to_phase3"),
                 gate("phase3_exit"),
                 gate("convergence"), gate("workflow_exit")]
        self.assertTrue(validate_gates(gates, "fix-only", resumed=True)["valid"])


class TestDocumentedCleanRunValidates(unittest.TestCase):
    """A run that emits every documented gate event must pass its own exit gate.

    `convergence` and `scope_verify` were added to REQUIRED_STEPS without the
    instructions that emit them on the clean path, so every compliant run
    failed here. These pin the two halves together: if the requirement moves,
    the emission instructions have to move with it.
    """

    STEPS = {
        "phase1_structural_audit": "done",
        "phase1_criteria_audit": "done",
        "phase1_evaluate": "done",
        "phase1_spec_artifacts": "done",
        "phase1_reference_validation": "done",
        "phase2_fresh_eyes": "done",
        "phase2_trigger_test": "done",
        "phase2_improve": "done",
        "phase3_reevaluate": "done",
    }

    @staticmethod
    def _gate(step, result="pass"):
        return {"step": step, "judge": "self-check", "result": result, "ts": "t"}

    def _state(self, **overrides):
        state = {
            "steps": dict(self.STEPS),
            "applied_edits": {"edit_count": 3},
            "gates": [
                self._gate("phase1_to_phase2"),
                # SKILL.md's template and its gate table both put scope_verify
                # here, in front of the phase transition: Step 6a runs before
                # Phase 3 is entered. The order matters beyond bookkeeping,
                # because PAST_PHASE2 treats a phase2_to_phase3 beside an
                # exemption claim as proof Step 6a was already behind it.
                self._gate("scope_verify"),
                self._gate("phase2_to_phase3"),
                self._gate("phase3_exit"),
                self._gate("convergence"),
                self._gate("workflow_exit"),
            ],
        }
        state.update(overrides)
        return state

    def _report(self, state):
        from validate_gates import (
            derive_edits_applied,
            derive_resumed,
            validate_gates,
        )
        from hone_common import derive_gate_mode

        return validate_gates(
            state.get("gates", []),
            derive_gate_mode(state.get("steps")) or "normal",
            derive_resumed(state),
            derive_edits_applied(state),
        )

    def test_a_fully_documented_normal_run_is_valid(self):
        report = self._report(self._state())
        self.assertTrue(report["valid"], report["errors"])

    def test_omitting_convergence_is_still_caught(self):
        state = self._state()
        state["gates"] = [g for g in state["gates"] if g["step"] != "convergence"]
        self.assertIn("convergence", self._report(state)["missing_steps"])

    def test_omitting_scope_verify_is_still_caught_when_edits_applied(self):
        state = self._state()
        state["gates"] = [g for g in state["gates"] if g["step"] != "scope_verify"]
        self.assertIn("scope_verify", self._report(state)["missing_steps"])


class TestResumedIsDerivedFromState(unittest.TestCase):
    """`resumed` must come off the state file, not a flag the exit gate omits."""

    def test_derive_resumed_reads_the_state_field(self):
        from validate_gates import derive_resumed

        self.assertTrue(derive_resumed({"resumed": True}))
        self.assertFalse(derive_resumed({"resumed": False}))
        self.assertFalse(derive_resumed({}))
        self.assertFalse(derive_resumed("not a state file"))
        # Only a real boolean true counts; a truthy string is not a resume.
        self.assertFalse(derive_resumed({"resumed": "yes"}))

    def test_a_resumed_run_missing_its_resume_event_fails_with_no_flag(self):
        base = TestDocumentedCleanRunValidates()
        state = base._state(resumed=True)
        report = base._report(state)
        self.assertFalse(report["valid"])
        self.assertIn("resume", report["missing_steps"])

    def test_a_resumed_run_that_recorded_the_event_is_valid(self):
        base = TestDocumentedCleanRunValidates()
        state = base._state(resumed=True)
        state["gates"].append(base._gate("resume"))
        self.assertTrue(base._report(state)["valid"])


class TestHaltTailMatchesTheScorer(unittest.TestCase):
    """validate_gates and score_gate_compliance read one halt shape.

    The comment above the fail-semantics loop claimed they already did; they
    did not. `terminal` was `all(step in HALT_SEQUENCE_STEPS)`, satisfied by a
    tail of `convergence` alone, while the scorer also required a
    `workflow_exit`.
    """

    def test_tail_without_workflow_exit_warns(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("failed but the run continued" in w
                    for w in report["warnings"]))

    def test_forward_progress_after_an_unrelated_fail_warns(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("handoff_phase2_apply", result="fail"),
            gate("phase2_to_phase3", result="pass"),
            gate("workflow_exit", result="pass"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("failed but the run continued" in w
                    for w in report["warnings"]))

    def test_the_documented_regression_halt_does_not_warn(self):
        """phase3_exit fails on auto-revert; the exit gate follows."""
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
            gate("workflow_exit", result="pass"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(report["warnings"], [])


class TestRepeatedGateAttempts(unittest.TestCase):
    """A gate emitted once per attempt is settled by its next attempt.

    Phase 3's exit-2 branch emits `convergence` with `result: "fail"` and
    `reason: ledger_missing`, rewrites the ledger, re-runs the check, and
    emits a second `convergence`. That second event fails whenever the re-run
    returns `escalate` or `capped`, so a CORRECT repair has no later `pass`
    and, because the halt-tail slice is exclusive of the failing step, is not
    its own halt either. It warned, and `score_gate_compliance` scored it
    exactly as an ignored halt.
    """

    def test_the_exit_2_ledger_repair_does_not_warn(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="ledger_missing"),
            gate("convergence", result="fail", reason="escalate"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(report["warnings"], [])

    def test_a_repair_that_then_passes_does_not_warn(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="ledger_missing"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(report["warnings"], [])

    def test_a_recorded_confirm_extension_does_not_warn(self):
        """capped -> halt -> --confirm grants rounds -> resume -> caps again.

        `workflow_exit` sits BEFORE the `resume`: phase3-reevaluation.md puts
        the human gate outside and after the FORCED halt, and requires the
        exit event on the halt itself. Asking is what happens once the loop
        has stopped, so the halt is on the record before the restart is. The
        halt's exit PASSES: `capped` is a clean stop carrying a bad verdict,
        and the gate table reserves `workflow_exit: fail` for an error halt.
        """
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("workflow_exit"),
            gate("resume"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(report["warnings"], [])

    def test_a_restart_with_no_recorded_halt_warns(self):
        """A `resume` with no exit event in front of it is not a halt."""
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("resume"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("gates[3]", report["warnings"][0])

    def test_an_unrecorded_restart_after_a_halt_still_warns(self):
        """Without the resume event this is a run that ignored the halt."""
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence", result="fail", reason="capped"),
            gate("workflow_exit", result="fail"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("gates[3]", report["warnings"][0])

    def test_a_retried_handoff_that_never_settles_still_warns(self):
        gates = [
            gate("phase1_to_phase2"),
            gate("handoff_interfaces", result="fail"),
            gate("handoff_interfaces", result="fail"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("gates[2]", report["warnings"][0])

    def test_a_second_phase3_exit_after_an_auto_revert_still_warns(self):
        """phase3_exit is not repeatable: its fail orders an immediate halt."""
        gates = [
            gate("phase1_to_phase2"),
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
            gate("phase3_exit", result="fail"),
            gate("convergence"),
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("gates[2]" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()


class TestScopeVerifyCannotBeSwitchedOff(unittest.TestCase):
    """r3-S1: the exemption had two ways in, one with a flag and one without.

    `_expected_steps` documents that its conditions come off the state file
    and "never read off a caller flag that could switch them off". The
    error-halt exemption did exactly that: the effective mode it keyed on
    could be a `--mode error-halt` the caller supplied. And the mode is
    derived from `steps{}`, which the executor writes, so leaving one step at
    `in_progress` reached the same exemption with no flag at all.
    """

    @staticmethod
    def _steps(phase1: str, phase23: str) -> dict:
        from hone_common import PHASE1_STEPS, PHASE23_STEPS

        return {
            **{step: phase1 for step in PHASE1_STEPS},
            **{step: phase23 for step in PHASE23_STEPS},
        }

    def _run(self, state, *extra):
        import json as _json
        import subprocess
        import sys as _sys
        import tempfile
        from pathlib import Path

        script = str(Path(__file__).parent / "validate_gates.py")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as handle:
            _json.dump(state, handle)
            tmp_path = handle.name
        result = subprocess.run(
            [_sys.executable, script, tmp_path, "--json", *extra],
            capture_output=True,
            text=True,
        )
        return result.returncode, _json.loads(result.stdout)

    def _completed_run(self, **overrides):
        state = {
            "steps": self._steps("done", "done"),
            "applied_edits": {"edit_count": 3},
            "gates": list(NORMAL_RUN),
        }
        state.update(overrides)
        return state

    def test_the_mode_flag_cannot_reach_the_exemption(self):
        code, report = self._run(self._completed_run(), "--mode", "error-halt")
        self.assertEqual(code, 1)
        self.assertIn("scope_verify", report["missing_steps"])

    def test_an_in_flight_step_alone_cannot_reach_the_exemption(self):
        """No flag needed for this one: steps{} is executor-written too."""
        state = self._completed_run(
            steps=dict(self._steps("done", "done"), phase2_improve="in_progress")
        )
        code, report = self._run(state)
        self.assertEqual(report["mode"], "error-halt")
        self.assertEqual(code, 1)
        self.assertIn("scope_verify", report["missing_steps"])

    def test_a_real_halt_still_gets_the_exemption_and_the_warning(self):
        state = self._completed_run(
            steps=dict(self._steps("done", "done"), phase2_improve="in_progress"),
            gates=[gate("phase1_to_phase2"), gate("workflow_exit", result="fail")],
        )
        code, report = self._run(state)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["missing_steps"], [])
        self.assertTrue(any("scope_verify" in w for w in report["warnings"]))

    def test_an_underivable_steps_map_cannot_reach_the_exemption(self):
        """No steps{}, no derived mode, and --mode must not fill the gap.

        `derive_gate_mode` returns None for a steps{} that is missing, empty,
        or not a dict. main() used to pass that None straight through, which
        validate_gates reads as "treat `mode` as the derived one" -- so the
        caller's --mode error-halt reached the exemption after all, on any
        state file whose steps{} was absent.
        """
        halt_shape = [
            gate("phase1_to_phase2", result="fail"),
            gate("workflow_exit", result="fail"),
        ]
        for steps in ({}, None, [], "done"):
            state = self._completed_run(gates=list(halt_shape))
            if steps is None:
                state.pop("steps")
            else:
                state["steps"] = steps
            code, report = self._run(state, "--mode", "error-halt")
            self.assertEqual(code, 1, (steps, report))
            self.assertIn("scope_verify", report["missing_steps"])
            self.assertTrue(
                any("--mode does not confer" in e for e in report["errors"]),
                report["errors"],
            )

    def test_a_halt_recorded_past_phase_2_is_not_exempt(self):
        """`phase2_to_phase3` beside the claim says Step 6a was behind it."""
        from validate_gates import scope_verify_exempt

        self.assertFalse(scope_verify_exempt("error-halt", [
            gate("phase2_to_phase3"),
            gate("phase3_exit", result="fail"),
            gate("workflow_exit", result="fail"),
        ]))

    def test_a_halt_with_no_failing_event_is_not_exempt(self):
        from validate_gates import scope_verify_exempt

        self.assertFalse(scope_verify_exempt("error-halt", [gate("workflow_exit")]))

    def test_no_other_mode_is_ever_exempt(self):
        from validate_gates import scope_verify_exempt

        for mode in ("normal", "fix-only", "no-improvement"):
            self.assertFalse(
                scope_verify_exempt(mode, [gate("workflow_exit", result="fail")]),
                mode,
            )


class TestHaltReasonValidation(unittest.TestCase):
    """`reason` is validated because a settlement predicate keys on it.

    Before this, `reason` was validated nowhere -- not by this script, not by
    the published schema. That is survivable while nothing reads the field and
    is not survivable once a declaration buys an outcome: an executor writing
    an arbitrary string, or the wrong type, or nothing at all, must never do
    better than one telling the truth.
    """

    HALT = [
        gate("phase1_to_phase2"),
        gate("phase2_to_phase3"),
        gate("phase3_exit"),
    ]

    def _report(self, convergence_gate):
        gates = self.HALT + [convergence_gate, gate("workflow_exit")]
        return validate_gates(gates, "normal")

    def test_a_declared_verdict_is_accepted(self):
        for reason in ("escalate", "capped"):
            with self.subTest(reason=reason):
                report = self._report(
                    gate("convergence", result="fail", reason=reason))
                self.assertTrue(report["valid"])
                self.assertEqual(
                    [w for w in report["warnings"] if "reason" in w], [])

    def test_an_undeclared_halt_warns_and_names_the_vocabulary(self):
        """A warning, not an error, and only for migration.

        State files written before the field existed carry no `reason` and
        stay valid. What they lose is settlements, not validity: an
        undeclared halt forfeits both the restart and the in-place retry.
        """
        report = self._report(gate("convergence", result="fail"))
        self.assertTrue(report["valid"])
        matched = [w for w in report["warnings"]
                   if "without declaring a reason" in w]
        self.assertEqual(len(matched), 1)
        for word in ("capped", "escalate", "ledger_missing"):
            self.assertIn(word, matched[0])

    def test_an_invented_reason_is_an_error(self):
        """Junk is never better than silence, and here it is worse.

        At the predicate the two are identical -- both resolve to `None` and
        lose both settlements. This file is what separates them, and it
        separates them in the direction a typo should go.
        """
        report = self._report(
            gate("convergence", result="fail", reason="looks_fine_to_me"))
        self.assertFalse(report["valid"])
        self.assertTrue(any("is not one of" in e for e in report["errors"]))

    def test_a_legacy_free_form_reason_is_a_documented_breaking_change(self):
        """A state file that validated before this enum existed now errors.

        `reason` was in no `properties` block and was read by nothing, so a
        free-form value on a failing `convergence` was legal. It is now an
        error, which exits this script non-zero at the mechanical exit gate.
        That is deliberate, not a regression to soften into a warning: the
        state file is a per-run artifact at /tmp/workflow-${RUN_ID}.json
        written and validated inside the same run, so there is no corpus to
        migrate; the only `reason` the docs ever mandated on this step was
        the literal `ledger_missing`, which is in the vocabulary; and nothing
        in the state file records a schema version, so a warning added "for a
        migration window" would have no way to close and would downgrade
        typos along with legacy values.

        An ABSENT reason stays a warning, because absence is what every
        pre-`reason` file actually contains. Neither buys anything at the
        predicate: both resolve to `None` and lose both settlements.
        """
        legacy = self._report(
            gate("convergence", result="fail",
                 reason="escalate: f1,f2 open 4 rounds"))
        self.assertFalse(legacy["valid"])
        self.assertTrue(any("is not one of" in e for e in legacy["errors"]))

        absent = self._report(gate("convergence", result="fail"))
        self.assertTrue(absent["valid"])
        self.assertTrue(any("without declaring a reason" in w
                            for w in absent["warnings"]))

    def test_a_near_miss_is_an_error_rather_than_a_silent_pass(self):
        """Matching is exact, so a typo is reported instead of normalized."""
        for reason in ("Capped", " capped ", "CAPPED", "in_progress"):
            with self.subTest(reason=reason):
                report = self._report(
                    gate("convergence", result="fail", reason=reason))
                self.assertFalse(report["valid"])

    def test_a_non_string_reason_is_a_schema_error(self):
        """The schema declares `reason` a string for every event."""
        for value in (7, True, ["capped"], {"reason": "capped"}, None):
            with self.subTest(value=repr(value)):
                event = gate("convergence", result="fail")
                event["reason"] = value
                report = validate_gates(
                    self.HALT + [event, gate("workflow_exit")], "normal")
                self.assertFalse(report["valid"])
                self.assertTrue(
                    any("reason must be a string" in e
                        for e in report["errors"]))

    def test_a_passing_convergence_needs_no_declaration(self):
        """Only a `fail` is ambiguous; `converged` and `in_progress` both pass."""
        report = self._report(gate("convergence"))
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_reason_stays_free_form_where_nothing_reads_it(self):
        """SKILL.md's own examples must keep validating.

        `corrupt_state_file` on a failing `workflow_exit` and "prior
        evaluation reused" on a `fixonly_entry` are documented values that no
        predicate reads, so closing the enum over every event would have
        invalidated the skill's own text.
        """
        gates = [
            gate("fixonly_entry", reason="prior evaluation reused"),
            gate("phase2_to_phase3"),
            gate("phase3_exit"),
            gate("convergence"),
            gate("workflow_exit", result="fail",
                 reason="corrupt_state_file"),
        ]
        report = validate_gates(gates, "fix-only")
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_the_declaration_reaches_the_fail_semantics_check(self):
        """The validator and the predicate read the same event.

        Identical sequences, opposite verdicts, and the only difference is the
        word on the failing event -- which is the whole change.
        """
        def run(reason):
            gates = self.HALT + [
                gate("convergence", result="fail", reason=reason),
                gate("workflow_exit"),
                gate("resume"),
                gate("phase2_to_phase3"),
                gate("phase3_exit"),
                gate("convergence"),
                gate("workflow_exit"),
            ]
            return validate_gates(gates, "normal", resumed=True)

        capped = run("capped")
        self.assertEqual(
            [w for w in capped["warnings"] if "the run continued" in w], [])
        escalate = run("escalate")
        self.assertTrue(
            any("the run continued" in w for w in escalate["warnings"]))
