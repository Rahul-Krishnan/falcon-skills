#!/usr/bin/env python3
"""Tests for check_scope.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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


def walk_state(root: Path, exclude=None) -> dict:
    """A walk-mode manifest payload, as `--snapshot` writes outside a repo."""
    return {"mode": "walk", "files": build_manifest(root, exclude)}


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
    """`--root` and `--scope` are independent knobs and derive differently.

    The docs used to derive both from one dirname chain
    (`SCOPE_ROOT=$(dirname $ARTIFACT_DIR)`, `SCOPE_NAME=$(basename ...)`),
    which is wrong in opposite directions for the two artifact shapes: it made
    a hook's watch too wide (`~/.claude` instead of `~/.claude/hooks`) and its
    scope too permissive (every hook in the directory), while making a skill's
    watch too narrow to see a sibling command directory or a repo-root script.
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

    def test_outside_a_repo_the_root_is_the_install_dir(self):
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        artifact = hooks / "foo.sh"
        artifact.write_text("#!/bin/sh\n")
        root, fallback = derive_root(Path(os.path.realpath(artifact)), "hook")
        self.assertIsNone(fallback)
        self.assertEqual(os.path.realpath(root), os.path.realpath(hooks))


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
        state = walk_state(self.root)
        # Edit only in-scope; workout/ is untouched by this "run".
        (self.root / "hone" / "SKILL.md").write_text("hone v2")

        real_git = check_scope._git_changed
        # Simulate a repo where workout/ was already uncommitted before we ran.
        check_scope._git_changed = lambda root: ["hone/SKILL.md", "workout/SKILL.md"]
        try:
            report = verify(self.root, state, ["hone"])
        finally:
            check_scope._git_changed = real_git

        self.assertEqual(report["violations"], [])
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("workout/SKILL.md", report["preexisting_dirty_out_of_scope"])

    def test_real_out_of_scope_edit_still_violates(self):
        state = walk_state(self.root)
        (self.root / "workout" / "SKILL.md").write_text("WE ACTUALLY CHANGED THIS")
        report = verify(self.root, state, ["hone"])
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
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["modified_in_scope"], ["allowed/f.md"])

    def test_out_of_scope_edit_is_a_violation(self):
        (self.root / "other" / "g.md").write_text("changed")
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/g.md", report["violations"])

    def test_untracked_new_file_out_of_scope_is_caught(self):
        # The case a git diff alone would miss entirely.
        (self.root / "other" / "new.md").write_text("surprise")
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("other/new.md", report["violations"])

    def test_new_file_in_scope_is_reported_not_violated(self):
        (self.root / "allowed" / "new.md").write_text("expected")
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "clean")
        self.assertIn("allowed/new.md", report["new_files_in_scope"])

    def test_deletion_out_of_scope_is_a_violation(self):
        (self.root / "other" / "g.md").unlink()
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "scope_violation")

    def test_no_change_is_clean(self):
        report = verify(self.root, self.state, ["allowed"])
        self.assertEqual(report["verdict"], "clean")

    def test_manifest_exclusion_prevents_self_detection(self):
        manifest_file = self.root / "m.json"
        manifest_file.write_text("{}")
        report = verify(self.root, self.state, ["allowed"],
                        exclude={manifest_file.resolve()})
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
        # Guarded tree sits one level below the repo root, which is the shape
        # --max-files produces when it narrows a repo watch back down to the
        # install directory.
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
        report = verify(self.root, state, ["hone"])
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["preexisting_dirty_out_of_scope"], ["other/g.md"])
        self.assertNotIn("hone/SKILL.md", report["preexisting_dirty_out_of_scope"])


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
        if not init_repo(self.repo):  # pragma: no cover - git is present in CI
            self.skipTest("git unavailable")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_a_directory_created_by_this_run_is_not_preexisting(self):
        state = check_scope.snapshot_state(self.root)
        (self.root / "newdir").mkdir()
        (self.root / "newdir" / "x.txt").write_text("written by this run\n")
        report = verify(self.root, state, ["hone"])
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
    """An explicit --verify --root that disagrees with the manifest exits 2.

    `--verify` normally reads the root back out of the manifest, precisely so
    the executor never has to reproduce it across two tool calls. When a caller
    overrides it anyway, a mismatch must not be treated as data: under a
    different root every recorded file is "removed" and every present file is
    "added", and the documented response to a violation is to revert the listed
    paths.
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
                             str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)

    def test_a_spelling_difference_for_the_same_directory_is_not_a_mismatch(self):
        run_cli("--root", str(self.root), "--manifest", str(self.manifest),
                "--snapshot")
        respelled = str(self.root) + "/./hone/.."
        verify_run = run_cli("--root", respelled, "--manifest",
                             str(self.manifest), "--scope", "hone", "--verify")
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)


class TestSkillInsideARepository(unittest.TestCase):
    """r2-S8: a skill's watch must reach the whole repository.

    `SCOPE_ROOT=$(dirname $ARTIFACT_DIR)` gave `plugins/p/skills` for a skill
    at `plugins/p/skills/s/`, a tree that cannot see `plugins/p/commands/`, a
    sibling plugin, or a repo-root `scripts/` -- the three collateral-damage
    cases the script exists to catch. Every one of them verified `clean`.
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        """Acceptance 5: no pre-image stored, so the change is seen, not blamed.

        Git calling a file clean at snapshot and dirty at verify proves it
        changed during the round. It does not prove this run changed it -- the
        user's editor and a second session in the same checkout look identical
        -- so the change reaches the report without reaching the revert list.
        """
        self.assertEqual(self._snapshot().returncode, 0)
        payload = json.loads(self.manifest.read_text())
        self.assertEqual(payload["mode"], "git")
        self.assertEqual(payload["dirty_tracked"], {})
        self.assertEqual(payload["untracked"], {})
        self.assertNotIn("lib/shared.md", json.dumps(payload))

        (self.repo / "lib" / "shared.md").write_text("clean v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("scripts/ignored-out.py", json.loads(run.stdout)["violations"])

    def test_a_clean_tracked_file_deleted_out_of_scope_is_unattributed(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").unlink()
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 3, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertEqual(report["unattributed_out_of_scope"], ["lib/shared.md"])
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["counts"]["removed"], 1)

    def test_a_dirty_file_deleted_out_of_scope_is_a_violation(self):
        """The manifest holds a pre-image here, so the change is attributable."""
        (self.repo / "lib" / "shared.md").write_text("dirty before the run\n")
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").unlink()
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertEqual(report["violations"], ["lib/shared.md"])
        self.assertEqual(report["unattributed_out_of_scope"], [])

    def test_preexisting_dirty_is_reported_and_not_violated(self):
        """Acceptance 4: dirty before the run, untouched by it."""
        (self.repo / "lib" / "shared.md").write_text("someone else's uncommitted\n")
        self.assertEqual(self._snapshot().returncode, 0)
        payload = json.loads(self.manifest.read_text())
        self.assertIn("lib/shared.md", payload["dirty_tracked"])

        (self.skill / "SKILL.md").write_text("skill v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], ["lib/shared.md"])

    def test_editing_a_preexisting_dirty_file_is_still_a_violation(self):
        """The hash is the only thing separating "already dirty" from "we did it"."""
        (self.repo / "lib" / "shared.md").write_text("someone else's uncommitted\n")
        self.assertEqual(self._snapshot().returncode, 0)
        (self.repo / "lib" / "shared.md").write_text("and then WE changed it\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["lib/shared.md"])
        self.assertEqual(report["preexisting_dirty_out_of_scope"], [])

    def test_verify_needs_only_the_manifest(self):
        """Acceptance 6: no --root, no --scope, no re-derivation by the caller."""
        self.assertEqual(self._snapshot().returncode, 0)
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["scope"], ["plugins/p/skills/s"])
        self.assertEqual(os.path.realpath(report["root"]),
                         os.path.realpath(self.repo))

    def test_max_files_narrows_the_root_and_says_so(self):
        """Acceptance 7: a silent narrowing is the failure this PR is about."""
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

        run = run_cli("--manifest", str(self.manifest), "--verify")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("NARROWER than intended", run.stdout)


class TestHookOutsideARepository(unittest.TestCase):
    """r6-S1: a hook's watch is its install dir and its scope is one file.

    `SCOPE_ROOT=$(dirname $ARTIFACT_DIR)` widened the watch to the whole
    `~/.claude` config tree while `SCOPE_NAME=$(basename $ARTIFACT_DIR)`
    granted permission to edit *every* hook in `hooks/` -- too broad and too
    permissive at once.
    """

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
        # settings.json lives above the watch and is nobody's business here.
        self.assertNotIn("settings.json", json.dumps(payload["files"]))

    def test_editing_the_artifact_itself_is_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.hooks / "foo.sh").write_text("#foo v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["modified_in_scope"], ["foo.sh"])

    def test_editing_a_sibling_hook_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.hooks / "bar.sh").write_text("#bar v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
    """r1-B1: a guard that cannot see must never report `clean`.

    Snapshot a repo, change two files out of scope, then make git unanswerable
    before verifying. The old code turned that into zero observed changes, a
    `clean` verdict and exit 0, which the caller records as a passing
    `scope_verify` gate on a tree nothing looked at.
    """

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
        """r1-B1: a git-mode manifest stores no whole-tree hashes.

        The old text claimed the hash manifest had been the only check, on a
        manifest that holds no such hashes: nothing at all had been checked.
        """
        shutil.rmtree(self.repo / ".git")
        run = run_cli("--manifest", str(self.manifest), "--verify")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertNotIn("hash manifest was the only check", run.stdout)
        self.assertIn("UNCHECKED", run.stdout)


class TestNestedRepositoriesAndSubmodules(unittest.TestCase):
    """r1-B4: `git status` collapses a nested repo, and a gitlink is not a file.

    `~/.claude/skills` and `~/.claude/scripts` are separate repositories inside
    the `~/.claude` repo, which is exactly the tree a run on a hook or a script
    watches, so this is the ordinary shape here rather than an exotic one.
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("nested/other.md", report["violations"])

    def test_a_new_file_in_a_nested_repository_is_a_violation(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.nested / "added.md").write_text("brand new\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("nested/added.md", json.loads(run.stdout)["violations"])

    def test_an_untouched_nested_repository_verifies_clean(self):
        self.assertEqual(self._snapshot().returncode, 0)
        (self.skill / "SKILL.md").write_text("skill v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertIn("sub/other.md", report["violations"])
        # The gitlink path itself is never reported as a removed file.
        self.assertNotIn("sub", report["violations"])

    def test_a_submodule_is_seen_when_the_root_sits_below_the_toplevel(self):
        """The two path namespaces again: `ls-files` is root-relative already.

        `git status` prints repository-toplevel-relative paths and has to be
        rebased onto the watch root; `git ls-files` prints root-relative ones
        and must not be. Rebasing both doubles the prefix whenever the watch
        root sits below the repository root, which is the documented hook and
        script invocation, and the submodule falls back out of the report.
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("sub/other.md", json.loads(run.stdout)["violations"])

    def test_an_unhashable_subtree_reports_not_measurable(self):
        snap = run_cli("--root", str(self.repo), "--scope", "skills/s",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", "0")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertIn("nested", json.loads(snap.stdout)["unmeasurable"])
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(report["not_measurable_reasons"])

    def test_an_unhashable_subtree_does_not_swallow_a_real_violation(self):
        """A partly-readable tree still reports what it did read.

        The subtree nobody could hash must not take the actionable revert list
        down with it: the caller needs both the halt and the paths.
        """
        snap = run_cli("--root", str(self.repo), "--scope", "skills/s",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", "0")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        (self.repo / "outside.md").write_text("untracked and out of scope\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["verdict"], "scope_violation")
        self.assertIn("outside.md", report["violations"])
        self.assertTrue(report["not_measurable_reasons"])


class TestPreImagesSurviveASubtreeChangingShape(unittest.TestCase):
    """The pre-image stores are partitioned by how git saw a file, not by content.

    A `git init` in a subdirectory mid-round moves ordinary untracked paths
    into an opaque nested subtree, and removing a submodule moves them back.
    A classifier reading only its own store finds no pre-image for a
    byte-identical file, calls it `added`, and the caller is told to revert a
    file nobody touched.
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json")
        report = json.loads(run.stdout)
        self.assertEqual(report["violations"], ["vendor/lib.txt"], run.stdout)
        self.assertEqual(report["counts"]["modified"], 1)


class TestUnreadableFiles(unittest.TestCase):
    """An unreadable file is the plainest "could not see", and used to be neither.

    Hashing it to the same `None` that means "absent" classified it as `added`
    and put it in the revert list, and an unreadable file at both ends compared
    equal to itself and vanished from the report entirely.
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
    """r1-S4: a lint run during a round must not halt it."""

    def test_common_caches_are_ignored(self):
        for rel in (".ruff_cache/x/0.json", ".mypy_cache/3.12/m.data.json",
                    "node_modules/pkg/index.js", ".venv/lib/site.py",
                    "htmlcov/index.html", ".tox/py312/log.txt"):
            self.assertTrue(_is_ignored(Path(rel)), rel)

    def test_source_directories_are_not_ignored(self):
        for rel in ("scripts/x.py", "src/app/main.py", "dist/bundle.js",
                    "build/out.txt", "references/notes.md"):
            self.assertFalse(_is_ignored(Path(rel)), rel)


if __name__ == "__main__":
    unittest.main()
