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


def round_entry(number, findings, run=None):
    """A round with an explicit number, and a `run` id when the test needs one."""
    entry = {"round": number, "findings": findings}
    if run is not None:
        entry["run"] = run
    return entry


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
        self.assertTrue(any("did not move" in r for r in report["reasons"]))

    def test_a_rising_blocking_count_is_not_a_stall(self):
        """[0, 0, 1]: a newly found finding, not a loop that stopped moving.

        `window[-1] >= max(window)` escalated on a blocking finding's first
        appearance, before the loop had one round to fix it.
        """
        rounds = [[], [], [finding("a")]]
        report = analyze(ledger(rounds, max_rounds=9), 99, 3)
        self.assertEqual(report["verdict"], "in_progress")
        self.assertEqual(report["reasons"], [])

    def test_a_fixed_streak_no_longer_escalates(self):
        """A finding open three rounds and then fixed is healed, not stuck.

        `reasons` is built from the whole history and was consulted before the
        "nothing open -> converged" branch, so one 3-round streak made every
        later round return `escalate` with an empty `open_blocking` -- a run
        that halted on a convergence gate it had already cleared.
        """
        rounds = [[finding("f1")]] * 3 + [[finding("f1", status="fixed")]] * 2
        report = analyze(ledger(rounds, max_rounds=9), 3, 9)
        self.assertEqual(report["verdict"], "converged")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["recurring"], [])

    def test_a_still_open_streak_still_escalates(self):
        rounds = [[finding("f1")]] * 3
        report = analyze(ledger(rounds, max_rounds=9), 3, 9)
        self.assertEqual(report["verdict"], "escalate")
        self.assertIn("f1", report["recurring"])

    def test_a_repaired_relocation_no_longer_escalates(self):
        rounds = [
            [finding("f1", status="fixed", file="a.py", summary="same shape")],
            [finding("f2", status="open", file="b.py", summary="same shape")],
        ]
        self.assertEqual(
            analyze(ledger(rounds, max_rounds=9), 9, 9)["verdict"], "escalate"
        )
        rounds.append([finding("f2", status="fixed", file="b.py", summary="same shape")])
        report = analyze(ledger(rounds, max_rounds=9), 9, 9)
        self.assertEqual(report["verdict"], "converged")
        self.assertEqual(report["relocations"], [])

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


class TestRelocationDeduplication(unittest.TestCase):
    """One relocation still open across N rounds is one relocation."""

    def test_a_relocation_open_for_three_rounds_is_counted_once(self):
        rounds = [
            [finding("f1", file="SKILL.md", summary="gate order wrong")],
            [finding("f1", status="fixed", file="SKILL.md",
                     summary="gate order wrong")],
            [finding("f2", file="ref.md", summary="gate order wrong")],
            [finding("f2", file="ref.md", summary="gate order wrong")],
            [finding("f2", file="ref.md", summary="gate order wrong")],
        ]
        report = analyze(ledger(rounds, max_rounds=9), 99, 99)
        self.assertEqual(len(report["relocations"]), 1)
        self.assertIn("1 finding(s) closed in one file reopened in another",
                      report["reasons"])

    def test_two_distinct_relocations_are_both_reported(self):
        rounds = [
            [finding("f1", file="a.md", summary="alpha"),
             finding("g1", file="c.md", summary="beta")],
            [finding("f1", status="fixed", file="a.md", summary="alpha"),
             finding("g1", status="fixed", file="c.md", summary="beta")],
            [finding("f2", file="b.md", summary="alpha"),
             finding("g2", file="d.md", summary="beta")],
        ]
        report = analyze(ledger(rounds, max_rounds=9), 99, 99)
        self.assertEqual(len(report["relocations"]), 2)


class TestLedgerTypeTolerance(unittest.TestCase):
    """A stringified number in the ledger is exit 2 territory, not a traceback.

    Everything else in this module tolerates a wrong type; `round` and
    `max_rounds` reached a comparison and raised TypeError out of analyze().
    """

    def test_string_round_numbers_sort_without_raising(self):
        report = analyze(
            {"max_rounds": 5, "rounds": [
                {"round": "2", "findings": [finding("f1")]},
                {"round": "1", "findings": []},
            ]}, 3, 3)
        self.assertEqual(report["rounds_run"], 2)
        self.assertEqual(report["verdict"], "in_progress")

    def test_string_max_rounds_still_caps(self):
        report = analyze(
            {"max_rounds": "2", "rounds": [
                {"round": 1, "findings": [finding("f1")]},
                {"round": 2, "findings": [finding("f1")]},
            ]}, 99, 99)
        self.assertEqual(report["max_rounds"], 2)
        self.assertEqual(report["verdict"], "capped")

    def test_unparseable_max_rounds_is_treated_as_absent(self):
        report = analyze(
            {"max_rounds": "soon", "rounds": [
                {"round": 1, "findings": [finding("f1")]},
            ]}, 99, 99)
        self.assertIsNone(report["max_rounds"])
        self.assertEqual(report["verdict"], "in_progress")


class TestLedgerListTolerance(unittest.TestCase):
    """A scalar where an array belongs must not raise out of analyze()."""

    def test_scalar_rounds_does_not_raise(self):
        from check_convergence import analyze

        for bad in ({"rounds": 3}, {"rounds": True}, {"rounds": "1"}):
            with self.subTest(bad=bad):
                self.assertEqual(analyze(bad, 3, 3)["rounds_run"], 0)

    def test_scalar_findings_does_not_raise(self):
        from check_convergence import analyze

        report = analyze({"rounds": [{"round": 1, "findings": 7}]}, 3, 3)
        self.assertEqual(report["rounds_run"], 1)
        self.assertEqual(report["open_blocking"], [])

    def test_one_malformed_round_does_not_sink_the_others(self):
        from check_convergence import analyze

        ledger = {
            "max_rounds": 3,
            "rounds": [
                {"round": 1, "findings": 7},
                {"round": 2, "findings": [
                    {"id": "F1", "status": "open", "severity": "critical",
                     "summary": "real", "file": "a.py"}]},
            ],
        }
        report = analyze(ledger, 3, 3)
        self.assertEqual([f["id"] for f in report["open_blocking"]], ["F1"])

    def test_cli_rejects_scalar_rounds_with_exit_2(self):
        import json as _json
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(_json.dumps({"max_rounds": 3, "rounds": 3}))
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "check_convergence.py"),
                 str(path), "--json"],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a JSON array", result.stderr)


class TestReopenCountsVisibleBelowTheBar(unittest.TestCase):
    """Reaching the reopen bar needs 5 rounds; the count must show before that."""

    @staticmethod
    def _round(number, status):
        return {"round": number, "findings": [
            {"id": "F1", "status": status, "severity": "critical",
             "summary": "recurring thing", "file": "a.py"}]}

    def test_single_reopen_is_reported_but_does_not_escalate(self):
        from check_convergence import analyze

        report = analyze({"max_rounds": 3, "rounds": [
            self._round(1, "open"), self._round(2, "fixed"),
            self._round(3, "open")]}, 3, 3)
        self.assertEqual(report["reopen_counts"], {"F1": 1})
        self.assertEqual(report["reopened"], [])
        self.assertNotIn("found open again", " ".join(report["reasons"]))

    def test_two_reopens_escalate(self):
        from check_convergence import analyze

        report = analyze({"max_rounds": 3, "rounds": [
            self._round(1, "open"), self._round(2, "fixed"),
            self._round(3, "open"), self._round(4, "fixed"),
            self._round(5, "open")]}, 3, 3)
        self.assertEqual(report["reopen_counts"], {"F1": 2})
        self.assertEqual(report["reopened"], ["F1"])
        self.assertEqual(report["verdict"], "escalate")

    def test_empty_ledger_carries_the_key(self):
        from check_convergence import analyze

        self.assertEqual(analyze({"rounds": []}, 3, 3)["reopen_counts"], {})


class TestCumulativeLedgerHoldsSeveralRuns(unittest.TestCase):
    """The ledger is per artifact and permanent; `round` restarts per run.

    Reading the cumulative array as one monotonic run produced a false
    `converged` (sorting interleaved the runs) and a false `capped`
    (`len(rounds)` measured against a per-run `max_rounds`).
    """

    def test_a_second_runs_open_finding_is_not_sorted_away(self):
        """Run 1's rounds 1-3 then run 2's rounds 1-2 sorted to [1,1,2,2,3]."""
        report = analyze({"artifact": "x", "max_rounds": 3, "rounds": [
            round_entry(1, [finding("A")]),
            round_entry(2, [finding("A")]),
            round_entry(3, [finding("A", status="fixed")]),
            round_entry(1, [finding("B")]),
            round_entry(2, [finding("B")]),
        ]}, 3, 3)
        self.assertNotEqual(report["verdict"], "converged")
        self.assertEqual([f["id"] for f in report["open_blocking"]], ["B"])
        self.assertEqual(report["runs_logged"], 2)

    def test_max_rounds_is_measured_against_the_current_run(self):
        """A 4-round ledger must not cap run 2 on its first round."""
        report = analyze({"artifact": "x", "max_rounds": 3, "rounds": [
            round_entry(1, [finding("A", status="fixed")]),
            round_entry(2, [finding("A", status="fixed")]),
            round_entry(3, [finding("A", status="fixed")]),
            round_entry(1, [finding("C")]),
        ]}, 9, 9)
        self.assertEqual(report["verdict"], "in_progress")
        self.assertEqual(report["rounds_run"], 1)
        self.assertEqual(report["total_rounds_logged"], 4)

    def test_an_explicit_run_id_is_authoritative(self):
        """The `run` id beats the repeated-number inference."""
        report = analyze({"artifact": "x", "max_rounds": 3, "rounds": [
            round_entry(1, [finding("A", status="fixed")], run="r1"),
            round_entry(2, [finding("A", status="fixed")], run="r1"),
            round_entry(3, [finding("A", status="fixed")], run="r1"),
            round_entry(4, [finding("C")], run="r2"),
        ]}, 9, 9)
        self.assertEqual(report["runs_logged"], 2)
        self.assertEqual(report["rounds_run"], 1)
        self.assertEqual(report["verdict"], "in_progress")

    def test_an_out_of_order_append_is_one_run_not_two(self):
        """[round 2, round 1] needs sorting, not splitting.

        A "the number went down" boundary rule would read this as two runs and
        lose a round; a repeated number does not fire here.
        """
        report = analyze({"max_rounds": 5, "rounds": [
            {"round": "2", "findings": [finding("f1")]},
            {"round": "1", "findings": []},
        ]}, 3, 3)
        self.assertEqual(report["runs_logged"], 1)
        self.assertEqual(report["rounds_run"], 2)
        self.assertEqual(report["verdict"], "in_progress")

    def test_a_single_run_still_reports_one_run(self):
        report = analyze(ledger([[finding("f1")], []]), 3, 3)
        self.assertEqual(report["runs_logged"], 1)
        self.assertEqual(report["rounds_run"], 2)
        self.assertEqual(report["total_rounds_logged"], 2)


class TestReopenCounterExpires(unittest.TestCase):
    """The reopen bar is cross-run by design; permanence was not.

    One historical alternation escalated every future invocation for that
    artifact, with `open_blocking` empty and no recovery short of deleting the
    ledger.
    """

    @staticmethod
    def _rounds(statuses):
        return [{"round": i + 1, "findings": [
            {"id": "F1", "status": s, "severity": "critical",
             "summary": "recurring thing", "file": "a.py"}]}
            for i, s in enumerate(statuses)]

    def test_a_recent_alternation_still_escalates(self):
        statuses = ["open", "fixed", "open", "fixed", "open", "fixed",
                    "fixed", "fixed"]
        report = analyze({"max_rounds": 9, "rounds": self._rounds(statuses)}, 3, 9)
        self.assertEqual(report["verdict"], "escalate")
        self.assertEqual(report["reopened"], ["F1"])

    def test_an_aged_out_alternation_no_longer_escalates(self):
        statuses = (["open", "fixed", "open", "fixed", "open"]
                    + ["fixed"] * 8)
        report = analyze({"max_rounds": 20, "rounds": self._rounds(statuses)}, 3, 20)
        self.assertEqual(report["verdict"], "converged")
        self.assertEqual(report["reopened"], [])

    def test_the_window_is_configurable(self):
        statuses = ["open", "fixed", "open", "fixed", "open", "fixed"]
        rounds = {"max_rounds": 9, "rounds": self._rounds(statuses)}
        self.assertEqual(analyze(rounds, 3, 9)["reopened"], ["F1"])
        self.assertEqual(analyze(rounds, 3, 9, reopen_window=2)["reopened"], [])


class TestEscalationReasonsAreSeverityFiltered(unittest.TestCase):
    """`reasons` short-circuits to `escalate` ahead of the converged branch.

    Unfiltered, a single known nit left open halted a run that had fixed
    everything blocking -- against the module docstring and
    test_minor_findings_do_not_block_convergence.
    """

    def test_a_minor_finding_open_three_rounds_does_not_escalate(self):
        rounds = [[finding("m1", severity="minor")]] * 3
        report = analyze(ledger(rounds, max_rounds=9), 3, 9)
        self.assertEqual(report["verdict"], "converged")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["open_blocking"], [])

    def test_a_minor_relocation_does_not_escalate(self):
        rounds = [
            [finding("m1", severity="minor", status="fixed", file="a.md",
                     summary="same shape")],
            [finding("m2", severity="minor", file="b.md", summary="same shape")],
        ]
        report = analyze(ledger(rounds, max_rounds=9), 9, 9)
        self.assertEqual(report["relocations"], [])
        self.assertEqual(report["verdict"], "converged")

    def test_a_minor_alternation_does_not_escalate(self):
        statuses = ["open", "fixed", "open", "fixed", "open"]
        rounds = [[finding("m1", severity="minor", status=s)] for s in statuses]
        report = analyze(ledger(rounds, max_rounds=9), 3, 9)
        self.assertEqual(report["reopened"], [])
        self.assertEqual(report["verdict"], "converged")

    def test_a_blocking_finding_still_escalates(self):
        report = analyze(ledger([[finding("b1")]] * 3, max_rounds=9), 3, 9)
        self.assertEqual(report["verdict"], "escalate")


class TestRelocationIsOrderIndependent(unittest.TestCase):
    """Detection must not depend on finding order inside a round's array."""

    def test_a_fixed_entry_listed_after_the_reopened_one_is_still_seen(self):
        for order in ("fixed_first", "open_first"):
            fixed = finding("a", status="fixed", file="A.md",
                            summary="same shape")
            reopened = finding("b", file="B.md", summary="same shape")
            second = ([fixed, reopened] if order == "fixed_first"
                      else [reopened, fixed])
            with self.subTest(order=order):
                report = analyze({"max_rounds": 9, "rounds": [
                    {"round": 1, "findings": [
                        finding("a", file="A.md", summary="same shape")]},
                    {"round": 2, "findings": second},
                ]}, 9, 9)
                self.assertEqual(len(report["relocations"]), 1)
                self.assertEqual(report["relocations"][0]["from"], "A.md")
                self.assertEqual(report["relocations"][0]["to"], "B.md")


class TestMissingRoundsIsLoud(unittest.TestCase):
    """Findings at the top level silently disabled the check forever."""

    def _run(self, payload):
        import json as _json
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(_json.dumps(payload))
            return subprocess.run(
                [sys.executable,
                 str(Path(__file__).parent / "check_convergence.py"),
                 str(path), "--json"],
                capture_output=True, text=True,
            )

    def test_a_ledger_with_no_rounds_key_exits_2(self):
        result = self._run({"artifact": "x", "findings": [{"id": "F1"}]})
        self.assertEqual(result.returncode, 2)
        self.assertIn("no 'rounds' array", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_empty_rounds_array_is_still_in_progress(self):
        result = self._run({"artifact": "x", "max_rounds": 3, "rounds": []})
        self.assertEqual(result.returncode, 1)
        self.assertIn("in_progress", result.stdout)


class TestEscalationSignalsAreRunScoped(unittest.TestCase):
    """The ledger is cumulative; the escalation signals are not.

    The streak, the stall window and the relocation trail read the whole
    array and had neither the run scoping `max_rounds` got nor the trailing
    window the reopen counter got, so each halted a NEW run on its first
    round on previous-run history alone -- permanently, since Phase 2 Step 8
    restates still-open findings on every future invocation.
    """

    def _ledger(self, rounds, max_rounds=3):
        return {"artifact": "demo", "max_rounds": max_rounds, "rounds": rounds}

    def test_a_carried_over_finding_does_not_escalate_the_next_first_round(self):
        """Run 1 ends with F1 open; run 2 round 1 restates it, as mandated."""
        report = analyze(self._ledger([
            round_entry(1, [finding("F1")], "run-1"),
            round_entry(2, [finding("F1")], "run-1"),
            round_entry(3, [finding("F1")], "run-1"),
            round_entry(1, [finding("F1")], "run-2"),
        ]), 3, 3)
        self.assertEqual(report["verdict"], "in_progress")
        self.assertEqual(report["rounds_run"], 1)
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["recurring"], [])

    def test_a_streak_inside_one_run_still_escalates(self):
        report = analyze(self._ledger([
            round_entry(1, [finding("F1")], "run-1"),
            round_entry(2, [finding("F1")], "run-1"),
            round_entry(3, [finding("F1")], "run-1"),
        ]), 3, 3)
        self.assertEqual(report["verdict"], "escalate")
        self.assertEqual(report["recurring"], ["F1"])

    def test_the_stall_window_does_not_straddle_a_restart(self):
        """Run 2 round 1 closes both carry-overs and opens two new findings."""
        report = analyze(self._ledger([
            round_entry(1, [finding("A", summary="a"), finding("B", summary="b")],
                        "run-1"),
            round_entry(2, [finding("A", summary="a"), finding("B", summary="b")],
                        "run-1"),
            round_entry(1, [finding("A", status="fixed", summary="a"),
                            finding("B", status="fixed", summary="b"),
                            finding("C", summary="c"), finding("D", summary="d")],
                        "run-2"),
        ]), 3, 3)
        self.assertEqual(report["verdict"], "in_progress")
        self.assertEqual(report["rounds_run"], 1)
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["blocking_counts"], [2])

    def test_a_stall_inside_one_run_still_escalates(self):
        """Distinct ids every round, so only the flat count can fire."""
        rounds = [
            round_entry(n, [finding(f"P{2 * n - 1}", summary=f"issue {2 * n - 1}"),
                            finding(f"P{2 * n}", summary=f"issue {2 * n}")],
                        "run-1")
            for n in (1, 2, 3)
        ]
        report = analyze(self._ledger(rounds, max_rounds=9), 3, 3)
        self.assertEqual(report["verdict"], "escalate")
        self.assertEqual(report["blocking_counts"], [2, 2, 2])
        self.assertTrue(any("did not move" in r for r in report["reasons"]))

    def test_a_relocation_across_a_run_boundary_does_not_escalate(self):
        """Closed in run 1's A.md, opened in run 2's B.md, is a new finding."""
        report = analyze(self._ledger([
            round_entry(1, [finding("X", file="A.md", summary="step lacks exit")],
                        "run-1"),
            round_entry(2, [finding("X", file="A.md", summary="step lacks exit",
                                    status="fixed")], "run-1"),
            round_entry(1, [finding("Y", file="B.md", summary="step lacks exit")],
                        "run-2"),
        ]), 3, 3)
        self.assertEqual(report["verdict"], "in_progress")
        self.assertEqual(report["relocations"], [])
        self.assertEqual(report["reasons"], [])

    def test_the_reopen_counter_still_spans_runs(self):
        """The one signal that is cross-run by design keeps counting."""
        report = analyze(self._ledger([
            round_entry(1, [finding("F1")], "run-1"),
            round_entry(2, [finding("F1", status="fixed")], "run-1"),
            round_entry(3, [finding("F1")], "run-1"),
            round_entry(1, [finding("F1")], "run-2"),
            round_entry(2, [finding("F1", status="fixed")], "run-2"),
            round_entry(3, [finding("F1")], "run-2"),
        ]), 3, 3)
        self.assertEqual(report["verdict"], "escalate")
        self.assertEqual(report["reopen_counts"], {"F1": 2})
        self.assertTrue(
            any("found open again" in r for r in report["reasons"])
        )


if __name__ == "__main__":
    unittest.main()
