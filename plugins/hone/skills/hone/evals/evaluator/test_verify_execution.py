#!/usr/bin/env python3
"""Verifier controls only; these tests do not execute hone or any model."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "aggregate"


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hone-verifier-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        shutil.copytree(FIXTURE, self.workspace)

    def verify(self):
        result = subprocess.run(
            [sys.executable, str(HERE / "verify_execution.py"), str(self.workspace)],
            capture_output=True, text=True, check=False,
        )
        return result.returncode, json.loads(result.stdout)

    def use_reference(self):
        shutil.copyfile(HERE / "reference_aggregate.py", self.workspace / "aggregate.py")

    def test_rejects_wrong_output_despite_successful_script_exit(self):
        status, checks = self.verify()
        self.assertEqual(status, 1)
        failures = checks["actual-output"]["failures"]
        self.assertTrue(failures)
        self.assertTrue(all(failure["exit_code"] == 0 for failure in failures))
        self.assertTrue(checks["protected-bytes"]["passed"])

    def test_accepts_known_correct_reference(self):
        self.use_reference()
        status, checks = self.verify()
        self.assertEqual(status, 0)
        self.assertTrue(all(check["passed"] for check in checks.values()))

    def test_rejects_unrelated_file_change_even_when_output_is_correct(self):
        self.use_reference()
        notes = self.workspace / "unrelated_notes.txt"
        notes.write_bytes(notes.read_bytes() + b"Unrequested change.\n")
        status, checks = self.verify()
        self.assertEqual(status, 1)
        self.assertTrue(checks["actual-output"]["passed"])
        self.assertEqual(checks["protected-bytes"]["changed_or_missing"], [notes.name])


if __name__ == "__main__":
    unittest.main(verbosity=2)
