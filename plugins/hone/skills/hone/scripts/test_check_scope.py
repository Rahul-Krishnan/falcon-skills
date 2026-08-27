#!/usr/bin/env python3
"""Tests for check_scope.py."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import check_scope  # noqa: E402
from check_scope import _in_scope, _is_ignored, build_manifest, verify  # noqa: E402


class TestScopeMatching(unittest.TestCase):
    def test_exact_and_nested_match(self):
        self.assertTrue(_in_scope("hone/SKILL.md", ["hone"]))
        self.assertTrue(_in_scope("hone/scripts/a.py", ["hone"]))
        self.assertTrue(_in_scope("hone", ["hone"]))

    def test_sibling_prefix_is_not_in_scope(self):
        # "honeycomb" must not match scope "hone".
        self.assertFalse(_in_scope("honeycomb/SKILL.md", ["hone"]))

    def test_outside_scope(self):
        self.assertFalse(_in_scope("workout/SKILL.md", ["hone"]))


class TestIgnored(unittest.TestCase):
    def test_noise_is_ignored(self):
        self.assertTrue(_is_ignored(Path(".DS_Store")))
        self.assertTrue(_is_ignored(Path("hone/__pycache__/x.pyc")))
        self.assertTrue(_is_ignored(Path("hone/SKILL.md.pre-hone")))

    def test_real_content_is_not_ignored(self):
        self.assertFalse(_is_ignored(Path("hone/SKILL.md")))


class TestPreexistingDirtyTree(unittest.TestCase):
    """A file dirty in git but unchanged since the snapshot is not our doing.

    Regression: verify() used to union the git-status signal with the hash
    manifest, so any pre-existing uncommitted work outside scope produced a
    scope_violation. The documented response to a violation is to revert the
    offending paths — which would have destroyed that unrelated work.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "workout").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("hone v1")
        (self.root / "workout" / "SKILL.md").write_text("workout dirty already")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_git_dirty_but_hash_unchanged_is_not_a_violation(self):
        manifest = build_manifest(self.root)
        # Edit only in-scope; workout/ is untouched by this "run".
        (self.root / "hone" / "SKILL.md").write_text("hone v2")

        real_git = check_scope._git_changed
        # Simulate a repo where workout/ was already uncommitted before we ran.
        check_scope._git_changed = lambda root: ["hone/SKILL.md", "workout/SKILL.md"]
        try:
            report = verify(self.root, manifest, ["hone"])
        finally:
            check_scope._git_changed = real_git

        self.assertEqual(report["violations"], [])
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("workout/SKILL.md", report["preexisting_dirty_out_of_scope"])

    def test_real_out_of_scope_edit_still_violates(self):
        manifest = build_manifest(self.root)
        (self.root / "workout" / "SKILL.md").write_text("WE ACTUALLY CHANGED THIS")
        report = verify(self.root, manifest, ["hone"])
        self.assertIn("workout/SKILL.md", report["violations"])
        self.assertEqual(report["verdict"], "scope_violation")


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "allowed").mkdir()
        (self.root / "other").mkdir()
        (self.root / "allowed" / "f.md").write_text("a")
        (self.root / "other" / "g.md").write_text("b")
        self.manifest = build_manifest(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_in_scope_edit_is_clean(self):
        (self.root / "allowed" / "f.md").write_text("changed")
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["modified_in_scope"], ["allowed/f.md"])

    def test_out_of_scope_edit_is_a_violation(self):
        (self.root / "other" / "g.md").write_text("changed")
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/g.md", report["violations"])

    def test_untracked_new_file_out_of_scope_is_caught(self):
        # The case a git diff alone would miss entirely.
        (self.root / "other" / "new.md").write_text("surprise")
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/new.md", report["violations"])

    def test_new_file_in_scope_is_reported_not_violated(self):
        (self.root / "allowed" / "new.md").write_text("expected")
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("allowed/new.md", report["new_files_in_scope"])

    def test_deletion_out_of_scope_is_a_violation(self):
        (self.root / "other" / "g.md").unlink()
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")

    def test_no_change_is_clean(self):
        report = verify(self.root, self.manifest, ["allowed"])
        self.assertEqual(report["verdict"], "clean")

    def test_manifest_exclusion_prevents_self_detection(self):
        manifest_file = self.root / "m.json"
        manifest_file.write_text("{}")
        report = verify(self.root, self.manifest, ["allowed"],
                        exclude={manifest_file.resolve()})
        self.assertEqual(report["verdict"], "clean")


if __name__ == "__main__":
    unittest.main()
