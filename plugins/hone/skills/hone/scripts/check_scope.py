#!/usr/bin/env python3
"""Scope guard for hone Phase 2 edits.

Preference 11 (stale-write protection) stops hone from clobbering a file
someone else changed. Nothing stops hone from changing a file it was never
asked to touch: a shared reference, a sibling skill, a script two directories
over. Those edits are invisible in the run report and land in the working tree
next to the intended ones.

Git alone is not enough. A file git does not track produces no diff, so an
edit to an untracked file inside or outside scope is silent. This script
therefore pairs git's own accounting (for tracked files) with a content-hash
manifest (for the files git cannot attribute), which is the arrangement
trailofbits/skills skill-improver arrived at for the same reason.

Two independent knobs:

  root   the tree being watched -- everything that could plausibly be
         collateral damage.
  scope  the subset of that tree this run is permitted to change.

They are derived from the artifact rather than from a dirname chain in the
caller's shell, because the two derivations pull in opposite directions and a
single chain cannot serve both. A hook at `~/.claude/hooks/foo.sh` needs a
narrow root (`~/.claude/hooks`) and a narrower scope (that one file), while a
skill at `<repo>/plugins/p/skills/s/` needs a *wide* root (the repository, so
edits to `plugins/p/commands/` or a repo-root `scripts/` are visible) and a
scope of just its own directory. Passing `--artifact` and `--type` lets the
script answer both, and `--verify` reads root and scope back out of the
manifest so the caller never has to reproduce them across two tool calls.

Two phases, both read-only with respect to the artifact:

  --snapshot  Record the pre-edit state of the repository into a manifest.
              Run before Phase 2 applies any edit.
  --verify    Compare the current state against the manifest and report every
              change outside the declared scope, plus untracked files that
              appeared inside scope without being registered.

Detection and attribution are two different questions, and only the first one
the tree can answer. Comparing a snapshot against the live tree shows that a
file changed DURING the round; it can never show that THIS run is what changed
it, because a second session, the user's own editor, and a build step all
leave the same marks. Inferring attribution from the diff got it backwards in
practice: an out-of-scope file the run really did edit came back
`not_measurable` while a file the user was editing themselves came back
`scope_violation`, whose documented response is `git checkout` of the listed
paths.

Attribution therefore comes from the run's own declaration of what it wrote
(`--declared`, `--declared-file`, `--declared-none`), never from the diff. An
out-of-scope change the run declared is one this run caused, so it is a
`violation` and is safe to revert. An out-of-scope change it did not declare
belongs to somebody else: it is reported under `unattributed_out_of_scope` and
the round halts, with no destructive instruction riding on a guess.

A verify has three possible answers, not two. "I could not see" is the third,
and collapsing it into `clean` is the one failure mode a safety check must not
have: the caller records a passing `scope_verify` gate and the run proceeds as
though the tree had been checked. Every path that cannot answer therefore
reports `not_measurable` and exits 3, following the vocabulary
`check_overfit.py` and `check_eval_power.py` already use for the same
distinction. Four cases reach it: git stops answering between snapshot and
verify, a nested repository or submodule the guard could not hash, an
out-of-scope change this run did not declare, and a `--verify` given no
declaration at all. That last one is deliberately blunt: a run that will not
say what it wrote cannot be told it stayed in scope.

Exit codes: 0 clean, 1 scope violation, 2 usage error (the check never ran),
3 not_measurable (the check ran and could not answer). Only 0 is a pass.

Stdlib only. Never modifies tracked content; the manifest is the only file it
writes, and only under --snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

# Files that change as a side effect of normal tool use and would otherwise
# produce a violation on every run.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", ".coverage"}

# Directories holding tool output rather than source. A lint, type-check, or
# coverage run *during* a round writes into these, and `--ignored=traditional`
# lists such a file individually whenever the `.gitignore` entry names it one
# by one instead of collapsing the whole directory. Without this list an
# incidental `ruff` invocation halts the round with a scope violation the run
# had no way to avoid. Only caches and dependency trees belong here: a
# directory that can hold hand-written source does not, which is why `dist`
# and `build` are absent.
#
# Every name here matches on ANY path component, so a name that could plausibly
# appear in an artifact's own install path would make that artifact invisible
# and the verify `clean`. Bare `venv` and `.cache` were dropped for exactly
# that reason; the rest are distinctive enough not to collide. `verify` also
# reports a scope that lands under one of these as `not_measurable` rather than
# clean, so a future collision fails loudly instead of silently.
IGNORED_PARTS = {
    "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".pytype", ".tox", ".nox", ".hypothesis", "htmlcov", ".ipynb_checkpoints",
    "node_modules", ".venv", ".gradle", ".terraform",
    ".next", ".nuxt", ".turbo", ".parcel-cache",
}

# Backup and working files hone and workout create by design.
IGNORED_SUFFIXES = (".pyc", ".pre-hone", ".pre-workout", ".pre-audit", ".pre-enrich")

ARTIFACT_TYPES = ("skill", "command", "hook", "script")

# One place the caller's branch table is defined. `not_measurable` gets its own
# code rather than sharing 1 with `scope_violation`, because the two demand
# opposite responses: a violation says revert the listed paths, and
# not_measurable says revert nothing because nothing here knows what to revert.
VERDICT_EXITS = {"clean": 0, "scope_violation": 1, "not_measurable": 3}

DEFAULT_MAX_FILES = 20000

# Sentinel for "this file existed at snapshot time and was clean in git, so no
# hash was stored". Git alone shows such a file changed during the round, which
# is all a pre-image would have shown too; who changed it comes from the
# declaration either way.
CLEAN_TRACKED = object()


class TooManyFiles(Exception):
    """The watched tree is larger than --max-files and cannot be narrowed."""

    def __init__(self, count: int, limit: int):
        super().__init__(f"{count} files exceeds --max-files {limit}")
        self.count = count
        self.limit = limit


def _is_ignored(path: Path) -> bool:
    if path.name in IGNORED_NAMES:
        return True
    if any(part in IGNORED_PARTS for part in path.parts):
        return True
    return any(path.name.endswith(suffix) for suffix in IGNORED_SUFFIXES)


# A file that exists but could not be read. Distinct from `None`, which means
# the file was not there at all: conflating the two classified an unreadable
# file as `added` and put it in the revert list, and made an unreadable file
# that stayed unreadable compare equal to itself and vanish from the report.
# It is a string so it survives the JSON round-trip through the manifest, and
# one no sha256 digest can collide with.
UNREADABLE = "unreadable"


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return UNREADABLE


def _real(path: Path) -> str:
    """Resolved path as a string, tolerant of paths that do not exist."""
    return os.path.realpath(str(path))


def _walk(root: Path, exclude: set[Path] | None = None,
          limit: int | None = None) -> list[Path]:
    """Every non-ignored file under root, without descending noise directories.

    `rglob("*")` descends `.git` and `__pycache__` in full and only then
    discards what it found, which on a repository root costs orders of
    magnitude more than the walk itself. Prune at the directory boundary
    instead.
    """
    excluded = {_real(p) for p in (exclude or set())}
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in IGNORED_PARTS:
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            if _is_ignored(path.relative_to(root)):
                continue
            if _real(path) in excluded:
                continue
            found.append(path)
            if limit is not None and len(found) > limit:
                raise TooManyFiles(len(found), limit)
    return found


def build_manifest(root: Path, exclude: set[Path] | None = None,
                   limit: int | None = None) -> dict:
    """Hash every non-ignored file under root.

    `exclude` holds paths to skip. The manifest itself belongs there: writing
    it inside the guarded tree would otherwise register as a new out-of-scope
    file on the very next verify.
    """
    return {
        str(p.relative_to(root)): _hash_file(p)
        for p in _walk(root, exclude, limit)
    }


def _git(root: Path, *args: str) -> str | None:
    """Run git in `root` and return stdout, or None when git cannot answer.

    Bytes, decoded the way the filesystem decodes them, rather than
    `text=True`. `text=True` decodes strictly under the locale encoding, and
    `git ls-files -z` emits raw path bytes (unlike `git status --porcelain`,
    which C-quotes them). One non-UTF-8 filename anywhere under the root
    therefore raised `UnicodeDecodeError` out of the middle of the guard and
    exited 1 -- the code the caller's branch table reads as "scope violation,
    revert the listed paths", with no report to list any. `surrogateescape` is
    what `os.fsdecode` uses, so the decoded string round-trips back to the same
    bytes on disk and `Path` operations on it work.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return os.fsdecode(result.stdout)


def _git_toplevel(root: Path) -> Path | None:
    """Repository root containing `root`, or None when root is not in a repo."""
    out = _git(root, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    line = out.strip()
    return Path(line) if line else None


def _nul_list(stdout: str | None) -> list[str]:
    return [] if stdout is None else [p for p in stdout.split("\0") if p]


def _git_tracked(root: Path) -> set[str] | None:
    """Index paths under `root`, relative to `root`.

    `git ls-files` respects the working directory, so `-C root` already scopes
    the answer to the watched tree and prints root-relative paths -- unlike
    `git status --porcelain`, which always prints repository-root-relative
    paths and has to be rebased by hand below.
    """
    out = _git(root, "ls-files", "-z")
    if out is None:
        return None
    return set(_nul_list(out))


def _git_gitlinks(root: Path) -> list[str] | None:
    """Registered submodules under `root`, as root-relative paths.

    A submodule is a gitlink: one index entry with mode 160000 whose content
    lives in another repository. `git status` reports it as a single path with
    no trailing slash, and `Path.is_file()` calls it absent, so the classifier
    used to bucket a content change inside a submodule as `removed` and tell
    the caller to revert a directory the manifest holds no content for.
    """
    out = _git(root, "ls-files", "--stage", "-z")
    if out is None:
        return None
    links: list[str] = []
    for record in _nul_list(out):
        # `<mode> <sha> <stage>\t<path>`. `-z` output is never C-quoted, so it
        # must not be unquoted here: `_git_tracked` does not either, and a path
        # that happens to start and end with a quote would otherwise be spelled
        # two different ways by two functions whose results are compared.
        head, _, path = record.partition("\t")
        if head.startswith("160000") and path:
            links.append(path)
    return links


def _git_hash_candidates(root: Path) -> int | None:
    """How many files a git-mode snapshot would actually hash under `root`.

    This is the cost estimate `--max-files` is compared against, and it is
    deliberately not the size of the tree. `snapshot_state` stores nothing for
    the clean-tracked majority, because git attributes those on its own, so the
    cost of watching a repository scales with its dirty and untracked files
    rather than with how many files it tracks.

    Comparing the limit against the tracked count instead narrowed the watch on
    any repository over 20000 files while saving no work whatsoever, and the
    coverage it dropped was the whole reason the root is wide: a sibling
    `commands/` directory, a repo-root `scripts/`, a sibling plugin. A monorepo
    got a permanently degraded guard in exchange for nothing.

    Opaque subtrees count as one apiece here. They are hashed in full, but
    `_hash_opaque` passes the same budget down to each one and records the
    subtrees that blow it under `unmeasurable`, so their cost is already
    bounded where it is actually incurred.
    """
    status = _git_status(root)
    if status is None:
        return None
    entries, opaque = status
    hashed = [rel for _code, rel in entries if not _is_ignored(Path(rel))]
    return len(hashed) + len(opaque)


def _git_status(root: Path) -> tuple[list[tuple[str, str]], list[str]] | None:
    """`((status code, root-relative path), ...)` plus the paths git cannot see into.

    The second list is the one that stops this from being a fail-open check.

    `git status --porcelain` prints paths relative to the *repository* root,
    never to `--root`. The manifest, `--scope`, and `_in_scope` all speak
    root-relative paths, so comparing the two namespaces directly matched
    nothing whenever root sits below the repo root -- which is a documented
    invocation (a hook watched at `~/.claude/hooks` inside a repo rooted at
    `~/.claude`). Every dirty file then looked both unchanged by this run and
    out of scope, so the report listed all of them, including the in-scope
    file the run had just legitimately edited, under
    `preexisting_dirty_out_of_scope` next to "do not revert them".

    Rebase each path onto `root` and drop the ones that fall outside it.

    `--untracked-files=all` is not decoration. The default `normal` collapses
    a wholly untracked directory to one entry, `newdir/`, which never matches
    the file-level paths in the hash manifest. A directory this run created
    therefore passed the `not in changed` test and was reported as
    pre-existing dirt, while the files inside it were reported as violations:
    the caller was told to revert `newdir/x.txt` and, in the same report, that
    `newdir` predates the run and must not be reverted.

    `--ignored=traditional` brings back individually-listed `.gitignore`d
    files, which the old whole-tree rglob used to hash and plain `status`
    hides entirely. Wholly-ignored *directories* still collapse to one entry
    and are skipped -- that is the point, since they are the `node_modules`
    and `.venv` trees this rewrite exists to stop walking.

    A collapsed entry that is NOT ignored is a different animal and used to be
    dropped by the same branch. `--untracked-files=all` expands every ordinary
    untracked directory, so the only thing left collapsing is a directory git
    declines to look inside: a nested repository. Dropping it made every edit
    within that repository invisible and the verdict `clean`. Those paths come
    back as the second return value, and the caller hashes them itself or says
    it could not.
    """
    stdout = _git(root, "status", "--porcelain", "--untracked-files=all",
                  "--ignored=traditional")
    if stdout is None:
        # Older git spells it `--ignored` with no value; older still lacks it.
        stdout = _git(root, "status", "--porcelain", "--untracked-files=all")
    if stdout is None:
        return None
    toplevel = _git_toplevel(root)
    if toplevel is None:
        return None
    root_resolved = _real(root)
    toplevel_resolved = Path(_real(toplevel))

    entries: list[tuple[str, str]] = []
    opaque: list[str] = []
    for line in stdout.splitlines():
        if len(line) <= 3:
            continue
        code = line[:2]
        entry = _porcelain_path(line)
        collapsed = entry.endswith("/")
        try:
            relative = (toplevel_resolved / entry).relative_to(root_resolved)
        except ValueError:
            # Dirty, but outside the guarded tree entirely. Not this run's
            # business either way, and reporting it would be noise.
            continue
        if collapsed:
            # An ignored tree is collapsed on purpose and stays skipped. Any
            # other collapsed directory is a nested repository git will not
            # look inside, and dropping it is how edits in there went unseen.
            if code != "!!" and not _is_ignored(relative):
                opaque.append(str(relative))
            continue
        entries.append((code, str(relative)))

    gitlinks = _git_gitlinks(root)
    if gitlinks is None:
        return None
    # `ls-files` prints paths relative to its working directory, so these are
    # already root-relative and already scoped to root. Rebasing them onto the
    # repository toplevel the way the status lines above are rebased would
    # double the prefix whenever root sits below the toplevel.
    opaque.extend(link for link in gitlinks if not _is_ignored(Path(link)))
    # A submodule can also appear as a dirty entry; the opaque handling owns it.
    opaque_set = set(opaque)
    entries = [(code, rel) for code, rel in entries if rel not in opaque_set]
    return entries, sorted(opaque_set)


def _git_changed(root: Path) -> list[str] | None:
    """Root-relative paths git reports as dirty, or None outside a repo.

    `.gitignore`d entries are excluded: they are tracked for *attribution*
    (see `_git_status`) but calling them "dirty" would flood
    `preexisting_dirty_out_of_scope` with build output.
    """
    status = _git_status(root)
    if status is None:
        return None
    return [path for code, path in status[0] if code != "!!"]


_C_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"',
}


def _unquote_git_path(entry: str) -> str:
    """Decode the C-quoted path form `git status --porcelain` emits.

    Git wraps a path in double quotes and escapes it whenever it holds a
    control character, a quote, a backslash, or a non-ASCII byte, encoding
    those bytes as three-digit octal. Stripping the quotes without undoing the
    escapes leaves a literal `\303\251` that matches nothing on disk, so the
    entry falls out of the report it was supposed to appear in.
    """
    if len(entry) < 2 or not (entry.startswith('"') and entry.endswith('"')):
        return entry
    body = entry[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        nxt = body[index]
        octal = body[index:index + 3]
        if len(octal) == 3 and all(digit in "01234567" for digit in octal):
            out.append(int(octal, 8))
            index += 3
            continue
        out.extend(_C_ESCAPES.get(nxt, nxt).encode("utf-8"))
        index += 1
    # `os.fsdecode`, not `decode(..., "replace")`: replacement characters do
    # not round-trip, so a path holding a non-UTF-8 byte decoded to a name that
    # matched nothing on disk and fell out of the report. Surrogate escapes
    # re-encode to the original bytes, which is what makes `Path(...).is_file()`
    # and the comparison against `git ls-files -z` output agree.
    return os.fsdecode(bytes(out))


def _porcelain_path(line: str) -> str:
    """The on-disk path a `git status --porcelain` line refers to.

    Two shapes need handling. A rename or copy prints `old -> new`, and taking
    the whole tail gives the single nonexistent path `old -> new`, which never
    resolves under root and is dropped silently -- losing an entry from
    `preexisting_dirty_out_of_scope`, the list whose whole job is telling the
    caller what not to revert. The destination is the path that exists now, so
    that is the one to keep. The other shape is C-quoting, handled above.
    """
    entry = line[3:].strip()
    if line[:1] in ("R", "C") and " -> " in entry:
        entry = entry.split(" -> ", 1)[-1].strip()
    return _unquote_git_path(entry)


def _in_scope(rel_path: str, scope: list[str]) -> bool:
    candidate = Path(rel_path)
    for entry in scope:
        entry_path = Path(entry)
        if candidate == entry_path:
            return True
        try:
            candidate.relative_to(entry_path)
            return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------
# Derivation: artifact -> (watch root, permitted scope)
# --------------------------------------------------------------------------


def permitted_scope(artifact: Path, artifact_type: str) -> Path:
    """The one path this run may change.

    A skill is a directory (`.../{name}/SKILL.md`), so the whole directory is
    fair game -- references, scripts, evals all belong to it. A command, hook,
    or script is a single file and nothing around it is in scope: a hook's
    siblings in `~/.claude/hooks/` are other people's hooks, and permitting
    the directory would permit editing every one of them.
    """
    return artifact.parent if artifact_type == "skill" else artifact


def install_root(artifact: Path, artifact_type: str) -> Path:
    """The narrow watch root used when the artifact is not inside a repository.

    For a skill this is the directory holding all installed skills
    (`~/.claude/skills`); for a single-file artifact it is the install
    directory itself (`~/.claude/hooks`). Never the parent of that, which for
    a hook would widen the watch to the whole `~/.claude` config tree.
    """
    container = artifact.parent
    return container.parent if artifact_type == "skill" else container


def derive_root(artifact: Path, artifact_type: str,
                max_files: int = DEFAULT_MAX_FILES) -> tuple[Path, dict | None]:
    """Watch root for `artifact`, plus a fallback record if it was narrowed.

    Inside a repository the root is the repository, so a change to a sibling
    plugin, a sibling command directory, or a repo-root `scripts/` is visible.
    That is only affordable because `snapshot_state` hashes just the files git
    cannot attribute, so the budget is compared against that hashing workload
    (`_git_hash_candidates`) and not against the size of the checkout. A
    hundred-thousand-file monorepo with a handful of dirty files keeps the wide
    watch, because widening it costs nothing there. Only when the work itself
    is too big does the root narrow to the install directory, and then it says
    so out loud.
    """
    narrow = install_root(artifact, artifact_type)
    toplevel = _git_toplevel(artifact.parent)
    if toplevel is None:
        return narrow, None
    # `artifact` arrives already resolved; put the repo root in the same
    # namespace or the `is this below that` test below compares /tmp against
    # /private/tmp and silently declines to narrow.
    toplevel = Path(_real(toplevel))

    count = _git_hash_candidates(toplevel)
    can_narrow = narrow == toplevel or toplevel in narrow.parents
    if count is not None and count > max_files and can_narrow and narrow != toplevel:
        return narrow, {
            "reason": "max_files_exceeded",
            "candidate_count": count,
            "limit": max_files,
            "intended_root": str(toplevel),
            "actual_root": str(narrow),
        }
    return toplevel, None


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def snapshot_state(root: Path, exclude: set[Path] | None = None,
                   max_files: int = DEFAULT_MAX_FILES) -> dict:
    """Pre-edit state of `root`, hashing as little as correctness allows.

    Inside a repository, git can attribute any change to a file that was clean
    when we started: if it is dirty at verify time, this run dirtied it. Only
    the files git *cannot* attribute need a pre-image:

      * tracked and already dirty -- git will call it dirty either way, so the
        hash is the only thing that separates "we changed it" from "it was
        already like that", which is the difference between reverting our own
        edit and destroying someone else's uncommitted work.
      * untracked (and individually-listed ignored files) -- invisible to
        `git diff` entirely.

    Both sets are small. The clean-tracked majority, which is the whole cost
    of the old rglob-everything manifest, is stored as nothing at all.

    The exception is a nested repository or a registered submodule. The outer
    git knows nothing about their contents, so neither "clean in git" nor
    "dirty in git" means anything there and the cheap accounting above cannot
    be applied. Those subtrees are hashed in full, exactly as walk mode hashes
    an install directory, and a subtree too large or unreadable to hash is
    recorded under `unmeasurable` rather than dropped.
    """
    excluded = {_real(p) for p in (exclude or set())}
    status = _git_status(root)
    tracked = _git_tracked(root)
    if status is None or tracked is None:
        # Not a repository (or git is unavailable): walk it. These roots are
        # install directories, which are small.
        return {
            "mode": "walk",
            "files": build_manifest(root, exclude, max_files),
        }
    entries, opaque = status

    dirty_tracked: dict[str, str | None] = {}
    untracked: dict[str, str | None] = {}
    for _code, rel in entries:
        if _is_ignored(Path(rel)):
            continue
        path = root / rel
        if _real(path) in excluded:
            continue
        digest = _hash_file(path) if path.is_file() else None
        if rel in tracked:
            dirty_tracked[rel] = digest
        else:
            untracked[rel] = digest

    nested, unmeasurable = _hash_opaque(root, opaque, exclude, max_files)
    return {
        "mode": "git",
        "dirty_tracked": dirty_tracked,
        "untracked": untracked,
        "nested": nested,
        "unmeasurable": unmeasurable,
        # Carried in the state itself, not only spliced into the CLI's manifest
        # payload, so an in-process `verify(root, snapshot_state(...))` walks
        # nested subtrees under the budget the snapshot actually used.
        "max_files": max_files,
    }


def _hash_opaque(root: Path, opaque: list[str], exclude: set[Path] | None,
                 max_files: int) -> tuple[dict[str, dict], list[str]]:
    """Hash each subtree git cannot see into; name the ones that could not be.

    Returns `({subtree: {path relative to the subtree: digest}}, unmeasurable)`.
    A subtree over `--max-files` or unreadable goes in the second list, which
    is what makes the verify report `not_measurable` instead of `clean`.
    """
    nested: dict[str, dict] = {}
    unmeasurable: list[str] = []
    for rel in opaque:
        subtree = root / rel
        if not subtree.is_dir():
            unmeasurable.append(rel)
            continue
        try:
            nested[rel] = build_manifest(subtree, exclude, max_files)
        except (TooManyFiles, OSError):
            unmeasurable.append(rel)
    return nested, sorted(unmeasurable)


def state_size(state: dict) -> int:
    if state.get("mode") == "git":
        return (
            len(state.get("dirty_tracked", {}))
            + len(state.get("untracked", {}))
            + sum(len(files) for files in state.get("nested", {}).values())
        )
    return len(state.get("files", {}))


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


class Blind(Exception):
    """Nothing about the tree could be read, so there is no report to build."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _classify_git(root: Path, state: dict,
                  exclude: set[Path] | None) -> tuple[list[str], list[str],
                                                      list[str], list[str],
                                                      list[str], list[str]]:
    """(modified, added, removed, git_changed, blind, unreadable) for git mode.

    Detection only. Nothing here decides who made a change: that comes from the
    run's declaration in `verify`, because no comparison of two tree states can
    tell this run's write from the user's editor saving the same file.

    `blind` names the subtrees that could not be read. It is returned rather
    than raised because a partly-readable tree still has findings worth
    reporting: an unhashable submodule must not swallow the out-of-scope edit
    the rest of the walk did see.

    `Blind` IS raised for the one case where nothing at all was observed, git
    itself going quiet. The old code returned an empty change list there, which
    `verify` rendered as `verdict: "clean"` and the caller recorded as a
    passing `scope_verify` gate: a guard reporting a clean tree it never read.
    """
    excluded = {_real(p) for p in (exclude or set())}
    pre_images = _pre_images(state)

    status = _git_status(root)
    tracked_now = _git_tracked(root)
    if status is None or tracked_now is None:
        raise Blind([
            "git could not report the state of the watched tree at verify "
            "time, so no change inside it could be seen"
        ])
    entries, opaque_now = status

    dirty_now = {rel for _code, rel in entries if not _is_ignored(Path(rel))}
    # A file recorded at snapshot that git no longer reports still needs
    # checking: reverting a dirty file to its HEAD content, or deleting an
    # untracked one, both drop it out of `git status` while being changes this
    # run made. Paths inside a currently-opaque subtree are `_classify_nested`'s
    # to bucket, and taking them here as well would report each one twice.
    candidates = {rel for rel in dirty_now | set(pre_images)
                  if not _in_scope(rel, opaque_now)}

    modified: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    unreadable: list[str] = []
    for rel in sorted(candidates):
        if _is_ignored(Path(rel)):
            continue
        path = root / rel
        if _real(path) in excluded:
            continue
        if rel in pre_images:
            before: object = pre_images[rel]
            existed = before is not None
        elif rel in tracked_now:
            # Tracked now and absent from every pre-image store means it was
            # clean when we started, so git reporting it dirty now proves it
            # changed during the round. Detection, not attribution: who changed
            # it is the declaration's question, not this one's.
            before, existed = CLEAN_TRACKED, True
        else:
            before, existed = None, False

        exists_now = path.is_file()
        if existed and not exists_now:
            removed.append(rel)
        elif not existed and exists_now:
            added.append(rel)
        elif existed and exists_now:
            now = _hash_file(path)
            if before == UNREADABLE or now == UNREADABLE:
                unreadable.append(rel)
                continue
            if before is CLEAN_TRACKED or now != before:
                modified.append(rel)

    nested = _classify_nested(root, state, pre_images, opaque_now,
                              tracked_now, exclude)
    modified.extend(nested[0])
    added.extend(nested[1])
    removed.extend(nested[2])
    unreadable.extend(nested[4])

    git_changed = [path for code, path in entries if code != "!!"]
    return (modified, added, removed, git_changed, nested[3], unreadable)


def _pre_images(state: dict) -> dict[str, object]:
    """Every path the snapshot recorded a pre-image for, root-relative.

    The three stores are partitioned by HOW git saw a file at snapshot time,
    not by what the file is, and a file moves between them during a round for
    reasons that have nothing to do with its content: a `git init` in a
    subdirectory turns ordinary untracked paths into an opaque nested subtree,
    and removing a submodule turns a nested subtree back into ordinary paths.
    A classifier that consults only its own store then finds no pre-image for a
    byte-identical file, calls it `added`, and hands the caller a revert
    instruction for a file nobody touched. One flat map, read by both
    classifiers, is what makes the movement irrelevant.
    """
    flat: dict[str, object] = {}
    flat.update(state.get("untracked", {}) or {})
    flat.update(state.get("dirty_tracked", {}) or {})
    for rel, files in (state.get("nested", {}) or {}).items():
        for inner, digest in files.items():
            flat[str(Path(rel) / inner)] = digest
    return flat


def _classify_nested(root: Path, state: dict, pre_images: dict[str, object],
                     opaque_now: list[str], tracked_now: set[str],
                     exclude: set[Path] | None
                     ) -> tuple[list[str], list[str], list[str],
                                list[str], list[str]]:
    """Bucket the subtrees git cannot see into: (mod, add, rm, blind, unreadable).

    Only the subtrees opaque *now* are walked here. One the snapshot hashed
    that is no longer opaque needs no special handling: its pre-images are in
    the same flat map the top-level loop reads, and git reports its files
    directly again.

    The reverse movement needs handling, and did not get it. A subtree that
    becomes opaque *during* the round -- a `git init` in a subdirectory, a
    submodule initialised by a build step -- takes its clean tracked files with
    it, and those files have no pre-image by construction: the snapshot stored
    nothing for them precisely because git was attributing them. Reading
    "absent from the pre-images" as `added` then manufactured a change for
    every one of them. They are still in the outer repository's index, which
    `git init` in a subdirectory does not touch, so `tracked_now` identifies
    them; their content at snapshot time is genuinely unknown, so they are
    reported as unreadable rather than invented as new.

    A subtree the snapshot could not hash, or one that cannot be hashed now,
    produces a blind reason rather than silence: `~/.claude/skills` and
    `~/.claude/scripts` are separate repositories inside `~/.claude`, which is
    the exact tree a run on a hook or a script watches, so this is the ordinary
    case here rather than an exotic one.
    """
    unmeasurable = list(state.get("unmeasurable", []) or [])
    # The snapshot's own budget, so a subtree that fitted then and has since
    # grown past it is reported rather than walked without limit.
    limit = state.get("max_files")
    if not isinstance(limit, int):
        limit = DEFAULT_MAX_FILES
    blind = [
        f"subtree {rel!r} is a nested repository or submodule the snapshot "
        f"could not hash, so changes inside it were not checked"
        for rel in unmeasurable
    ]

    modified: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    unreadable: list[str] = []
    for rel in sorted(set(opaque_now) - set(unmeasurable)):
        subtree = root / rel
        if subtree.is_dir():
            try:
                now = build_manifest(subtree, exclude, limit)
            except (TooManyFiles, OSError):
                blind.append(
                    f"subtree {rel!r} could not be hashed at verify time, so "
                    f"changes inside it were not checked")
                continue
        else:
            # Opaque per git, yet not a directory on disk. Whatever it is, its
            # contents were not read, and the snapshot records the same shape
            # under `unmeasurable` rather than passing over it.
            blind.append(
                f"{rel!r} is a submodule or nested repository git reports but "
                f"which is not a directory on disk, so it could not be read")
            continue

        prefix = rel + os.sep
        stored_here = {
            key[len(prefix):]: value for key, value in pre_images.items()
            if key.startswith(prefix)
        }
        for inner in sorted(set(stored_here) | set(now)):
            full = str(Path(rel) / inner)
            before = stored_here.get(inner)
            after = now.get(inner)
            if before == UNREADABLE or after == UNREADABLE:
                unreadable.append(full)
            elif inner not in stored_here and full in tracked_now:
                # Clean and tracked at snapshot, and the outer index still says
                # so, so the file predates the round and only its snapshot-time
                # content is unknown. `unreadable` is exactly that state, and
                # routing it there gets it a not_measurable reason instead of a
                # fabricated `added` and a revert that would delete it.
                unreadable.append(full)
            elif inner not in stored_here:
                added.append(full)
            elif inner not in now:
                removed.append(full)
            elif before != after:
                modified.append(full)
    return modified, added, removed, blind, unreadable


class DeclarationError(Exception):
    """A declaration that cannot be used as one."""


def _match_key(rel: str) -> str:
    """Comparison key for a root-relative path.

    Declared paths and detected paths come from different machinery -- the
    executor's own tool calls on one side, `git status`, `os.scandir` and
    `Path` joins on the other -- and two spellings of one file compare unequal.
    `./b`, `a/../b` and `b` are the same file. So are the NFC and NFD spellings
    of an accented name: macOS hands back whichever form the writer used while
    git's index holds NFC, so a path with an accent in it can be spelled two
    ways within a single run.

    A miss here is not cosmetic. It moves a path the run admits writing out of
    `violations` and into `unattributed_out_of_scope`, which reads as another
    writer's change -- the fail-open this whole design closes. It does fail in
    the safe direction when it happens anyway (the change lands in
    `unattributed` AND the declared path lands in `declared_out_of_scope` with
    nothing detected, so both push the verdict to `not_measurable`), but the
    round is lost either way.

    Keys are for comparison only. Every path in the report keeps the spelling
    it was found with, so `root / rel` still opens the right file.
    """
    return unicodedata.normalize("NFC", os.path.normpath(rel))


class Declaration:
    """What the run says it wrote, or the absence of any such statement.

    Tri-valued, and the third value is the one that matters. `present` with an
    empty path set is a run stating it wrote nothing, which is a claim the
    guard can hold it to. Absent is a run that said nothing at all, and reading
    those two as the same thing is how a guard fails open: every out-of-scope
    change would land in whichever bucket the empty set implies.

    Membership is exact, never prefix. A declared directory would attribute
    everything beneath it to this run, including the file the user was editing,
    and hand the caller a `git checkout` for it -- so `normalize_declared`
    refuses a directory outright rather than letting one widen attribution.
    """

    def __init__(self, paths: set[str] | None,
                 outside_root: list[str] | None = None):
        self.paths = paths
        self._keys = None if paths is None else {_match_key(p) for p in paths}
        self.outside_root = sorted(outside_root or [])

    @property
    def present(self) -> bool:
        return self.paths is not None

    def __contains__(self, rel: object) -> bool:
        return self._keys is not None and _match_key(str(rel)) in self._keys


ABSENT = Declaration(None)


def normalize_declared(raw: list[str], root: Path) -> Declaration:
    """Resolve declared paths into the same namespace the detected paths use.

    A declared path arrives however the executor spelled it: absolute,
    root-relative, `~`-prefixed, or reached through a symlinked temp directory
    (`/tmp` against `/private/tmp` on macOS). Detected paths are root-relative
    and built from `os.path.realpath(root)`. A spelling difference between the
    two is not cosmetic: it drops a path the run admits writing out of
    `violations` and into `unattributed_out_of_scope`, which reads as another
    writer's change and is exactly the fail-open this design closes. Resolve
    both ends the same way, once, here.

    A relative path is resolved against the watch root, not the process's
    working directory: Step 6a runs in whatever directory the executor happens
    to be in, and the root is the only namespace both ends of the comparison
    share. An absolute path is the unambiguous form and is what the docs ask
    for.

    A declared path that lands outside the watched root is kept separately. It
    cannot be checked against a snapshot that never covered it, so it is
    reported rather than silently dropped or blamed.
    """
    root_real = Path(_real(root))
    inside: set[str] = set()
    outside: list[str] = []
    for entry in raw:
        text = str(entry).strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root_real / candidate
        resolved = Path(_real(candidate))
        if resolved.is_dir():
            raise DeclarationError(
                f"declared path {text!r} is a directory. Declare the files "
                f"this run wrote, one per path: a directory attributes "
                f"everything under it to this run, including whatever the "
                f"user changed there, and the caller reverts what it is told "
                f"to revert.")
        try:
            rel = resolved.relative_to(root_real)
        except ValueError:
            outside.append(text)
            continue
        inside.add(str(rel))
    return Declaration(inside, outside)


def verify(root: Path, state: dict, scope: list[str],
           exclude: set[Path] | None = None,
           declared: Declaration | None = None) -> dict:
    """Compare the live tree against a snapshot and bucket every change.

    Three verdicts, and the third one is the point. `clean` means the tree was
    read and held no out-of-scope change. `scope_violation` means it held one
    THIS RUN DECLARED WRITING, which is the only kind the caller may revert.
    `not_measurable` means the question was not answered: git went quiet, a
    nested repository could not be hashed, an out-of-scope file changed that
    this run did not declare, or no declaration was supplied at all.

    The declaration is what separates the second verdict from the third, and it
    has to be, because the tree cannot. A file that changed during the round
    looks identical whether this run wrote it or the user's editor did, and the
    documented response to a violation is `git checkout` of the listed paths.
    Attributing from the diff put the user's own uncommitted work in
    `violations` and the run's real out-of-scope edit in
    `unattributed_out_of_scope`, firing the destructive branch in precisely the
    case where attribution was unsound. Reporting an undeclared change and
    halting loses a round; reverting it loses the work.
    """
    blind: list[str] = []
    unreadable: list[str] = []
    mode = state.get("mode")
    declaration = declared if declared is not None else ABSENT
    if mode == "git":
        try:
            (modified, added, removed, git_changed,
             blind, unreadable) = _classify_git(root, state, exclude)
        except Blind as exc:
            return _blind_report(root, scope, state, exc.reasons, declaration)
        # Which paths the snapshot holds a pre-image for, which is exactly the
        # set that was already dirty or untracked when the run began.
        pre_image_paths = set(_pre_images(state))
    elif mode == "walk":
        manifest = state.get("files", {})
        # The snapshot's own budget. Without it a walk root that grew past the
        # limit between snapshot and verify was hashed without bound, while
        # every other walk in this file -- the snapshot, the nested subtrees --
        # is bounded.
        limit = state.get("max_files")
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = DEFAULT_MAX_FILES
        try:
            current = build_manifest(root, exclude, limit)
        except TooManyFiles as exc:
            return _blind_report(root, scope, state, [
                f"the watched tree holds more than {exc.limit} file(s) at "
                f"verify time, over the budget its snapshot was taken under, "
                f"so it was not hashed"], declaration)
        unreadable = sorted(
            p for p in set(manifest) | set(current)
            if manifest.get(p) == UNREADABLE or current.get(p) == UNREADABLE
        )
        skip = set(unreadable)
        modified = sorted(
            p for p in (set(manifest) & set(current)) - skip
            if manifest[p] != current[p]
        )
        added = sorted(set(current) - set(manifest) - skip)
        removed = sorted(set(manifest) - set(current) - skip)
        # In walk mode the hash manifest IS the check, so git having nothing to
        # say costs no visibility. It only feeds the informational
        # `preexisting_dirty_out_of_scope` list.
        git_changed = _git_changed(root)
        # Walk mode is what runs outside a repository, so every path has a
        # pre-image and none of them has a HEAD to be checked out back to.
        pre_image_paths = set(manifest)
    else:
        # A manifest whose mode is missing or unrecognised used to fall through
        # to the walk branch, where an absent `files` map made every file under
        # the root `added` and the caller was handed the whole tree as a revert
        # list. `_run_verify` guards the analogous `--root` mismatch for the
        # same reason. Nothing was compared here, so say that.
        return _blind_report(root, scope, state, [
            f"the manifest records mode {mode!r}, which this guard cannot "
            f"read, so nothing in the watched tree was compared"], declaration)

    changed = sorted(set(modified) | set(added) | set(removed))
    out_of_scope = [p for p in changed if not _in_scope(p, scope)]
    # Attribution, and the only place it happens. A change this run declared
    # writing is one it caused and may revert; anything else out of scope
    # belongs to another writer and is reported, never reverted.
    violations = sorted(p for p in out_of_scope if p in declaration)
    unattributed = sorted(p for p in out_of_scope if p not in declaration)
    # Violations `git checkout` cannot correctly undo. It works on one shape
    # only, a file clean and tracked when the run began, where HEAD still holds
    # what the run overwrote. A file already dirty at snapshot loses the user's
    # earlier edits too; a file the run created has no HEAD content to return
    # to. Attribution is sound either way -- the run declared these -- so they
    # stay violations and only the instruction differs.
    added_set = set(added)
    violations_manual = sorted(
        p for p in violations if p in pre_image_paths or p in added_set)
    declared_out_of_scope = sorted(
        p for p in (declaration.paths or set()) if not _in_scope(p, scope))
    in_scope_new = sorted(p for p in added if _in_scope(p, scope))
    # The one check that can catch a declaration that is simply wrong: an
    # honest run declares its in-scope edits, so an undeclared one proves the
    # declaration incomplete -- and an incomplete declaration is what would let
    # a real out-of-scope write pass as another writer's.
    undeclared_in_scope = sorted(
        p for p in changed if _in_scope(p, scope) and p not in declaration
    ) if declaration.present else []
    if not declaration.present:
        blind.append(
            "this run declared no edited paths, so nothing found in the tree "
            "can be attributed to it. Re-run --verify with --declared, "
            "--declared-file, or --declared-none")
    blind.extend(
        f"{path!r} changed inside the permitted scope but this run did not "
        f"declare writing it, so the declaration is incomplete and what it "
        f"omits elsewhere is unknown" for path in undeclared_in_scope
    )
    blind.extend(
        f"{path!r} changed outside scope during the round and this run did "
        f"not declare writing it, so another writer changed it and it must "
        f"not be reverted" for path in unattributed
    )
    # Declared out of scope, yet nothing there changed. The run's own admission
    # is enough to refuse a `clean`, and not enough to order a revert: the path
    # may be an untracked file that predates the round, and reverting one means
    # deleting it.
    blind.extend(
        f"this run declared it wrote {path!r}, which is outside the permitted "
        f"scope, but no change was detected there"
        for path in declared_out_of_scope if path not in set(violations)
    )
    blind.extend(
        f"this run declared it wrote {path!r}, which lies outside the watched "
        f"root, so whether it changed could not be checked"
        for path in declaration.outside_root
    )
    # An unreadable file is the plainest form of "could not see": it exists and
    # its content is unknown at one or both ends of the comparison. Reported
    # out of scope only, since in scope this run is free to change it anyway.
    blind.extend(
        f"{path!r} could not be read, so whether it changed during the round "
        f"is unknown" for path in sorted(unreadable) if not _in_scope(path, scope)
    )
    # The scope itself sitting under an ignored directory name would make every
    # edit the run is supposed to make invisible, and a tree nothing was read
    # from must not come back `clean`.
    blind.extend(
        f"the permitted scope {entry!r} lies under an ignored directory name, "
        f"so nothing inside it was read" for entry in scope
        if _is_ignored(Path(entry))
    )

    preexisting = []
    if git_changed is not None:
        # Git reports a file dirty relative to HEAD; the manifest reports it
        # relative to this run's start. A file git calls dirty whose hash still
        # matches the snapshot was already dirty before the run began — someone
        # else's uncommitted work, not ours.
        #
        # This must NOT be a violation. The caller's documented response to a
        # violation is to revert the offending paths, and reverting a file this
        # run never touched destroys whatever uncommitted work was sitting
        # there. Report it as context and let the hash manifest, which is the
        # only signal that can attribute a change to this run, decide.
        changed_set = set(changed)
        preexisting = sorted(
            p for p in git_changed
            if p not in changed_set and not _is_ignored(Path(p))
            and not _in_scope(p, scope)
        )

    if violations:
        verdict = "scope_violation"
    elif blind:
        verdict = "not_measurable"
    else:
        verdict = "clean"
    report = {
        "verdict": verdict,
        "root": str(root),
        "scope": scope,
        "mode": mode,
        "modified_in_scope": sorted(p for p in modified if _in_scope(p, scope)),
        "new_files_in_scope": in_scope_new,
        "violations": violations,
        # The subset of `violations` a `git checkout` would damage: the file
        # was already dirty or untracked at snapshot, so restore this run's
        # edit by hand instead.
        "violations_manual_revert": violations_manual,
        # Changed out of scope and undeclared, so somebody else's. Report, never revert.
        "unattributed_out_of_scope": unattributed,
        # Changed in scope without being declared. The declaration is incomplete.
        "undeclared_in_scope": undeclared_in_scope,
        # Dirty in git before this run started. Informational: never revert these.
        "preexisting_dirty_out_of_scope": preexisting,
        # Everything the run admitted writing outside its permitted scope,
        # whether or not a change was detected there.
        "declared_out_of_scope": declared_out_of_scope,
        "declared_outside_root": declaration.outside_root,
        "declaration_present": declaration.present,
        "not_measurable_reasons": blind,
        "git_available": git_changed is not None,
        "counts": {
            "modified": len(modified),
            "added": len(added),
            "removed": len(removed),
            "violations": len(violations),
            "unattributed": len(unattributed),
            "preexisting_dirty": len(preexisting),
            "declared": len(declaration.paths or ()),
        },
    }
    fallback = state.get("root_fallback")
    if fallback:
        report["root_fallback"] = fallback
    return report


def _blind_report(root: Path, scope: list[str], state: dict,
                  reasons: list[str],
                  declaration: Declaration | None = None) -> dict:
    """The report for a verify that could not read the tree it was asked about.

    Same shape as a normal report so the caller parses one thing, with empty
    change lists and a verdict that is not a pass. The empty lists used to be
    the whole report, under `verdict: "clean"`.
    """
    declaration = declaration if declaration is not None else ABSENT
    report = {
        "verdict": "not_measurable",
        "root": str(root),
        "scope": scope,
        "mode": state.get("mode"),
        "modified_in_scope": [],
        "new_files_in_scope": [],
        "violations": [],
        "violations_manual_revert": [],
        "unattributed_out_of_scope": [],
        "undeclared_in_scope": [],
        "preexisting_dirty_out_of_scope": [],
        "declared_out_of_scope": sorted(
            p for p in (declaration.paths or set()) if not _in_scope(p, scope)),
        "declared_outside_root": declaration.outside_root,
        "declaration_present": declaration.present,
        "not_measurable_reasons": reasons,
        "git_available": False,
        "counts": {"modified": 0, "added": 0, "removed": 0, "violations": 0,
                   "unattributed": 0, "preexisting_dirty": 0,
                   "declared": len(declaration.paths or ())},
    }
    fallback = state.get("root_fallback")
    if fallback:
        report["root_fallback"] = fallback
    return report


def _same_dir(recorded: str, root: Path) -> bool:
    """True when a manifest's recorded root names the same directory as `root`.

    String equality first (the snapshot writes `str(root)` unresolved), then a
    resolved comparison so a symlinked or `~`-spelled path is not a mismatch.
    """
    if recorded == str(root):
        return True
    try:
        return Path(recorded).expanduser().resolve() == root.resolve()
    except OSError:
        return False


def _fallback_note(fallback: dict) -> str:
    return (
        f"WARNING: the watch is NARROWER than intended. "
        f"{fallback.get('intended_root')} would hash "
        f"{fallback.get('candidate_count')} "
        f"file(s), over the --max-files limit of {fallback.get('limit')}, so only "
        f"{fallback.get('actual_root')} was watched. Changes elsewhere in that "
        f"repository were NOT checked."
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def _display(text: str) -> str:
    """A form of `text` that is safe to print on any terminal encoding.

    Paths carry surrogate escapes when the filesystem holds non-UTF-8 bytes,
    and printing one raises `UnicodeEncodeError` out of the reporting code
    after the check has already succeeded. Only the human-readable output needs
    this; `--json` escapes surrogates on its own.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scope guard for hone Phase 2 edits")
    parser.add_argument("--artifact",
                        help="Path to the artifact being honed. With --type, "
                             "derives both the watch root and the permitted scope.")
    parser.add_argument("--type", choices=ARTIFACT_TYPES, dest="artifact_type",
                        help="Artifact kind: %s" % "|".join(ARTIFACT_TYPES))
    parser.add_argument("--root", default=None,
                        help="Override the derived watch root.")
    parser.add_argument("--manifest", required=True,
                        help="Path to the manifest file to write or read")
    parser.add_argument("--scope", action="append", default=[],
                        help="Repeatable. Override the derived scope with a "
                             "root-relative path the run may change.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES,
                        dest="max_files",
                        help=f"Bail out of a watch root larger than N files "
                             f"(default {DEFAULT_MAX_FILES}); inside a repo, fall "
                             f"back to the install directory and say so.")
    parser.add_argument("--declared", action="append", default=[],
                        help="Repeatable. A path THIS RUN wrote, absolute or "
                             "root-relative. Only a declared out-of-scope "
                             "change is a revertable violation; an undeclared "
                             "one belongs to another writer. Required on "
                             "--verify (or --declared-file / --declared-none).")
    parser.add_argument("--declared-file", dest="declared_file", default=None,
                        help="JSON holding the declaration: a list of paths, "
                             "{\"edited_paths\": [...]}, or a workflow state "
                             "file with applied_edits.edited_paths.")
    parser.add_argument("--declared-none", action="store_true",
                        dest="declared_none",
                        help="This run wrote nothing. The explicit form of an "
                             "empty declaration, which is NOT the same as "
                             "omitting the declaration.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true", help="Record pre-edit state")
    group.add_argument("--verify", action="store_true", help="Check against manifest")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    if bool(args.artifact) != bool(args.artifact_type):
        _fail("--artifact and --type must be given together")

    declared_given = bool(args.declared or args.declared_file or args.declared_none)
    if args.snapshot and declared_given:
        _fail("--declared/--declared-file/--declared-none belong to --verify: "
              "a snapshot is taken before the run writes anything")
    if args.declared_none and (args.declared or args.declared_file):
        _fail("--declared-none says this run wrote nothing; passing it "
              "alongside --declared or --declared-file says two things at once")

    artifact = None
    if args.artifact:
        artifact = Path(args.artifact).expanduser()
        if not artifact.exists():
            _fail(f"artifact not found: {artifact}")
        artifact = Path(_real(artifact))
        if artifact.is_dir():
            _fail(f"--artifact must be a file, not a directory: {artifact}. "
                  "For a skill, pass the SKILL.md inside it.")

    manifest_path = Path(args.manifest).expanduser()
    excluded = {manifest_path}

    if args.snapshot:
        _run_snapshot(args, artifact, manifest_path, excluded)
        return
    _run_verify(args, artifact, manifest_path, excluded)


def _resolve_scope(artifact: Path | None, artifact_type: str | None,
                   root: Path, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    if artifact is None:
        return []
    scope_abs = permitted_scope(artifact, artifact_type)
    try:
        return [str(scope_abs.relative_to(Path(_real(root))))]
    except ValueError:
        _fail(f"artifact {artifact} is not inside the watch root {root}")
    return []  # unreachable; _fail exits


def _declared_paths(args) -> list[str] | None:
    """The raw declared paths from the CLI, or None when nothing was declared.

    `--declared-file` accepts three shapes so the executor can point it at
    whatever it already writes: a bare list, `{"edited_paths": [...]}`, or a
    workflow state file carrying `applied_edits.edited_paths`. A file that
    holds none of them is a usage error rather than an empty declaration --
    the caller aimed at a declaration and there is not one there, and reading
    that as "wrote nothing" would quietly disarm the check.
    """
    if args.declared_none:
        return []
    if not args.declared and not args.declared_file:
        return None
    raw = list(args.declared)
    if args.declared_file:
        path = Path(args.declared_file).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _fail(f"--declared-file not found: {args.declared_file}")
            return None
        except (OSError, json.JSONDecodeError) as exc:
            _fail(f"cannot read --declared-file {args.declared_file}: {exc}")
            return None
        found = _extract_edited_paths(payload)
        if found is None:
            _fail(f"--declared-file {args.declared_file} holds no declaration: "
                  f"expected a list of paths, an object with 'edited_paths', "
                  f"or a state file with applied_edits.edited_paths")
            return None
        if not found and not raw:
            # An empty list here is `--declared-none` arrived at by accident,
            # which is the one reading of a declaration that must never be
            # inferred. The handoff schema requires a non-empty `edited_paths`
            # alongside `edit_count >= 1`, so an empty one means the run failed
            # to record what it wrote.
            _fail(f"--declared-file {args.declared_file} declares an empty "
                  f"list. If this run really wrote nothing, say so with "
                  f"--declared-none; otherwise record the paths it wrote.")
            return None
        raw.extend(found)
    return raw


def _extract_edited_paths(payload: object) -> list[str] | None:
    """The declared path list inside a `--declared-file` payload, or None."""
    if isinstance(payload, list):
        return [str(entry) for entry in payload]
    if isinstance(payload, dict):
        for holder in (payload, payload.get("applied_edits")):
            if isinstance(holder, dict):
                found = holder.get("edited_paths")
                if isinstance(found, list):
                    return [str(entry) for entry in found]
    return None


def _run_snapshot(args, artifact: Path | None, manifest_path: Path,
                  excluded: set[Path]) -> None:
    fallback = None
    if args.root:
        root = Path(args.root).expanduser()
    elif artifact is not None:
        root, fallback = derive_root(artifact, args.artifact_type, args.max_files)
    else:
        _fail("--snapshot needs --artifact/--type (or an explicit --root)")
        return

    if not root.is_dir():
        _fail(f"root is not a directory: {root}")

    scope = _resolve_scope(artifact, args.artifact_type, root, args.scope)

    try:
        state = snapshot_state(root, excluded, args.max_files)
    except TooManyFiles as exc:
        _fail(f"watch root {root} holds more than --max-files {exc.limit} files "
              f"and cannot be narrowed further; pass a smaller --root or raise "
              f"--max-files")
        return

    payload = {
        "root": str(root),
        "scope": scope,
        "artifact": str(artifact) if artifact else None,
        "type": args.artifact_type,
        "max_files": args.max_files,
        **state,
    }
    if fallback:
        payload["root_fallback"] = fallback

    try:
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot write manifest {args.manifest}: {exc}")

    recorded = state_size(state)
    unmeasurable = list(state.get("unmeasurable", []) or [])
    report = {
        "verdict": "snapshot",
        "files_recorded": recorded,
        "manifest": args.manifest,
        "root": str(root),
        "scope": scope,
        "mode": state.get("mode"),
        "nested_repos": sorted(state.get("nested", {}) or {}),
        "unmeasurable": unmeasurable,
    }
    if fallback:
        report["root_fallback"] = fallback

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"SNAPSHOT: {recorded} file(s) recorded to {args.manifest}")
        print(f"  root:  {root}")
        print(f"  scope: {', '.join(scope) if scope else '(none declared)'}")
        for rel in report["nested_repos"]:
            print(f"  nested repository hashed in full: {rel}")
        for rel in unmeasurable:
            print(f"  WARNING: {rel} could not be hashed; --verify will report "
                  f"not_measurable rather than clean")
        if fallback:
            print("  " + _fallback_note(fallback))
    sys.exit(0)


def _run_verify(args, artifact: Path | None, manifest_path: Path,
                excluded: set[Path]) -> None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"manifest not found: {args.manifest}; run --snapshot first")
        return
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read manifest {args.manifest}: {exc}")
        return

    recorded_root = payload.get("root")

    # --verify reads root and scope back out of the manifest so the caller
    # never has to reproduce them. Step 5a and Step 6a are separate tool calls
    # and shell state does not survive between them; asking the executor to
    # re-derive $SCOPE_ROOT is exactly how the root-mismatch bug happened.
    if args.root:
        root = Path(args.root).expanduser()
        # The manifest records the root it was taken under. Under a different
        # root every recorded path is missing and every present path is new, so
        # the report lists the whole tree under `violations` -- and the
        # documented response to a violation is "revert only the paths listed".
        if (
            isinstance(recorded_root, str)
            and recorded_root
            and not _same_dir(recorded_root, root)
        ):
            _fail(f"manifest was taken under root {recorded_root} but --root "
                  f"resolved to {root}; re-run --snapshot or pass the same --root")
    elif isinstance(recorded_root, str) and recorded_root:
        root = Path(recorded_root).expanduser()
    else:
        _fail(f"manifest {args.manifest} records no root; re-run --snapshot")
        return

    if not root.is_dir():
        _fail(f"root is not a directory: {root}")

    scope = args.scope or payload.get("scope") or []
    if artifact is not None and not args.scope:
        scope = _resolve_scope(artifact, args.artifact_type, root, [])
    if not scope:
        _fail("--verify found no scope: the manifest declares none and no "
              "--scope was passed. Re-run --snapshot with --artifact/--type.")

    raw_declared = _declared_paths(args)
    try:
        declaration = (ABSENT if raw_declared is None
                       else normalize_declared(raw_declared, root))
    except DeclarationError as exc:
        _fail(str(exc))
        return

    report = verify(root, payload, scope, excluded, declaration)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']} (root: {root}; "
              f"scope: {', '.join(report['scope'])})")
        counts = report["counts"]
        # `violations` alone under-reads as "nothing happened out of scope"
        # whenever the out-of-scope changes were all unattributable.
        out_of_scope = counts["violations"] + counts["unattributed"]
        print(f"  {counts['modified']} modified, {counts['added']} added, "
              f"{counts['removed']} removed, {out_of_scope} out of scope "
              f"({counts['violations']} revertable)")
        manual = set(report["violations_manual_revert"])
        for path in report["violations"]:
            if path in manual:
                print(f"  VIOLATION: {_display(path)} (was already dirty or "
                      f"untracked at snapshot: undo this run's edit by hand, "
                      f"a checkout would take the earlier work too)")
            else:
                print(f"  VIOLATION: {_display(path)}")
        unattributed = report["unattributed_out_of_scope"]
        if unattributed:
            print(f"  NOTE: {len(unattributed)} file(s) changed out of scope "
                  "during the round that this run did not declare writing, so "
                  "another writer changed them. Do NOT revert them; inspect "
                  "them:")
            for path in unattributed:
                print(f"    unattributed: {_display(path)}")
        preexisting = report["preexisting_dirty_out_of_scope"]
        if preexisting:
            print(f"  NOTE: {len(preexisting)} file(s) were already uncommitted "
                  "before this run and are unchanged by it. Do not revert them:")
            for path in preexisting:
                print(f"    pre-existing: {_display(path)}")
        if report.get("root_fallback"):
            print("  " + _fallback_note(report["root_fallback"]))
        for reason in report["not_measurable_reasons"]:
            print(f"  UNCHECKED: {_display(reason)}")
        if not report["git_available"] and report.get("mode") != "git":
            # Only true of a walk-mode manifest, which hashes the whole tree.
            # A git-mode manifest stores no whole-tree hashes, so when git goes
            # quiet there nothing was checked at all -- and the verdict above
            # is `not_measurable`, not `clean`.
            print("  NOTE: git unavailable here; hash manifest was the only check")

    sys.exit(VERDICT_EXITS.get(report["verdict"], 1))


def _cli() -> None:
    """`main()` with every uncaught error routed away from exit 1.

    Exit 1 is `scope_violation`, and the caller's branch table answers it by
    reverting the paths under `violations`. A traceback exits 1 too, and prints
    no report at all, so a crash read as an order to revert a list the caller
    cannot even read. Anything unplanned is exit 2: the check never ran, revert
    nothing, halt. `SystemExit` passes through untouched so the verdict codes
    the body sets still reach the shell.
    """
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _fail("interrupted")
    except BaseException as exc:  # noqa: BLE001 - deliberate catch-all
        _fail(f"internal error in check_scope.py ({type(exc).__name__}: {exc}); "
              f"the scope check did not run, so revert nothing and halt")


if __name__ == "__main__":
    _cli()
