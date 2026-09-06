#!/usr/bin/env python3
"""Tests for check_scope.py."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import check_scope  # noqa: E402
from check_scope import (  # noqa: E402
    _in_scope,
    _is_ignored,
    build_manifest,
    derive_root,
    install_root,
    permitted_scope,
    verify,
)

SCRIPT = str(Path(check_scope.__file__))


def walk_state(root: Path, exclude=None,
               max_files: int = check_scope.DEFAULT_MAX_FILES) -> dict:
    """Build walk-mode state through snapshot_state, including its budget and
    unreadable-path records, so fixtures cannot hide omissions in real snapshots.
    """
    files, unmeasurable = check_scope.walk_manifest(root, exclude, max_files)
    return {"mode": "walk", "files": files, "unmeasurable": unmeasurable,
            "max_files": max_files}


def init_repo(path: Path) -> bool:
    """Initialise a real git repo at `path` and commit everything in it."""
    run = subprocess.run(["git", "init", "-q", str(path)],
                         capture_output=True, text=True)
    if run.returncode != 0:  # pragma: no cover - git is present in CI
        return False
    for args in (
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(path), *args], capture_output=True)
    return True


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True)


def declaring(root: Path, *paths: str) -> check_scope.Declaration:
    """Declare exactly paths, converting root-relative inputs to resolved paths."""
    return check_scope.normalize_declared([str(root / p) for p in paths], root)


# Explicit no-writes is present and empty; an absent declaration is different.
NOTHING = check_scope.Declaration(set())


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

    def test_a_nested_scope_path_matches_only_below_itself(self):
        # The derived scope for a skill in a repo is a multi-segment path.
        scope = ["plugins/p/skills/s"]
        self.assertTrue(_in_scope("plugins/p/skills/s/SKILL.md", scope))
        self.assertFalse(_in_scope("plugins/p/commands/c.md", scope))
        self.assertFalse(_in_scope("scripts/x.py", scope))


class TestIgnored(unittest.TestCase):
    def test_noise_is_ignored(self):
        self.assertTrue(_is_ignored(Path(".DS_Store")))
        self.assertTrue(_is_ignored(Path("hone/__pycache__/x.pyc")))
        self.assertTrue(_is_ignored(Path("hone/SKILL.md.pre-hone")))

    def test_real_content_is_not_ignored(self):
        self.assertFalse(_is_ignored(Path("hone/SKILL.md")))


class TestDerivation(unittest.TestCase):
    """Root and scope derive independently: hooks need a narrow watch and one
    permitted file; repository skills need a wide watch and one permitted directory.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_skill_may_change_its_whole_directory(self):
        artifact = self.tmp / "skills" / "hone" / "SKILL.md"
        self.assertEqual(permitted_scope(artifact, "skill"), artifact.parent)

    def test_a_single_file_artifact_may_change_only_itself(self):
        artifact = self.tmp / "hooks" / "foo.sh"
        for kind in ("hook", "command", "script"):
            self.assertEqual(permitted_scope(artifact, kind), artifact)

    def test_install_root_for_a_hook_is_the_hooks_dir_not_its_parent(self):
        artifact = self.tmp / ".claude" / "hooks" / "foo.sh"
        self.assertEqual(install_root(artifact, "hook"), self.tmp / ".claude" / "hooks")

    def test_install_root_for_a_skill_is_the_skills_dir(self):
        artifact = self.tmp / ".claude" / "skills" / "hone" / "SKILL.md"
        self.assertEqual(install_root(artifact, "skill"),
                         self.tmp / ".claude" / "skills")

    def test_the_install_dir_survives_a_symlinked_artifact_directory(self):
        """access_path must resolve only the install directory; following the
        artifact symlink would derive a watch from the wrong checkout.
        """
        checkout = self.tmp / "checkout"
        (checkout / "skills" / "hone").mkdir(parents=True)
        (checkout / "skills" / "hone" / "SKILL.md").write_text("v1\n")
        if not init_repo(checkout):  # pragma: no cover - git is in CI
            self.skipTest("git unavailable")
        install = self.tmp / "install" / "skills"
        install.mkdir(parents=True)
        (install / "hone").symlink_to(checkout / "skills" / "hone")

        artifact = check_scope.access_path(install / "hone" / "SKILL.md", "skill")
        self.assertEqual(artifact,
                         Path(os.path.realpath(install)) / "hone" / "SKILL.md")
        root, fallback = derive_root(artifact, "skill")
        self.assertIsNone(fallback)
        self.assertEqual(root, Path(os.path.realpath(install)))

    def test_outside_a_repo_the_root_is_the_install_dir(self):
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        artifact = hooks / "foo.sh"
        artifact.write_text("#!/bin/sh\n")
        root, fallback = derive_root(Path(os.path.realpath(artifact)), "hook")
        self.assertIsNone(fallback)
        self.assertEqual(os.path.realpath(root), os.path.realpath(hooks))


class TestPreexistingDirtyTree(unittest.TestCase):
    """Pre-existing dirty files unchanged since snapshot must never be reverted."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "workout").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("hone v1")
        (self.root / "workout" / "SKILL.md").write_text("workout dirty already")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_git_dirty_but_hash_unchanged_is_not_a_violation(self):
        state = walk_state(self.root)
        # Edit only in-scope; workout/ is untouched by this "run".
        (self.root / "hone" / "SKILL.md").write_text("hone v2")

        real_git = check_scope._git_changed
        # Simulate a repo where workout/ was already uncommitted before we ran.
        check_scope._git_changed = lambda root: ["hone/SKILL.md", "workout/SKILL.md"]
        try:
            report = verify(self.root, state, ["hone"],
                            declared=declaring(self.root, "hone/SKILL.md"))
        finally:
            check_scope._git_changed = real_git

        self.assertEqual(report["violations"], [])
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("workout/SKILL.md", report["preexisting_dirty_out_of_scope"])

    def test_real_out_of_scope_edit_still_violates(self):
        state = walk_state(self.root)
        (self.root / "workout" / "SKILL.md").write_text("WE ACTUALLY CHANGED THIS")
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "workout/SKILL.md"))
        self.assertIn("workout/SKILL.md", report["violations"])
        self.assertEqual(report["verdict"], "scope_violation")


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "allowed").mkdir()
        (self.root / "other").mkdir()
        (self.root / "allowed" / "f.md").write_text("a")
        (self.root / "other" / "g.md").write_text("b")
        self.state = walk_state(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_in_scope_edit_is_clean(self):
        (self.root / "allowed" / "f.md").write_text("changed")
        report = verify(self.root, self.state, ["allowed"],
                        declared=declaring(self.root, "allowed/f.md"))
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["modified_in_scope"], ["allowed/f.md"])

    def test_out_of_scope_edit_is_a_violation(self):
        (self.root / "other" / "g.md").write_text("changed")
        report = verify(self.root, self.state, ["allowed"],
                        declared=declaring(self.root, "other/g.md"))
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/g.md", report["violations"])

    def test_untracked_new_file_out_of_scope_is_caught(self):
        # The case a git diff alone would miss entirely.
        (self.root / "other" / "new.md").write_text("surprise")
        report = verify(self.root, self.state, ["allowed"],
                        declared=declaring(self.root, "other/new.md"))
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/new.md", report["violations"])

    def test_new_file_in_scope_is_reported_not_violated(self):
        (self.root / "allowed" / "new.md").write_text("expected")
        report = verify(self.root, self.state, ["allowed"],
                        declared=declaring(self.root, "allowed/new.md"))
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("allowed/new.md", report["new_files_in_scope"])

    def test_deletion_out_of_scope_is_a_violation(self):
        (self.root / "other" / "g.md").unlink()
        report = verify(self.root, self.state, ["allowed"],
                        declared=declaring(self.root, "other/g.md"))
        self.assertEqual(report["verdict"], "scope_violation")

    def test_no_change_is_clean(self):
        report = verify(self.root, self.state, ["allowed"], declared=NOTHING)
        self.assertEqual(report["verdict"], "clean")

    def test_manifest_exclusion_prevents_self_detection(self):
        manifest_file = self.root / "m.json"
        manifest_file.write_text("{}")
        report = verify(self.root, self.state, ["allowed"],
                        exclude={manifest_file.resolve()}, declared=NOTHING)
        self.assertEqual(report["verdict"], "clean")


class TestWalkPruning(unittest.TestCase):
    """The walk must stop at noise directories rather than filter after them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "keep").mkdir()
        (self.root / "keep" / "a.md").write_text("a")
        for noisy in (".git", "__pycache__", ".pytest_cache"):
            (self.root / noisy).mkdir()
            (self.root / noisy / "junk").write_text("junk")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_noise_directories_contribute_nothing(self):
        self.assertEqual(sorted(build_manifest(self.root)), ["keep/a.md"])

    def test_the_limit_is_enforced(self):
        with self.assertRaises(check_scope.TooManyFiles):
            build_manifest(self.root, limit=0)


class TestGitPathNamespace(unittest.TestCase):
    """git status paths are repo-root-relative; the manifest is --root-relative."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        # Use a watch below the repository root, as after budget-driven narrowing.
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
        state = walk_state(self.root)
        (self.root / "hone" / "SKILL.md").write_text("edited by this run\n")
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "hone/SKILL.md"))
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["preexisting_dirty_out_of_scope"], ["other/g.md"])
        self.assertNotIn("hone/SKILL.md", report["preexisting_dirty_out_of_scope"])


class TestUntrackedDirectories(unittest.TestCase):
    """Expand untracked directories to file paths so new files cannot also be
    reported as pre-existing directory dirt.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.root = self.repo / "skills"
        (self.root / "hone").mkdir(parents=True)
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")
        if not init_repo(self.repo):  # pragma: no cover - git is present in CI
            self.skipTest("git unavailable")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_a_directory_created_by_this_run_is_not_preexisting(self):
        state = check_scope.snapshot_state(self.root)
        (self.root / "newdir").mkdir()
        (self.root / "newdir" / "x.txt").write_text("written by this run\n")
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "newdir/x.txt"))
        self.assertTrue(report["git_available"])
        self.assertEqual(report["violations"], ["newdir/x.txt"])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], [])


class TestPorcelainPathParsing(unittest.TestCase):
    """Preserve rename destinations and C-quoted names in dirty-file reports;
    otherwise the list of pre-existing work to protect loses entries.
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
    """Reject a verify root that differs from the manifest with exit 2.

    Comparing different roots would fabricate removed and added paths.
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

    def test_verify_under_a_different_root_exits_2(self):
        snap = run_cli("--root", str(self.root), "--manifest", str(self.manifest),
                       "--snapshot")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        verify_run = run_cli("--root", str(self.other), "--manifest",
                             str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 2, verify_run.stdout)
        self.assertIn("manifest was taken under root", verify_run.stderr)
        self.assertNotIn("VIOLATION", verify_run.stdout)

    def test_verify_under_the_recorded_root_still_runs(self):
        run_cli("--root", str(self.root), "--manifest", str(self.manifest),
                "--snapshot")
        verify_run = run_cli("--root", str(self.root), "--manifest",
                             str(self.manifest), "--scope", "hone", "--verify",
                             "--declared-none")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)

    def test_a_spelling_difference_for_the_same_directory_is_not_a_mismatch(self):
        run_cli("--root", str(self.root), "--manifest", str(self.manifest),
                "--snapshot")
        respelled = str(self.root) + "/./hone/.."
        verify_run = run_cli("--root", respelled, "--manifest",
                             str(self.manifest), "--scope", "hone", "--verify",
                             "--declared-none")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)


class TestSkillInsideARepository(unittest.TestCase):
    """Watch repository-wide collateral edits: sibling commands, plugins, and
    repo-root scripts must remain visible for nested skills.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "plugins" / "p" / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill v1\n")
        (self.repo / "plugins" / "p" / "commands").mkdir(parents=True)
        (self.repo / "plugins" / "p" / "commands" / "c.md").write_text("cmd v1\n")
        (self.repo / "plugins" / "q").mkdir(parents=True)
        (self.repo / "plugins" / "q" / "SKILL.md").write_text("sibling plugin v1\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "x.py").write_text("print(1)\n")
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "shared.md").write_text("clean v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self, *extra):
        return run_cli("--artifact", str(self.skill / "SKILL.md"), "--type", "skill",
                       "--manifest", str(self.manifest), "--snapshot", "--json", *extra)

    def test_the_watch_root_is_the_repository_and_the_scope_is_the_skill_dir(self):
        snap = self._snapshot()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = json.loads(self.manifest.read_text())
        self.assertEqual(os.path.realpath(payload["root"]),
                         os.path.realpath(self.repo))
        self.assertEqual(payload["scope"], ["plugins/p/skills/s"])

    def test_sibling_command_and_repo_root_script_are_reported_unattributed(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        (self.repo / "plugins" / "p" / "commands" / "c.md").write_text("cmd v2\n")
        (self.repo / "scripts" / "x.py").write_text("print(2)\n")
        (self.repo / "plugins" / "q" / "SKILL.md").write_text("sibling v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"))
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["unattributed_out_of_scope"], [
            "plugins/p/commands/c.md",
            "plugins/q/SKILL.md",
            "scripts/x.py",
        ])
        # Nothing here may be reverted: the caller's revert list stays empty.
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["modified_in_scope"], ["plugins/p/skills/s/SKILL.md"])

    def test_a_clean_tracked_file_change_is_seen_but_not_attributed(self):
        """Git detects clean-to-dirty changes but cannot identify their writer."""
        self.assertEqual(self._snapshot().returncode, 0)
        payload = json.loads(self.manifest.read_text())
        self.assertEqual(payload["mode"], "git")
        self.assertEqual(payload["dirty_tracked"], {})
        self.assertEqual(payload["untracked"], {})
        self.assertNotIn("lib/shared.md", json.dumps(payload))

        (self.repo / "lib" / "shared.md").write_text("clean v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["unattributed_out_of_scope"], ["lib/shared.md"])
        self.assertEqual(report["violations"], [])
        self.assertTrue(report["not_measurable_reasons"])

    def test_untracked_file_created_out_of_scope_is_a_violation(self):
        """Acceptance 8: the case a git diff cannot see at all."""
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "scripts" / "new.py").write_text("brand new\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "scripts" / "new.py"))
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["scripts/new.py"])
        self.assertEqual(report["counts"]["added"], 1)

    def test_a_gitignored_file_created_out_of_scope_is_still_a_violation(self):
        (self.repo / ".gitignore").write_text("ignored-*\n")
        subprocess.run(["git", "-C", str(self.repo), "add", ".gitignore"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "ignore"],
                       capture_output=True)
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "scripts" / "ignored-out.py").write_text("invisible to diff\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "scripts" / "ignored-out.py"))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("scripts/ignored-out.py", json.loads(run.stdout)["violations"])

    def test_a_clean_tracked_file_deleted_out_of_scope_is_unattributed(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").unlink()
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["unattributed_out_of_scope"], ["lib/shared.md"])
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["counts"]["removed"], 1)

    def test_a_dirty_file_deleted_out_of_scope_is_a_violation_when_declared(self):
        """Declared deletion is attributable, but restore from the snapshot to
        preserve earlier uncommitted work; mark violations_manual_revert.
        """
        (self.repo / "lib" / "shared.md").write_text("dirty before the run\n")
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").unlink()
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "lib" / "shared.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertEqual(report["violations"], ["lib/shared.md"])
        self.assertEqual(report["violations_manual_revert"], ["lib/shared.md"])
        self.assertEqual(report["unattributed_out_of_scope"], [])

    def test_preexisting_dirty_is_reported_and_not_violated(self):
        """Acceptance 4: dirty before the run, untouched by it."""
        (self.repo / "lib" / "shared.md").write_text("someone else's uncommitted\n")
        self.assertEqual(self._snapshot().returncode, 0)
        payload = json.loads(self.manifest.read_text())
        self.assertIn("lib/shared.md", payload["dirty_tracked"])

        (self.skill / "SKILL.md").write_text("skill v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], ["lib/shared.md"])

    def test_editing_a_preexisting_dirty_file_is_a_violation_when_declared(self):
        """Declared, so it is this run's edit however dirty the file already was."""
        (self.repo / "lib" / "shared.md").write_text("someone else's uncommitted\n")
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").write_text("and then WE changed it\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "lib" / "shared.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["lib/shared.md"])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], [])

    def test_verify_needs_only_the_manifest(self):
        """Acceptance 6: no --root, no --scope, no re-derivation by the caller."""
        self.assertEqual(self._snapshot().returncode, 0)
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["scope"], ["plugins/p/skills/s"])
        self.assertEqual(os.path.realpath(report["root"]),
                         os.path.realpath(self.repo))

    def test_a_large_but_clean_repository_keeps_the_wide_watch(self):
        """Budget hashing workload, not tracked count; clean repositories need no
        hashes and should retain repository-wide coverage.
        """
        snap = self._snapshot("--max-files", "1")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        report = json.loads(snap.stdout)
        self.assertNotIn("root_fallback", report)
        self.assertEqual(os.path.realpath(report["root"]),
                         os.path.realpath(self.repo))

        # Confirm the wide watch still detects collateral changes.
        (self.repo / "plugins" / "p" / "commands" / "c.md").write_text("cmd v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared",
                      str(self.repo / "plugins" / "p" / "commands" / "c.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"],
                         ["plugins/p/commands/c.md"])

    def test_max_files_narrows_the_root_and_says_so(self):
        """Report narrowed coverage when actual hashing workload exceeds budget."""
        for name in ("a", "b", "c", "d"):
            (self.repo / f"untracked-{name}.md").write_text("uncommitted\n")
        snap = self._snapshot("--max-files", "2")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        fallback = json.loads(snap.stdout)["root_fallback"]
        self.assertEqual(fallback["reason"], "max_files_exceeded")
        self.assertEqual(fallback["limit"], 2)
        self.assertGreater(fallback["candidate_count"], 2)
        self.assertEqual(os.path.realpath(fallback["intended_root"]),
                         os.path.realpath(self.repo))

        payload = json.loads(self.manifest.read_text())
        self.assertEqual(os.path.realpath(payload["root"]),
                         os.path.realpath(self.skill.parent))
        self.assertEqual(payload["scope"], ["s"])

        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared-none")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("NARROWER than intended", run.stdout)


class TestHookOutsideARepository(unittest.TestCase):
    """Watch the hook's install directory and permit only its file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hooks = self.tmp / "hooks"
        self.hooks.mkdir()
        (self.hooks / "foo.sh").write_text("#foo v1\n")
        (self.hooks / "bar.sh").write_text("#bar v1\n")
        (self.tmp / "settings.json").write_text("{}\n")
        self.manifest = Path(tempfile.mkdtemp()) / "m.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.manifest.parent, ignore_errors=True)

    def _snapshot(self):
        return run_cli("--artifact", str(self.hooks / "foo.sh"), "--type", "hook",
                       "--manifest", str(self.manifest), "--snapshot", "--json")

    def test_the_watch_root_is_the_hooks_dir_not_its_parent(self):
        snap = self._snapshot()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = json.loads(self.manifest.read_text())
        self.assertEqual(os.path.realpath(payload["root"]),
                         os.path.realpath(self.hooks))
        self.assertEqual(payload["scope"], ["foo.sh"])
        # settings.json is outside this watch.
        self.assertNotIn("settings.json", json.dumps(payload["files"]))

    def test_editing_the_artifact_itself_is_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.hooks / "foo.sh").write_text("#foo v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.hooks / "foo.sh"))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["modified_in_scope"], ["foo.sh"])

    def test_editing_a_sibling_hook_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.hooks / "bar.sh").write_text("#bar v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.hooks / "bar.sh"))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"], ["bar.sh"])


class TestCliUsageErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "foo.sh").write_text("#!/bin/sh\n")
        self.manifest = self.tmp / "m.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifact_without_type_is_a_usage_error(self):
        run = run_cli("--artifact", str(self.tmp / "foo.sh"),
                      "--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(run.returncode, 2)
        self.assertIn("must be given together", run.stderr)

    def test_a_missing_artifact_is_a_usage_error(self):
        run = run_cli("--artifact", str(self.tmp / "nope.sh"), "--type", "hook",
                      "--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(run.returncode, 2)
        self.assertIn("artifact not found", run.stderr)

    def test_a_directory_artifact_is_rejected(self):
        run = run_cli("--artifact", str(self.tmp), "--type", "skill",
                      "--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(run.returncode, 2)
        self.assertIn("must be a file", run.stderr)

    def test_snapshot_without_artifact_or_root_is_a_usage_error(self):
        run = run_cli("--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(run.returncode, 2)
        self.assertIn("--artifact/--type", run.stderr)

    def test_verify_with_a_scopeless_manifest_is_a_usage_error(self):
        run_cli("--root", str(self.tmp), "--manifest", str(self.manifest),
                "--snapshot")
        run = run_cli("--manifest", str(self.manifest), "--verify")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("no scope", run.stderr)

    def test_verify_with_a_missing_manifest_is_a_usage_error(self):
        """Exit 2 is "the check never ran", and the caller has a branch for it."""
        run = run_cli("--manifest", str(self.tmp / "gone.json"), "--verify")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("manifest not found", run.stderr)


class TestGitGoesQuietAtVerify(unittest.TestCase):
    """Unavailable git after snapshot must return not_measurable, never clean."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill v1\n")
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "shared.md").write_text("clean v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"
        run = run_cli("--artifact", str(self.skill / "SKILL.md"), "--type", "skill",
                      "--manifest", str(self.manifest), "--snapshot", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_an_unanswerable_git_reports_not_measurable(self):
        (self.repo / "lib" / "shared.md").write_text("clobbered\n")
        (self.repo / "lib" / "new.md").write_text("also clobbered\n")
        shutil.rmtree(self.repo / ".git")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertFalse(report["git_available"])
        self.assertTrue(report["not_measurable_reasons"])

    def test_the_hash_manifest_note_is_not_printed_for_a_git_manifest(self):
        """A git-mode manifest has no whole-tree hashes to substitute for git."""
        shutil.rmtree(self.repo / ".git")
        run = run_cli("--manifest", str(self.manifest), "--verify")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertNotIn("hash manifest was the only check", run.stdout)
        self.assertIn("UNCHECKED", run.stdout)


class TestNestedRepositoriesAndSubmodules(unittest.TestCase):
    """Inspect collapsed nested repositories and gitlinks as subtrees.

    Nested skill/script repositories are common in the watched configuration tree.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.nested = self.repo / "nested"
        self.nested.mkdir()
        (self.nested / "other.md").write_text("nested v1\n")
        if not init_repo(self.nested):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self):
        return run_cli("--artifact", str(self.skill / "SKILL.md"), "--type", "skill",
                       "--manifest", str(self.manifest), "--snapshot", "--json")

    def test_the_snapshot_hashes_the_nested_repository(self):
        snap = self._snapshot()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertIn("nested", json.loads(snap.stdout)["nested_repos"])
        payload = json.loads(self.manifest.read_text())
        self.assertIn("other.md", payload["nested"]["nested"])

    def test_an_edit_inside_a_nested_repository_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.nested / "other.md").write_text("clobbered by the run\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.nested / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("nested/other.md", report["violations"])

    def test_a_new_file_in_a_nested_repository_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.nested / "added.md").write_text("brand new\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.nested / "added.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("nested/added.md", json.loads(run.stdout)["violations"])

    def test_an_untouched_nested_repository_verifies_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["verdict"], "clean")

    def test_a_registered_submodule_is_hashed_not_called_removed(self):
        add = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
             "submodule", "add", "-q", str(self.nested), "sub"],
            capture_output=True, text=True)
        if add.returncode != 0:  # pragma: no cover - old git
            self.skipTest("git submodule add unavailable")
        self.assertEqual(self._snapshot().returncode, 0)
        payload = json.loads(self.manifest.read_text())
        self.assertIn("sub", payload["nested"])

        (self.repo / "sub" / "other.md").write_text("clobbered inside a submodule\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "sub" / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertIn("sub/other.md", report["violations"])
        # The gitlink path itself is never reported as a removed file.
        self.assertNotIn("sub", report["violations"])

    def test_a_submodule_is_seen_when_the_root_sits_below_the_toplevel(self):
        """Rebase porcelain paths, but not ls-files paths, which are already
        root-relative; rebasing both would hide nested-root submodules.
        """
        add = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
             "submodule", "add", "-q", str(self.nested), "skills/sub"],
            capture_output=True, text=True)
        if add.returncode != 0:  # pragma: no cover - old git
            self.skipTest("git submodule add unavailable")
        run = run_cli("--root", str(self.repo / "skills"), "--scope", "s",
                      "--manifest", str(self.manifest), "--snapshot", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["nested_repos"], ["sub"])

        (self.repo / "skills" / "sub" / "other.md").write_text("clobbered\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared",
                      str(self.repo / "skills" / "sub" / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("sub/other.md", json.loads(run.stdout)["violations"])

    def test_an_unhashable_subtree_reports_not_measurable(self):
        # Count the opaque subtree once in the outer budget, then exceed that
        # budget inside it: one subtree containing two files, budget one.
        (self.nested / "second.md").write_text("nested v1 too\n")
        snap = run_cli("--root", str(self.repo), "--scope", "skills/s",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", "1")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertIn("nested", json.loads(snap.stdout)["unmeasurable"])
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(report["not_measurable_reasons"])

    def test_an_unhashable_subtree_does_not_swallow_a_real_violation(self):
        """Partial coverage must retain observed violations alongside the halt."""
        (self.nested / "second.md").write_text("nested v1 too\n")
        snap = run_cli("--root", str(self.repo), "--scope", "skills/s",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", "1")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        (self.repo / "outside.md").write_text("untracked and out of scope\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "outside.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("outside.md", report["violations"])
        self.assertTrue(report["not_measurable_reasons"])


class TestPreImagesSurviveASubtreeChangingShape(unittest.TestCase):
    """Preserve pre-images when git init or submodule removal changes a file's
    storage classification; unchanged files must not become added findings.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self):
        return run_cli("--artifact", str(self.skill / "SKILL.md"), "--type", "skill",
                       "--manifest", str(self.manifest), "--snapshot", "--json")

    def test_a_plain_dir_becoming_a_repo_does_not_invent_violations(self):
        vendor = self.repo / "vendor"
        vendor.mkdir()
        (vendor / "lib.txt").write_text("untracked v1\n")
        self.assertEqual(self._snapshot().returncode, 0)
        self.assertIn("vendor/lib.txt",
                      json.loads(self.manifest.read_text())["untracked"])

        if not init_repo(vendor):  # pragma: no cover
            self.skipTest("git unavailable")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [], run.stdout)
        self.assertEqual(run.returncode, 0, run.stdout)

    def test_a_repo_ceasing_to_be_one_does_not_invent_violations(self):
        vendor = self.repo / "vendor"
        vendor.mkdir()
        (vendor / "lib.txt").write_text("nested v1\n")
        if not init_repo(vendor):  # pragma: no cover
            self.skipTest("git unavailable")
        self.assertEqual(self._snapshot().returncode, 0)
        self.assertIn("vendor", json.loads(self.manifest.read_text())["nested"])

        shutil.rmtree(vendor / ".git")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [], run.stdout)
        self.assertEqual(run.returncode, 0, run.stdout)

    def test_a_file_is_not_reported_twice_when_a_subtree_is_opaque(self):
        vendor = self.repo / "vendor"
        vendor.mkdir()
        (vendor / "lib.txt").write_text("untracked v1\n")
        if not init_repo(vendor):  # pragma: no cover
            self.skipTest("git unavailable")
        self.assertEqual(self._snapshot().returncode, 0)
        (vendor / "lib.txt").write_text("clobbered\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(vendor / "lib.txt"))
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["vendor/lib.txt"], run.stdout)
        self.assertEqual(report["counts"]["modified"], 1)


class TestUnreadableFiles(unittest.TestCase):
    """Unreadable files differ from absent files and remain unmeasurable even
    when unreadable at both snapshot and verify.
    """

    def setUp(self):
        if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
            self.skipTest("running as root; chmod 000 is not enforced")
        self.tmp = Path(tempfile.mkdtemp())
        self.hooks = self.tmp / "hooks"
        self.hooks.mkdir()
        (self.hooks / "h.sh").write_text("#!/bin/sh\n")
        (self.hooks / "other.sh").write_text("sibling v1\n")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        (self.hooks / "other.sh").chmod(0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_an_unreadable_file_is_not_measurable_not_a_violation(self):
        run = run_cli("--artifact", str(self.hooks / "h.sh"), "--type", "hook",
                      "--manifest", str(self.manifest), "--snapshot", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        (self.hooks / "other.sh").chmod(0o000)
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [], run.stdout)
        self.assertEqual(report["verdict"], "not_measurable", run.stdout)
        self.assertEqual(run.returncode, 3, run.stdout)

    def test_a_file_unreadable_at_both_ends_does_not_read_as_clean(self):
        (self.hooks / "other.sh").chmod(0o000)
        run = run_cli("--artifact", str(self.hooks / "h.sh"), "--type", "hook",
                      "--manifest", str(self.manifest), "--snapshot", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(json.loads(run.stdout)["verdict"], "not_measurable",
                         run.stdout)
        self.assertEqual(run.returncode, 3, run.stdout)


class TestToolCacheDirectoriesAreIgnored(unittest.TestCase):
    """Routine lint caches must not halt an edit round."""

    def test_common_caches_are_ignored(self):
        for rel in (".ruff_cache/x/0.json", ".mypy_cache/3.12/m.data.json",
                    "node_modules/pkg/index.js", ".venv/lib/site.py",
                    "htmlcov/index.html", ".tox/py312/log.txt"):
            self.assertTrue(_is_ignored(Path(rel)), rel)

    def test_source_directories_are_not_ignored(self):
        for rel in ("scripts/x.py", "src/app/main.py", "dist/bundle.js",
                    "build/out.txt", "references/notes.md"):
            self.assertFalse(_is_ignored(Path(rel)), rel)


class TestAttributionComesFromTheDeclaration(unittest.TestCase):
    """Attribute edits from declarations, never from pre-image availability.

    Declared edits to clean siblings are violations; another writer's edits to
    pre-existing work must never enter the restore list.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "plugins" / "p" / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill v1\n")
        (self.repo / "plugins" / "p" / "commands").mkdir(parents=True)
        self.sibling = self.repo / "plugins" / "p" / "commands" / "other.md"
        self.sibling.write_text("other v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self):
        return run_cli("--artifact", str(self.skill / "SKILL.md"), "--type",
                       "skill", "--manifest", str(self.manifest), "--snapshot")

    def test_the_runs_own_out_of_scope_edit_is_a_real_violation(self):
        """Declared edits to clean tracked siblings are violations."""
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        self.sibling.write_text("edited by the run, out of scope\n")

        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"),
                      "--declared", str(self.sibling))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertEqual(report["violations"],
                         ["plugins/p/commands/other.md"])
        # Clean and tracked at snapshot, so a checkout is the right remedy.
        self.assertEqual(report["violations_manual_revert"], [])
        self.assertEqual(report["unattributed_out_of_scope"], [])

    def test_the_users_own_wip_edited_by_the_user_is_never_reverted(self):
        """Another writer's changes to pre-existing work are not attributable."""
        self.sibling.write_text("the user's uncommitted work\n")
        self.assertEqual(self._snapshot().returncode, 0)
        self.assertIn("plugins/p/commands/other.md",
                      json.loads(self.manifest.read_text())["dirty_tracked"])

        self.sibling.write_text("the user saves again, mid-round\n")
        (self.skill / "SKILL.md").write_text("skill v2\n")

        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"))
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["unattributed_out_of_scope"],
                         ["plugins/p/commands/other.md"])

    def test_an_untracked_file_someone_else_created_is_never_deleted(self):
        """Another writer's new untracked file must never enter the delete list."""
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        (self.repo / "build-output.txt").write_text("a build step wrote this\n")

        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"))
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["unattributed_out_of_scope"],
                         ["build-output.txt"])

    def test_an_untracked_file_this_run_created_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        (self.repo / "stray.txt").write_text("written by the run\n")

        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.skill / "SKILL.md"),
                      "--declared", str(self.repo / "stray.txt"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["stray.txt"])
        # Untracked, so `git checkout` cannot restore it either way.
        self.assertEqual(report["violations_manual_revert"], ["stray.txt"])

    def test_an_edit_the_run_did_not_declare_in_scope_is_not_measurable(self):
        """Undeclared in-scope edits show the declaration is incomplete."""
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["undeclared_in_scope"],
                         ["plugins/p/skills/s/SKILL.md"])


class TestTheDeclarationIsMandatory(unittest.TestCase):
    """Missing declarations are not_measurable even when the tree is unchanged;
    the guard cannot rule out writes beyond its watch.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("v1\n")
        (self.repo / "other.md").write_text("v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"
        snap = run_cli("--artifact", str(self.skill / "SKILL.md"), "--type",
                       "skill", "--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(snap.returncode, 0, snap.stderr)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_an_absent_declaration_on_an_untouched_tree_is_not_clean(self):
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertFalse(report["declaration_present"])
        self.assertTrue(any("declared no edited paths" in reason
                            for reason in report["not_measurable_reasons"]))

    def test_an_absent_declaration_never_produces_a_revert_list(self):
        (self.repo / "other.md").write_text("v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["unattributed_out_of_scope"], ["other.md"])

    def test_an_explicit_empty_declaration_on_an_untouched_tree_is_clean(self):
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-none")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "clean")
        self.assertTrue(report["declaration_present"])


class TestDeclarationShape(unittest.TestCase):
    """Accept supported declarations and reject malformed ones."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.skill = self.repo / "skills" / "s"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("v1\n")
        (self.repo / "other.md").write_text("v1\n")
        (self.repo / "third.md").write_text("v1\n")
        if not init_repo(self.repo):  # pragma: no cover
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"
        snap = run_cli("--artifact", str(self.skill / "SKILL.md"), "--type",
                       "skill", "--manifest", str(self.manifest), "--snapshot")
        self.assertEqual(snap.returncode, 0, snap.stderr)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _declared_file(self, payload) -> str:
        path = self.workdir / "declared.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def test_a_declared_directory_is_a_usage_error(self):
        """A directory would attribute everything under it, the user's work included."""
        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared", str(self.repo / "skills"))
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("is a directory", run.stderr)

    def test_declaring_one_file_does_not_attribute_its_sibling(self):
        (self.repo / "other.md").write_text("v2\n")
        (self.repo / "third.md").write_text("v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["other.md"])
        self.assertEqual(report["unattributed_out_of_scope"], ["third.md"])

    def test_a_root_relative_spelling_matches(self):
        (self.repo / "other.md").write_text("v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", "other.md")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"], ["other.md"])

    def test_a_noisy_spelling_of_the_same_path_matches(self):
        (self.repo / "other.md").write_text("v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "skills" / ".." / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"], ["other.md"])

    def test_a_declared_file_holding_a_bare_list_is_read(self):
        (self.repo / "other.md").write_text("v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-file",
                      self._declared_file([str(self.repo / "other.md")]))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"], ["other.md"])

    def test_a_workflow_state_file_is_read_directly(self):
        """The executor already writes applied_edits; point the flag at it."""
        (self.repo / "other.md").write_text("v2\n")
        state = {"applied_edits": {"edit_count": 1, "confirmed_on_disk": True,
                                   "edited_paths": [str(self.repo / "other.md")]}}
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-file", self._declared_file(state))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"], ["other.md"])

    def test_a_declared_file_with_no_declaration_in_it_is_a_usage_error(self):
        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared-file", self._declared_file({"unrelated": 1}))
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("holds no declaration", run.stderr)

    def test_an_empty_declared_file_is_a_usage_error_not_an_empty_declaration(self):
        """Empty files cannot implicitly claim --declared-none."""
        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared-file", self._declared_file([]))
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("--declared-none", run.stderr)

    def test_a_missing_declared_file_is_a_usage_error(self):
        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared-file", str(self.workdir / "gone.json"))
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("not found", run.stderr)

    def test_declared_none_cannot_be_combined_with_a_declaration(self):
        run = run_cli("--manifest", str(self.manifest), "--verify",
                      "--declared-none", "--declared", str(self.repo / "other.md"))
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("two things at once", run.stderr)

    def test_a_declaration_on_snapshot_is_a_usage_error(self):
        run = run_cli("--artifact", str(self.skill / "SKILL.md"), "--type",
                      "skill", "--manifest", str(self.workdir / "n.json"),
                      "--snapshot", "--declared-none")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("belong to --verify", run.stderr)

    def test_a_path_declared_outside_the_watched_root_is_reported_not_blamed(self):
        outside = Path(tempfile.mkdtemp()) / "elsewhere.md"
        outside.write_text("written somewhere the guard never watched\n")
        try:
            run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                          "--declared", str(outside))
            self.assertEqual(run.returncode, 3, run.stdout)
            report = json.loads(run.stdout)
            self.assertEqual(report["verdict"], "not_measurable")
            self.assertEqual(report["violations"], [])
            self.assertEqual(report["declared_outside_root"], [str(outside)])
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

    def test_a_declared_out_of_scope_path_that_did_not_change_is_not_reverted(self):
        """The run admits writing there, so not `clean`; nothing changed, so no revert."""
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "other.md"))
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["declared_out_of_scope"], ["other.md"])


class TestUnrecognizedManifestMode(unittest.TestCase):
    """Unknown manifest modes must not fabricate added files across the tree."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("v1\n")
        (self.root / "elsewhere.md").write_text("v1\n")
        self.manifest = Path(tempfile.mkdtemp()) / "m.json"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.manifest.parent, ignore_errors=True)

    def _write(self, payload):
        self.manifest.write_text(json.dumps(
            dict({"root": str(self.root), "scope": ["hone"]}, **payload)))

    def test_a_manifest_with_no_mode_is_not_measurable(self):
        self._write({})
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])

    def test_an_unrecognized_mode_is_not_measurable(self):
        self._write({"mode": "quantum", "files": {}})
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertTrue(any("quantum" in reason
                            for reason in report["not_measurable_reasons"]))

    def test_in_process_verify_refuses_the_same_manifest(self):
        report = verify(self.root, {"scope": ["hone"]}, ["hone"],
                        declared=NOTHING)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])


class TestInternalErrorsNeverExitOne(unittest.TestCase):
    """Crashes must not use exit 1, which requires a scope-violation report."""

    def test_an_unexpected_exception_exits_2(self):
        real_main = check_scope.main

        def boom():
            raise RuntimeError("something nobody planned for")

        check_scope.main = boom
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    check_scope._cli()
        finally:
            check_scope.main = real_main
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("internal error", stderr.getvalue())

    def test_a_verdict_exit_code_still_reaches_the_shell(self):
        real_main = check_scope.main
        check_scope.main = lambda: sys.exit(3)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    check_scope._cli()
        finally:
            check_scope.main = real_main
        self.assertEqual(caught.exception.code, 3)

    def test_a_non_utf8_filename_does_not_crash_the_guard(self):
        repo = Path(tempfile.mkdtemp())
        skill = repo / "skills" / "s"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("v1\n")
        try:
            odd = os.path.join(str(repo).encode(), b"odd-\xff-name.md")
            with open(odd, "wb") as handle:
                handle.write(b"raw bytes in the name\n")
        except (OSError, UnicodeError):  # pragma: no cover - APFS refuses these
            shutil.rmtree(repo, ignore_errors=True)
            self.skipTest("filesystem rejects non-UTF-8 filenames")
        try:
            if not init_repo(repo):  # pragma: no cover
                self.skipTest("git unavailable")
            manifest = repo.parent / "odd-manifest.json"
            snap = run_cli("--artifact", str(skill / "SKILL.md"), "--type",
                           "skill", "--manifest", str(manifest), "--snapshot")
            self.assertEqual(snap.returncode, 0, snap.stderr)
            run = run_cli("--manifest", str(manifest), "--verify",
                          "--declared-none")
            self.assertIn(run.returncode, (0, 3), run.stdout + run.stderr)
            self.assertNotIn("Traceback", run.stderr)
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class TestWalkVerifyRespectsTheSnapshotBudget(unittest.TestCase):
    """Walk-mode verify must retain the snapshot's file budget."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("v1\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_tree_that_grew_past_the_budget_is_not_measurable(self):
        state = walk_state(self.root, max_files=1)
        for name in ("a", "b", "c"):
            (self.root / f"{name}.md").write_text("grown since the snapshot\n")
        report = verify(self.root, state, ["hone"], declared=NOTHING)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])
        self.assertTrue(report["not_measurable_reasons"])

    def test_a_tree_inside_the_budget_still_verifies(self):
        state = walk_state(self.root, max_files=100)
        report = verify(self.root, state, ["hone"], declared=NOTHING)
        self.assertEqual(report["verdict"], "clean")

    def test_the_snapshot_carries_its_own_budget(self):
        """Store max_files in state so in-process verify cannot silently fall back
        to the 20000-file default after a tightly budgeted snapshot.
        """
        state = check_scope.snapshot_state(self.root, max_files=7)
        self.assertEqual(state["mode"], "walk")
        self.assertEqual(state["max_files"], 7)


class TestASubtreeBecomingOpaqueInventsNothing(unittest.TestCase):
    """Newly opaque clean tracked files lack pre-images but are not added.

    Declarations prevent false restores; the classifier must also avoid false
    added findings. Exercise it directly: git normally refuses to collapse a
    subtree whose files remain in the outer index.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vendor = self.root / "vendor"
        self.vendor.mkdir()
        (self.vendor / "committed.md").write_text("clean and tracked\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_tracked_file_under_an_opaque_subtree_is_not_called_added(self):
        modified, added, removed, blind, unreadable = check_scope._classify_nested(
            self.root, {}, {}, ["vendor"], {"vendor/committed.md"}, None)
        self.assertEqual(added, [])
        self.assertEqual(modified, [])
        self.assertEqual(removed, [])
        self.assertEqual(unreadable, ["vendor/committed.md"])

    def test_an_untracked_file_under_an_opaque_subtree_is_still_added(self):
        modified, added, removed, blind, unreadable = check_scope._classify_nested(
            self.root, {}, {}, ["vendor"], set(), None)
        self.assertEqual(added, ["vendor/committed.md"])
        self.assertEqual(unreadable, [])

    def test_an_undeclared_new_file_in_a_nested_repo_is_never_a_violation(self):
        """The declaration is what stops any such mistake reaching the revert list."""
        repo = Path(tempfile.mkdtemp())
        skill = repo / "skills" / "s"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("v1\n")
        try:
            if not init_repo(repo):  # pragma: no cover
                self.skipTest("git unavailable")
            manifest = repo.parent / f"{repo.name}-m.json"
            snap = run_cli("--artifact", str(skill / "SKILL.md"), "--type",
                           "skill", "--manifest", str(manifest), "--snapshot")
            self.assertEqual(snap.returncode, 0, snap.stderr)

            nested = repo / "vendor"
            nested.mkdir()
            (nested / "appeared.md").write_text("a second session's checkout\n")
            if not init_repo(nested):  # pragma: no cover
                self.skipTest("git unavailable")

            run = run_cli("--manifest", str(manifest), "--verify", "--json",
                          "--declared-none")
            report = json.loads(run.stdout)
            self.assertEqual(report["violations"], [], run.stdout)
            self.assertEqual(run.returncode, 3, run.stdout)
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class TestNoPathLeavesTheWalkUnrecorded(unittest.TestCase):
    """Unreadable out-of-scope entries must never produce clean reports.

    Cover listing failures, unknown entry types, dangling symlinks, special
    files, and loops (module cases 4-8). Each fixture has an in-scope hone/
    directory and one problematic out-of-scope entry.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")

    def tearDown(self):
        for path in self.root.rglob("*"):
            with contextlib.suppress(OSError):
                path.chmod(0o755)
        shutil.rmtree(self.root, ignore_errors=True)

    def _verdict(self):
        state = check_scope.snapshot_state(self.root)
        return verify(self.root, state, ["hone"], declared=NOTHING)

    # Case 4: directory listing failure.
    def test_an_unlistable_directory_is_recorded(self):
        if os.geteuid() == 0:  # pragma: no cover - root reads anything
            self.skipTest("running as root; chmod 000 does not deny reads")
        shut = self.root / "shut"
        shut.mkdir()
        (shut / "inside.md").write_text("unreachable\n")
        shut.chmod(0o000)
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("shut" in reason
                            for reason in report["not_measurable_reasons"]))

    # Case 5: unknown entry type.
    def test_an_unclassifiable_entry_is_recorded(self):
        real_scandir = os.scandir

        class Exploding:
            """A DirEntry whose stat calls fail, as one on a dying mount does."""

            name = "flaky.md"

            def __init__(self, path):
                self.path = path

            def is_dir(self, follow_symlinks=True):
                raise OSError("stat failed")

            is_file = is_symlink = is_dir

        def scandir(path):
            entries = list(real_scandir(path))
            if Path(path) == self.root:
                entries.append(Exploding(str(self.root / "flaky.md")))
            return entries

        with unittest.mock.patch.object(check_scope.os, "scandir", scandir):
            report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("flaky.md" in reason
                            for reason in report["not_measurable_reasons"]))

    # Case 6: dangling symlink.
    def test_a_dangling_symlink_is_recorded(self):
        (self.root / "gone.md").symlink_to(self.root / "never-existed.md")
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("gone.md" in reason
                            for reason in report["not_measurable_reasons"]))

    # Case 7: non-regular file.
    def test_a_fifo_is_recorded(self):
        if not hasattr(os, "mkfifo"):  # pragma: no cover - POSIX only
            self.skipTest("os.mkfifo unavailable")
        os.mkfifo(self.root / "pipe")
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("pipe" in reason
                            for reason in report["not_measurable_reasons"]))

    # Case 8: directory symlink loop.
    def test_a_symlink_loop_is_recorded_not_followed(self):
        (self.root / "loop").symlink_to(self.root)
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("loop" in reason
                            for reason in report["not_measurable_reasons"]))

    # Control: readable directory symlinks are measurable.
    def test_a_symlinked_directory_is_walked_rather_than_recorded(self):
        """Follow installed-directory symlinks so their contents remain measurable."""
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target, True)
        (target / "f.txt").write_text("v1\n")
        (self.root / "linked").symlink_to(target)

        state = check_scope.snapshot_state(self.root)
        self.assertIn("linked/f.txt", state["files"])
        self.assertEqual(state["unmeasurable"], [])

        (self.root / "linked" / "f.txt").write_text("v2\n")
        undeclared = verify(self.root, state, ["hone"], declared=NOTHING)
        self.assertEqual(undeclared["verdict"], "not_measurable")
        self.assertEqual(undeclared["violations"], [])
        self.assertEqual(undeclared["unattributed_out_of_scope"], ["linked/f.txt"])

        declared = verify(self.root, state, ["hone"],
                          declared=declaring(self.root, "linked/f.txt"))
        self.assertEqual(declared["verdict"], "scope_violation")
        self.assertEqual(declared["violations"], ["linked/f.txt"])

    def test_two_symlinks_to_one_target_are_not_a_loop(self):
        """Loop protection must not misread a shared target as a cycle."""
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target, True)
        (target / "f.txt").write_text("v1\n")
        (self.root / "one").symlink_to(target)
        (self.root / "two").symlink_to(target)

        state = check_scope.snapshot_state(self.root)
        self.assertEqual(state["unmeasurable"], [])
        self.assertIn("one/f.txt", state["files"])
        self.assertIn("two/f.txt", state["files"])

    def test_a_readable_tree_still_verifies_clean(self):
        """Control: complete measurable coverage can still pass."""
        report = self._verdict()
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["not_measurable_reasons"], [])


class TestUnmeasurableRecordsSurviveTheManifest(unittest.TestCase):
    """Preserve unreadable-path records through snapshot JSON and verify."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").mkdir()
        (self.root / "hone" / "SKILL.md").write_text("in scope\n")
        self.manifest = self.root.parent / f"{self.root.name}-m.json"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        with contextlib.suppress(OSError):
            self.manifest.unlink()

    def test_the_cli_reports_and_reloads_an_unreadable_entry(self):
        (self.root / "gone.md").symlink_to(self.root / "never-existed.md")
        snap = run_cli("--root", str(self.root), "--scope", "hone",
                       "--manifest", str(self.manifest), "--snapshot", "--json")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(json.loads(snap.stdout)["unmeasurable"], ["gone.md"])

        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared-none")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["violations"], [])
        self.assertTrue(any("gone.md" in reason
                            for reason in report["not_measurable_reasons"]))

    def test_a_manifest_holding_the_old_bare_string_spelling_still_reads(self):
        """Older manifests stored `unmeasurable` as bare subtree names."""
        state = {"mode": "walk", "files": {}, "max_files": 100,
                 "unmeasurable": ["vendor/sub"]}
        report = verify(self.root, state, ["hone"], declared=NOTHING)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("vendor/sub" in reason
                            for reason in report["not_measurable_reasons"]))


class TestCommandScopeAdmitsItsValidator(unittest.TestCase):
    """Permit a command's generated validate_handoffs.py companion so declaring
    that required file does not trigger a scope violation.
    """

    def test_the_validator_sibling_is_in_scope_for_a_command(self):
        artifact = Path("/x/commands/foo.md")
        self.assertEqual(
            check_scope.permitted_scopes(artifact, "command"),
            [artifact, Path("/x/commands/foo-validator")],
        )

    def test_every_other_type_keeps_its_single_scope(self):
        for kind, artifact in (
            ("skill", Path("/x/skills/foo/SKILL.md")),
            ("hook", Path("/x/hooks/foo.sh")),
            ("script", Path("/x/scripts/foo.py")),
        ):
            self.assertEqual(
                check_scope.permitted_scopes(artifact, kind),
                [check_scope.permitted_scope(artifact, kind)], kind)

    def test_a_generated_validator_verifies_clean_end_to_end(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        commands = root / "commands"
        commands.mkdir()
        command = commands / "foo.md"
        command.write_text("# foo\n")
        manifest = root / "m.json"

        snap = run_cli("--artifact", str(command), "--type", "command",
                       "--root", str(commands), "--manifest", str(manifest),
                       "--snapshot", "--json")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(json.loads(snap.stdout)["scope"],
                         ["foo.md", "foo-validator"])

        (commands / "foo-validator").mkdir()
        validator = commands / "foo-validator" / "validate_handoffs.py"
        validator.write_text("import sys\n")
        command.write_text("# foo, improved\n")

        run = run_cli("--manifest", str(manifest), "--verify", "--json",
                      "--declared", str(command), "--declared", str(validator))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["violations"], [])

    def test_a_sibling_that_is_not_the_validator_is_still_a_violation(self):
        """The widening is one named directory, not the commands folder."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        commands = root / "commands"
        commands.mkdir()
        command = commands / "foo.md"
        command.write_text("# foo\n")
        other = commands / "bar.md"
        other.write_text("someone else's command\n")
        manifest = root / "m.json"

        snap = run_cli("--artifact", str(command), "--type", "command",
                       "--root", str(commands), "--manifest", str(manifest),
                       "--snapshot", "--json")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        other.write_text("clobbered\n")
        run = run_cli("--manifest", str(manifest), "--verify", "--json",
                      "--declared", str(other))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["violations"], ["bar.md"])


class TestASymlinkedArtifactDirectoryIsUsable(unittest.TestCase):
    """Test the walk and _under_root with an explicitly supplied watch root.

    Preserve the installed symlink's path while resolving root aliases such as
    /tmp and /private/tmp. Root derivation is covered separately by
    TestTheSymlinkedInstallLayoutEndToEnd.
    """

    def setUp(self):
        self.checkout = Path(tempfile.mkdtemp())
        (self.checkout / "hone").mkdir()
        (self.checkout / "hone" / "SKILL.md").write_text("v1\n")
        self.root = Path(tempfile.mkdtemp())
        (self.root / "hone").symlink_to(self.checkout / "hone")
        (self.root / "other.md").write_text("a sibling artifact\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.checkout, ignore_errors=True)

    def test_an_in_scope_edit_through_the_symlink_is_clean(self):
        state = check_scope.snapshot_state(self.root)
        (self.root / "hone" / "SKILL.md").write_text("v2\n")
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "hone/SKILL.md"))
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["declared_outside_root"], [])
        self.assertEqual(report["modified_in_scope"], ["hone/SKILL.md"])

    def test_an_out_of_scope_edit_through_the_symlink_is_a_violation(self):
        state = check_scope.snapshot_state(self.root)
        (self.root / "other.md").write_text("clobbered\n")
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "other.md"))
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertEqual(report["violations"], ["other.md"])


class TestTheSymlinkedInstallLayoutEndToEnd(unittest.TestCase):
    """Derive the watch from the install directory, not a symlink's checkout.

    Use the CLI with --artifact/--type and inspect its output; supplying a root
    manually cannot catch derivation that hides sibling installations.
    """

    def setUp(self):
        self.checkout = Path(tempfile.mkdtemp())
        skill = self.checkout / "skills" / "hone"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill v1\n")
        (self.checkout / "unrelated.md").write_text("checkout file v1\n")
        if not init_repo(self.checkout):  # pragma: no cover - git is in CI
            self.skipTest("git unavailable")
        self.install = Path(tempfile.mkdtemp()) / "skills"
        (self.install / "other").mkdir(parents=True)
        (self.install / "other" / "SKILL.md").write_text("a sibling skill v1\n")
        (self.install / "hone").symlink_to(skill)
        self.artifact = self.install / "hone" / "SKILL.md"
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.checkout, ignore_errors=True)
        shutil.rmtree(self.install.parent, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self):
        return run_cli("--artifact", str(self.artifact), "--type", "skill",
                       "--manifest", str(self.manifest), "--snapshot", "--json")

    def _verify(self, *declared: str):
        args = ["--manifest", str(self.manifest), "--verify", "--json"]
        for path in declared:
            args += ["--declared", path]
        if not declared:
            args.append("--declared-none")
        return run_cli(*args)

    def test_the_watch_root_is_the_install_dir_not_the_checkout(self):
        snap = self._snapshot()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = json.loads(self.manifest.read_text())
        self.assertEqual(payload["root"], os.path.realpath(self.install))
        self.assertEqual(payload["scope"], ["hone"])

    def test_an_undeclared_sibling_edit_is_not_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.install / "other" / "SKILL.md").write_text("clobbered\n")
        run = self._verify()
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["unattributed_out_of_scope"],
                         ["other/SKILL.md"])

    def test_a_declared_sibling_edit_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.install / "other" / "SKILL.md").write_text("clobbered\n")
        run = self._verify(str(self.install / "other" / "SKILL.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertEqual(report["violations"], ["other/SKILL.md"])

    def test_the_artifacts_own_edit_through_the_symlink_is_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        self.artifact.write_text("skill v2\n")
        run = self._verify(str(self.artifact))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["modified_in_scope"], ["hone/SKILL.md"])
        self.assertEqual(report["declared_outside_root"], [])

    def test_the_checkout_the_symlink_points_into_is_not_watched(self):
        """Watch the artifact through its install symlink, not unrelated checkout files."""
        self.assertEqual(self._snapshot().returncode, 0)
        (self.checkout / "unrelated.md").write_text("checkout file v2\n")
        run = self._verify()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["verdict"], "clean")


class TestTheBudgetIsSpentInGitModeToo(unittest.TestCase):
    """Enforce --max-files on git-mode hashing as well as walks and opaque
    subtrees; an oversized outer snapshot must fail before writing its manifest.
    """

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "skills").mkdir()
        (self.repo / "skills" / "s.md").write_text("v1\n")
        if not init_repo(self.repo):  # pragma: no cover - git is in CI
            self.skipTest("git unavailable")
        self.workdir = Path(tempfile.mkdtemp())
        self.manifest = self.workdir / "m.json"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _snapshot(self, budget: str):
        return run_cli("--root", str(self.repo), "--scope", "skills",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", budget)

    def test_a_root_over_budget_bails_instead_of_hashing_it_all(self):
        (self.repo / "dirty.md").write_text("untracked\n")
        run = self._snapshot("0")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("--max-files 0", run.stderr)
        self.assertFalse(self.manifest.exists())

    def test_a_clean_repository_costs_nothing_and_fits_any_budget(self):
        """The budget is the hashing workload, not the size of the tree."""
        run = self._snapshot("0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["files_recorded"], 0)

    def test_a_root_within_budget_still_snapshots(self):
        (self.repo / "dirty.md").write_text("untracked\n")
        run = self._snapshot("5")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["mode"], "git")


if __name__ == "__main__":
    unittest.main()
