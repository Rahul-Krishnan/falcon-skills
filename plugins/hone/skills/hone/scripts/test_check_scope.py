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
    """A walk-mode manifest payload, as `--snapshot` writes outside a repo.

    Built the way `snapshot_state` builds it, budget and unreadable-path list
    included. Hand-assembling the dict here let the helper drift from the real
    thing: it omitted `max_files`, and the test that needed the budget spliced
    the field in itself, which hid the fact that `snapshot_state` was not
    writing one.
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
    """The declaration a run makes when it wrote exactly `paths`.

    Root-relative spellings in, resolved paths out, so a test states what its
    simulated run wrote in the same terms the report uses.
    """
    return check_scope.normalize_declared([str(root / p) for p in paths], root)


# A run that wrote nothing at all: present, and empty. Not the same value as
# the absence of a declaration, which is what `verify(...)` defaults to.
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
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "hone/SKILL.md"))
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
        report = verify(self.root, state, ["hone"],
                        declared=declaring(self.root, "newdir/x.txt"))
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
        """Declared, so attributable, and flagged as needing a manual undo.

        The file was already dirty when the run began, so restoring it with
        `git checkout` would take the earlier uncommitted work with it. The
        attribution is sound; only the remedy differs, which is what
        `violations_manual_revert` carries.
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
        """r2-S3: the budget bounds the hashing, and a clean repo hashes nothing.

        `--max-files` used to be compared against the repository's tracked
        count, so any monorepo over the limit had its watch narrowed to the
        install directory. In git mode the snapshot hashes only dirty and
        untracked files, so narrowing saved no work at all and dropped exactly
        the coverage the wide root exists for.
        """
        snap = self._snapshot("--max-files", "1")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        report = json.loads(snap.stdout)
        self.assertNotIn("root_fallback", report)
        self.assertEqual(os.path.realpath(report["root"]),
                         os.path.realpath(self.repo))

        # And the wide watch still does its job.
        (self.repo / "plugins" / "p" / "commands" / "c.md").write_text("cmd v2\n")
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared",
                      str(self.repo / "plugins" / "p" / "commands" / "c.md"))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(json.loads(run.stdout)["violations"],
                         ["plugins/p/commands/c.md"])

    def test_max_files_narrows_the_root_and_says_so(self):
        """Acceptance 7: a silent narrowing is the failure this PR is about.

        The budget is spent on files that must actually be hashed, so it takes
        real uncommitted work to exceed it.
        """
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared",
                      str(self.repo / "skills" / "sub" / "other.md"))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("sub/other.md", json.loads(run.stdout)["violations"])

    def test_an_unhashable_subtree_reports_not_measurable(self):
        snap = run_cli("--root", str(self.repo), "--scope", "skills/s",
                       "--manifest", str(self.manifest), "--snapshot", "--json",
                       "--max-files", "0")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertIn("nested", json.loads(snap.stdout)["unmeasurable"])
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json", "--declared-none")
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
        run = run_cli("--manifest", str(self.manifest), "--verify", "--json",
                      "--declared", str(self.repo / "outside.md"))
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


class TestAttributionComesFromTheDeclaration(unittest.TestCase):
    """r2-B2/r2-B3: the revert path used to fire in exactly the wrong case.

    Attribution was inferred from whether the snapshot held a pre-image, and a
    pre-image only proves a file changed DURING the round. Measured on the tree
    this replaces:

      hone edits a clean tracked sibling   -> not_measurable, violations []
      the user's own WIP, edited by them   -> scope_violation, violations [it]

    So the `git checkout` the caller is told to run never fired for the case
    the guard exists for, and fired only where attribution was unsound. Both
    directions are pinned here.
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
        """Direction A: the case the guard exists for, on a CLEAN tracked file."""
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
        """Direction B: dirty at snapshot, changed again by the USER."""
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
        """r2-B3: the same hole for untracked files, where a revert means delete."""
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
        """An incomplete declaration is the one lie this check can catch."""
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
    """r1's subject again: "cannot see" must never render as `clean`.

    A run that will not say what it wrote cannot be told it stayed in scope,
    so an absent declaration is `not_measurable` on its own -- including on a
    tree where nothing changed at all, since the guard has no way to know the
    run did not write somewhere it cannot observe.
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
    """How a declaration is spelled, and what happens when it is spelled badly."""

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
        """`--declared-none` reached by accident is the one inference to refuse."""
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
    """r2-S1: an unreadable manifest mode reported the whole tree as violations.

    `mode` missing or unrecognized fell through to the walk branch, where an
    absent `files` map made every file under the root `added`. The caller is
    told to revert what `violations` lists.
    """

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
    """r2-S4: exit 1 means `scope_violation`, and a traceback used to exit 1 too.

    The caller's branch table answers exit 1 by reverting the paths under
    `violations`, and a crash prints no report to read them from.
    """

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
    """r2-N1: walk-mode verify hashed without a limit while every other walk had one."""

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
        """r3-N1: the budget has to survive in the state, not just the CLI.

        `verify` reads `state["max_files"]`, so a walk-mode snapshot that did
        not store one had every in-process verify silently fall back to the
        20000 default -- a tree snapshotted under a tight budget and grown
        past it was re-hashed without bound instead of reported.
        """
        state = check_scope.snapshot_state(self.root, max_files=7)
        self.assertEqual(state["mode"], "walk")
        self.assertEqual(state["max_files"], 7)


class TestASubtreeBecomingOpaqueInventsNothing(unittest.TestCase):
    """r2-N2: a file swept into an opaque subtree must not be invented as `added`.

    `_classify_nested` builds its pre-image view from the flat map, which holds
    nothing for a file that was clean and tracked at snapshot -- by design,
    since git was attributing those on its own. Reading "absent from the
    pre-images" as `added` manufactures a change for a file nobody touched.

    Two things answer it. The declaration is the load-bearing one: an `added`
    path the run never declared cannot reach `violations` at all, so no revert
    instruction rides on the mistake any more. The guard below is the narrower
    one, and it stops the bogus `added` being reported in the first place.

    Reaching it through real git is hard on purpose: git declines to collapse a
    directory whose files the outer index still tracks, so the two conditions
    (tracked at snapshot, opaque at verify) do not co-occur in today's git. The
    classifier is therefore exercised directly rather than through a scenario
    that quietly stops reproducing.
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
    """r3-B1: the fail-open invariant, asserted over the enumerated set.

    Four doors on this class were closed one at a time (git unanswerable,
    nested repositories, registered submodules, unreadable files) and a fifth
    was still open: `_walk` dropped anything that was neither
    `is_dir(follow_symlinks=False)` nor `is_file()`, which is every symlinked
    directory, every dangling symlink, and every socket and fifo, with no
    record anywhere. So this does not test the symlink example. It enumerates
    every way an entry under the watch root can fail to be read or classified
    -- cases 4 to 8 of the module docstring -- and asserts the invariant the
    docstring states over the whole set: an out-of-scope path the guard could
    not read never comes back `clean`.

    Each case builds the same tree shape: an in-scope `hone/` the run is
    allowed to touch, and one awkward entry beside it, out of scope.
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

    # -- case 4: a directory that cannot be listed ------------------------
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

    # -- case 5: an entry whose type cannot be determined -----------------
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

    # -- case 6: a dangling symlink --------------------------------------
    def test_a_dangling_symlink_is_recorded(self):
        (self.root / "gone.md").symlink_to(self.root / "never-existed.md")
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("gone.md" in reason
                            for reason in report["not_measurable_reasons"]))

    # -- case 7: an entry that is not a regular file ----------------------
    def test_a_fifo_is_recorded(self):
        if not hasattr(os, "mkfifo"):  # pragma: no cover - POSIX only
            self.skipTest("os.mkfifo unavailable")
        os.mkfifo(self.root / "pipe")
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("pipe" in reason
                            for reason in report["not_measurable_reasons"]))

    # -- case 8: a symlinked directory looping onto its own descent -------
    def test_a_symlink_loop_is_recorded_not_followed(self):
        (self.root / "loop").symlink_to(self.root)
        report = self._verdict()
        self.assertEqual(report["verdict"], "not_measurable")
        self.assertTrue(any("loop" in reason
                            for reason in report["not_measurable_reasons"]))

    # -- the case that is deliberately NOT unmeasurable -------------------
    def test_a_symlinked_directory_is_walked_rather_than_recorded(self):
        """The reported case, and the reason the fix is not "record it too".

        `~/.claude/skills/{name}` is routinely a symlink into a repository
        checkout, so declining to read symlinked directories would report
        `not_measurable` on every ordinary walk-mode run. They are followed
        instead, which is what makes the change inside one visible at all.
        """
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
        """The control. The invariant must not be satisfied by never saying clean."""
        report = self._verdict()
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(report["not_measurable_reasons"], [])


class TestUnmeasurableRecordsSurviveTheManifest(unittest.TestCase):
    """The record has to reach --verify, which reads it back out of JSON."""

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
    """r3-B2: Phase 2 Step 6 must not write a file Step 6a orders deleted.

    Step 6 mandates a companion `validate_handoffs.py` and mandates declaring
    every file written, including that one. For a command the only in-scope
    path was the command file itself, so the validator was declared and out of
    scope, which is the definition of a real violation: Step 6a ordered the
    run to delete the validator it had just been required to create, and halt.
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
    """The ordinary `~/.claude/skills` layout, end to end.

    `~/.claude/skills/{name}` is normally a symlink into a repository
    checkout. Walking follows it, so the detected paths are spelled through
    the symlink (`hone/SKILL.md`). A declaration is spelled the same way,
    because that is the path the executor edited -- but `realpath` on it lands
    inside the checkout, outside the watch root entirely. Resolving both ends
    with `realpath` therefore reports every declared path as outside the root
    and every run as `not_measurable`; resolving neither reintroduces the
    `/tmp` against `/private/tmp` mismatch. `_under_root` resolves as far as
    the root and no further.
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
