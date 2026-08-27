#!/usr/bin/env python3
"""Tests for check_convergence.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from check_convergence import analyze  # noqa: E402


def finding(fid, severity="major", status="open", file="SKILL.md", summary="issue"):
    return {"id": fid, "severity": severity, "status": status,
            "file": file, "summary": summary}


def ledger(rounds, max_rounds=5):
    return {"artifact": "demo", "max_rounds": max_rounds,
            "rounds": [{"round": i + 1, "findings": f} for i, f in enumerate(rounds)]}


class TestConvergence(unittest.TestCase):
    def test_empty_ledger_is_in_progress(self):
        self.assertEqual(analyze({"rounds": []}, 3, 3)["verdict"], "in_progress")

    def test_clean_final_round_converges(self):
        report = analyze(ledger([[finding("f1")], []]), 3, 3)
        self.assertEqual(report["verdict"], "converged")

    def test_minor_findings_do_not_block_convergence(self):
        report = analyze(ledger([[finding("f1", severity="minor")]]), 3, 3)
        self.assertEqual(report["verdict"], "converged")
        self.assertEqual(report["open_minor_count"], 1)

    def test_in_progress_while_rounds_remain(self):
        report = analyze(ledger([[finding("f1")]], max_rounds=5), 3, 3)
        self.assertEqual(report["verdict"], "in_progress")

    def test_capped_is_not_converged(self):
        report = analyze(ledger([[finding("f1")], [finding("f2")]], max_rounds=2), 5, 5)
        self.assertEqual(report["verdict"], "capped")
        self.assertEqual(len(report["open_blocking"]), 1)


class TestEscalation(unittest.TestCase):
    def test_recurring_finding_escalates(self):
        report = analyze(ledger([[finding("f1")]] * 3), 3, 99)
        self.assertEqual(report["verdict"], "escalate")
        self.assertIn("f1", report["recurring"])

    def test_recurrence_streak_resets_when_closed(self):
        rounds = [[finding("f1")], [], [finding("f1")]]
        report = analyze(ledger(rounds), 3, 99)
        self.assertEqual(report["recurring"], [])

    def test_stalled_blocking_count_escalates(self):
        rounds = [
            [finding("a"), finding("b")],
            [finding("c"), finding("d")],
            [finding("e"), finding("f")],
        ]
        report = analyze(ledger(rounds), 99, 3)
        self.assertEqual(report["verdict"], "escalate")
        self.assertTrue(any("did not fall" in r for r in report["reasons"]))

    def test_falling_count_does_not_escalate(self):
        rounds = [
            [finding("a"), finding("b"), finding("c")],
            [finding("d"), finding("e")],
            [finding("f")],
        ]
        report = analyze(ledger(rounds), 99, 3)
        self.assertNotIn("escalate", report["verdict"])
        self.assertEqual(report["blocking_counts"], [3, 2, 1])

    def test_relocated_finding_escalates(self):
        rounds = [
            [finding("a", status="fixed", file="SKILL.md", summary="missing gate event")],
            [finding("b", status="open", file="refs/phase1.md", summary="missing gate event")],
        ]
        report = analyze(ledger(rounds), 99, 99)
        self.assertEqual(report["verdict"], "escalate")
        self.assertEqual(report["relocations"][0]["from"], "SKILL.md")
        self.assertEqual(report["relocations"][0]["to"], "refs/phase1.md")

    def test_same_file_reopen_is_not_relocation(self):
        rounds = [
            [finding("a", status="fixed", file="SKILL.md", summary="missing gate event")],
            [finding("b", status="open", file="SKILL.md", summary="missing gate event")],
        ]
        report = analyze(ledger(rounds), 99, 99)
        self.assertEqual(report["relocations"], [])

    def test_rejected_findings_do_not_count_as_open(self):
        report = analyze(ledger([[finding("f1", status="rejected")]]), 3, 3)
        self.assertEqual(report["verdict"], "converged")


if __name__ == "__main__":
    unittest.main()
