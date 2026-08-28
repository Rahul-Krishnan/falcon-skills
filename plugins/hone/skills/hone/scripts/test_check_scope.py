#!/usr/bin/env python3
"""Tests for check_scope.py."""

import shutil
import subprocess
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


class TestGitPathNamespace(unittest.TestCase):
    """git status paths are repo-root-relative; the manifest is --root-relative."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        # Guarded tree sits one level below the repo root, which is the
        # documented shape: --root ~/.claude/skills inside a repo at ~/.claude.
        self.root = self.repo / "skills"
        (self.root / "hone").mkdir(parents=True)
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")
        (self.root / "other").mkdir()
        (self.root / "other" / "g.md").write_text("out of scope\n")
        (self.repo / "outside.md").write_text("above the guarded tree\n")

        def fake_git(root, *args):
            if args[:2] == ("rev-parse", "--show-toplevel"):
                return str(self.repo) + "\n"
            return (
                " M skills/hone/SKILL.md\n"
                " M skills/other/g.md\n"
                " M outside.md\n"
            )

        self._real_git = check_scope._git
        check_scope._git = fake_git

    def tearDown(self):
        check_scope._git = self._real_git
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_git_paths_are_rebased_onto_root(self):
        self.assertEqual(
            check_scope._git_changed(self.root),
            ["hone/SKILL.md", "other/g.md"],
        )

    def test_edited_in_scope_file_is_not_reported_preexisting(self):
        manifest = build_manifest(self.root)
        (self.root / "hone" / "SKILL.md").write_text("edited by this run\n")
        report = verify(self.root, manifest, ["hone"])
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["preexisting_dirty_out_of_scope"], ["other/g.md"])
        self.assertNotIn("hone/SKILL.md", report["preexisting_dirty_out_of_scope"])


if __name__ == "__main__":
    unittest.main()


class TestUntrackedDirectories(unittest.TestCase):
    """A directory this run created must not be reported as pre-existing dirt.

    `git status --porcelain` defaults to `-unormal`, which collapses a wholly
    untracked directory to `newdir/`. That entry never matched the file-level
    paths in the hash manifest, so it survived the "not in changed" filter and
    the report told the caller to revert `newdir/x.txt` and, two fields later,
    that `newdir` predated the run and must not be reverted.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.root = self.repo / "skills"
        (self.root / "hone").mkdir(parents=True)
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")
        run = subprocess.run(
            ["git", "init", "-q", str(self.repo)],
            capture_output=True, text=True,
        )
        if run.returncode != 0:  # pragma: no cover - git is present in CI
            self.skipTest("git unavailable")
        for args in (
            ["add", "-A"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_a_directory_created_by_this_run_is_not_preexisting(self):
        manifest = build_manifest(self.root)
        (self.root / "newdir").mkdir()
        (self.root / "newdir" / "x.txt").write_text("written by this run\n")
        report = verify(self.root, manifest, ["hone"])
        self.assertTrue(report["git_available"])
        self.assertEqual(report["violations"], ["newdir/x.txt"])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], [])


class TestPorcelainPathParsing(unittest.TestCase):
    """Renames and C-quoted paths must survive into the dirty-file report.

    `line[3:].strip().strip('"')` turned `R old -> new` into one nonexistent
    path and left git's octal escapes literal. Both fell out of
    `preexisting_dirty_out_of_scope`, the list that tells the caller what not
    to revert, so the caller reverted someone else's uncommitted work.
    """

    def test_a_plain_path_is_unchanged(self):
        from check_scope import _porcelain_path

        self.assertEqual(_porcelain_path(" M plain/file.py"), "plain/file.py")

    def test_a_path_with_spaces_is_kept_whole(self):
        from check_scope import _porcelain_path

        self.assertEqual(_porcelain_path(" M with space/file.md"), "with space/file.md")

    def test_a_rename_keeps_the_destination(self):
        from check_scope import _porcelain_path

        self.assertEqual(
            _porcelain_path("R  old/path.md -> new/path.md"), "new/path.md"
        )

    def test_a_copy_keeps_the_destination(self):
        from check_scope import _porcelain_path

        self.assertEqual(_porcelain_path("C  a/src.md -> b/copy.md"), "b/copy.md")

    def test_octal_escapes_are_decoded(self):
        from check_scope import _porcelain_path

        self.assertEqual(
            _porcelain_path('?? "caf\\303\\251/note.md"'), "café/note.md"
        )

    def test_a_quoted_rename_is_both_split_and_unquoted(self):
        from check_scope import _porcelain_path

        self.assertEqual(
            _porcelain_path('R  "old \\303\\251.md" -> "new \\303\\251.md"'),
            "new é.md",
        )

    def test_escaped_quotes_and_backslashes_round_trip(self):
        from check_scope import _porcelain_path

        self.assertEqual(_porcelain_path('?? "a\\"b/c\\\\d.md"'), 'a"b/c\\d.md')


class TestManifestRootMismatch(unittest.TestCase):
    """A --verify under a different root must exit 2, not emit a revert list.

    SKILL.md Step 6a tells the executor to reuse $SCOPE_ROOT from Step 5a, but
    shell state does not survive between tool calls, so a re-derived or empty
    --root is a realistic outcome. Under a mismatched root every recorded file
    is "removed" and every present file is "added", and the documented response
    to a violation is to revert the listed paths.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "skills"
        (self.root / "hone").mkdir(parents=True)
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")
        self.other = self.tmp / "elsewhere"
        (self.other / "hone").mkdir(parents=True)
        (self.other / "hone" / "SKILL.md").write_text("different tree\n")
        self.manifest = self.tmp / "manifest.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(check_scope.__file__)), *args],
            capture_output=True, text=True,
        )

    def test_verify_under_a_different_root_exits_2(self):
        snap = self._run("--root", str(self.root), "--manifest", str(self.manifest),
                         "--snapshot")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        verify_run = self._run("--root", str(self.other), "--manifest",
                               str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 2, verify_run.stdout)
        self.assertIn("manifest was taken under root", verify_run.stderr)
        self.assertNotIn("VIOLATION", verify_run.stdout)

    def test_verify_under_the_recorded_root_still_runs(self):
        self._run("--root", str(self.root), "--manifest", str(self.manifest),
                  "--snapshot")
        verify_run = self._run("--root", str(self.root), "--manifest",
                               str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)

    def test_a_spelling_difference_for_the_same_directory_is_not_a_mismatch(self):
        self._run("--root", str(self.root), "--manifest", str(self.manifest),
                  "--snapshot")
        respelled = str(self.root) + "/./hone/.."
        verify_run = self._run("--root", respelled, "--manifest",
                               str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)
