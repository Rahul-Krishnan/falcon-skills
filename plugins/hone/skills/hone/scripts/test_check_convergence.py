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


class TestLedgerRootShape(unittest.TestCase):
    """A non-object ledger root is a usage error, not an AttributeError."""

    def test_a_list_rooted_ledger_exits_2(self):
        import subprocess
        import sys
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            with open(path, "w") as handle:
                handle.write('[{"round": 1}]')
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(os.path.dirname(__file__), "check_convergence.py"),
                    path,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must be a JSON object", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TestCappedIsReachable(unittest.TestCase):
    """`capped` fires when the budget runs out on a finding opened late.

    A blocking finding first raised in the final round has a one-round streak
    and does not hold the blocking count flat, so no escalation reason fires
    and the rounds-exhausted branch is the one that wins.
    """

    def test_a_finding_new_in_the_last_round_caps_rather_than_escalates(self):
        from check_convergence import analyze

        def finding(fid, summary, status, severity="major", path="a.md"):
            return {
                "id": fid,
                "severity": severity,
                "file": path,
                "summary": summary,
                "status": status,
            }

        ledger = {
            "artifact": "x",
            "max_rounds": 3,
            "rounds": [
                {
                    "round": 1,
                    "findings": [
                        finding("F1", "alpha", "open", "critical"),
                        finding("F2", "beta", "open"),
                        finding("F3", "gamma", "open"),
                    ],
                },
                {
                    "round": 2,
                    "findings": [
                        finding("F1", "alpha", "fixed", "critical"),
                        finding("F2", "beta", "open"),
                        finding("F3", "gamma", "fixed"),
                    ],
                },
                {
                    "round": 3,
                    "findings": [
                        finding("F2", "beta", "fixed"),
                        finding("F4", "delta new", "open", "critical", "b.md"),
                    ],
                },
            ],
        }
        report = analyze(ledger, 3, 3)
        self.assertEqual(report["verdict"], "capped")
        self.assertEqual(report["reasons"], [])
        self.assertEqual([f["id"] for f in report["open_blocking"]], ["F4"])


class TestReopenRecurrence(unittest.TestCase):
    """The docstring's first failure shape: fixed one round, open the next.

    The consecutive-open streak cannot see this -- the round recording the fix
    zeroes it -- so before the reopen counter an oscillating critical finding
    reported `converged` with no reasons.
    """

    def test_alternating_open_and_fixed_escalates(self):
        statuses = ["open", "fixed", "open", "fixed", "open", "fixed"]
        rounds = [[finding("f1", severity="critical", status=s)] for s in statuses]
        report = analyze(ledger(rounds, max_rounds=8), 3, 3)
        self.assertEqual(report["verdict"], "escalate")
        self.assertIn("f1", report["reopened"])
        self.assertIn("f1", report["recurring"])
        self.assertTrue(any("found open again" in r for r in report["reasons"]))

    def test_single_reopen_is_not_yet_recurring(self):
        rounds = [[finding("f1", status=s)] for s in ["open", "fixed", "open"]]
        report = analyze(ledger(rounds), 3, 99)
        self.assertEqual(report["reopened"], [])

    def test_absent_round_is_a_gap_not_a_fix(self):
        """Only an explicit `fixed` record closes a finding.

        A ledger that lists open findings only would otherwise read every
        unreported round as a fix and escalate on the next sighting.
        """
        rounds = [[finding("f1")], [], [finding("f1")], [], [finding("f1")]]
        report = analyze(ledger(rounds, max_rounds=9), 3, 99)
        self.assertEqual(report["reopened"], [])

    def test_reopen_limit_is_configurable(self):
        rounds = [[finding("f1", status=s)] for s in ["open", "fixed", "open"]]
        report = analyze(ledger(rounds), 3, 99, reopen_limit=1)
        self.assertIn("f1", report["reopened"])


class TestJsonContractKeys(unittest.TestCase):
    """The empty-rounds early return is the same dict shape as the normal one."""

    def test_empty_ledger_reports_max_rounds(self):
        report = analyze({"rounds": [], "max_rounds": 5}, 3, 3)
        self.assertEqual(report["max_rounds"], 5)

    def test_empty_and_populated_returns_have_the_same_keys(self):
        empty = analyze({"rounds": [], "max_rounds": 5}, 3, 3)
        populated = analyze(ledger([[finding("f1")]]), 3, 3)
        self.assertEqual(set(empty), set(populated))
