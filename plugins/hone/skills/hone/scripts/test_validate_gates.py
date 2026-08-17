#!/usr/bin/env python3
"""Tests for validate_gates.py."""

from __future__ import annotations

import unittest

from validate_gates import validate_gates


def gate(step, result="pass", judge="self-check"):
    return {"step": step, "judge": judge, "result": result, "ts": "2026-08-16T00:00:00Z"}


NORMAL_RUN = [
    gate("phase1_to_phase2"),
    gate("phase2_to_phase3"),
    gate("phase3_exit"),
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

    def test_unknown_judge_is_warning_not_error(self):
        gates = NORMAL_RUN[:-1] + [gate("workflow_exit", judge="vibes")]
        report = validate_gates(gates, "normal")
        self.assertTrue(report["valid"])
        self.assertTrue(report["warnings"])

    def test_gates_not_a_list(self):
        report = validate_gates("nope", "normal")
        self.assertFalse(report["valid"])


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
            gate("workflow_exit"),
        ]
        report = validate_gates(gates, "normal")
        self.assertTrue(any("continued" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
