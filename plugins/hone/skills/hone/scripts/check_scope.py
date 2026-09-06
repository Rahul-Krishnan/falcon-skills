#!/usr/bin/env python3
"""Scope guard for hone Phase 2 edits.

Combine git status for tracked files with hashes for dirty, untracked, and
opaque paths. This detects collateral edits that git diff alone misses,
following trailofbits/skills skill-improver.

Root is the watched tree; scope is the subset this run may change. Derive
both from --artifact and --type: a hook needs its install directory watched
but only its file permitted; a repository skill needs the repository watched
but only its own directory permitted. Verify reloads both from the manifest.

  --snapshot  Record pre-edit state before Phase 2 writes.
  --verify    Compare against that state and the run's declared writes.

Tree changes establish detection, not authorship. Attribution comes only
from --declared, --declared-file, or --declared-none. Declared out-of-scope
changes are violations. Undeclared changes are unattributed: halt without
reverting another writer's possible work.

Incomplete coverage or attribution returns not_measurable, never clean:

  1.  Git stops answering between snapshot and verify.
  2.  A nested repository or submodule cannot be hashed.
  3.  A regular file cannot be read (_hash_file returns UNREADABLE).
  4.  A directory cannot be listed.
  5.  An entry's type cannot be determined.
  6.  A symlink is dangling.
  7.  An entry is not a directory or regular file (socket, fifo, device, door).
  8.  A directory symlink loops onto its descent path.
  9.  The tree exceeds --max-files at snapshot or verify.
  10. The manifest mode is unknown.
  11. Scope lies under an ignored directory.
  12. An out-of-scope change is undeclared.
  13. An in-scope change is undeclared, making the declaration incomplete.
  14. A declared path lies outside the watched root.
  15. Verify receives no declaration.

Follow and hash non-looping directory symlinks, which are common in skill
and hook installations. Record unreadable paths rather than dropping them.

Exit codes: 0 clean; 1 scope violation; 2 usage error (check did not run);
3 not_measurable (check could not answer). Only 0 passes. This vocabulary
matches check_overfit.py and check_eval_power.py.

Stdlib only. Writes only the manifest under --snapshot; never artifact content.
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

# Routine tool-generated files.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", ".coverage"}

# Ignore caches and dependency trees, not possible source directories such as
# dist or build. Names match any path component; avoid generic names such as
# venv and .cache that could hide artifact paths. A scope under an ignored
# name must report not_measurable.
IGNORED_PARTS = {
    "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".pytype", ".tox", ".nox", ".hypothesis", "htmlcov", ".ipynb_checkpoints",
    "node_modules", ".venv", ".gradle", ".terraform",
    ".next", ".nuxt", ".turbo", ".parcel-cache",
}

# Backup and working files hone and workout create by design.
IGNORED_SUFFIXES = (".pyc", ".pre-hone", ".pre-workout", ".pre-audit", ".pre-enrich")

ARTIFACT_TYPES = ("skill", "command", "hook", "script")

# Keep unknown coverage separate from violations: not_measurable provides
# no basis for restoring files.
VERDICT_EXITS = {"clean": 0, "scope_violation": 1, "not_measurable": 3}

DEFAULT_MAX_FILES = 20000

# Clean tracked files need no stored hash: git detects later changes, while
# declarations identify the writer.
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


# Distinguish unreadable files from absent ones (None). A JSON-safe sentinel
# that cannot equal a SHA-256 digest preserves this distinction across snapshots.
UNREADABLE = "unreadable"

# Older manifests stored opaque unreadable subtrees as bare paths. Preserve
# their reason when normalizing to {path, reason} records.
NESTED_UNMEASURABLE = ("it is a nested repository or submodule the snapshot "
                       "could not hash")


def _unmeasurable(state: dict) -> list[dict]:
    """`state['unmeasurable']`, normalised to `{"path", "reason"}` records."""
    records: list[dict] = []
    for entry in state.get("unmeasurable", []) or []:
        if isinstance(entry, str):
            records.append({"path": entry, "reason": NESTED_UNMEASURABLE})
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            reason = entry.get("reason")
            records.append({
                "path": entry["path"],
                "reason": reason if isinstance(reason, str) and reason
                else NESTED_UNMEASURABLE,
            })
    return records


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return UNREADABLE


def _real(path: Path) -> str:
    """Resolved path as a string, tolerant of paths that do not exist."""
    return os.path.realpath(str(path))


def _walk(root: Path, exclude: set[Path] | None = None,
          limit: int | None = None) -> tuple[list[Path], list[dict]]:
    """Return (files, unmeasurable) for entries under root.

    Prune ignored directories before descent. Record unhashable or unwalkable
    entries as {"path", "reason"}; snapshot_state preserves these and verify
    reports not_measurable instead of treating unread paths as empty.

    Follow directory symlinks, including installed artifacts. Track real
    ancestors per branch to stop loops; two links to one target remain valid
    paths and are both walked.
    """
    excluded = {_real(p) for p in (exclude or set())}
    found: list[Path] = []
    unmeasurable: list[dict] = []

    def record(path: Path, reason: str) -> None:
        try:
            rel = str(path.relative_to(root))
        except ValueError:  # pragma: no cover - path always sits under root
            rel = str(path)
        unmeasurable.append({"path": rel, "reason": reason})

    # (directory, real paths of every directory from root down to it).
    stack: list[tuple[Path, frozenset[str]]] = [(root, frozenset({_real(root)}))]
    while stack:
        current, ancestors = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            record(current, f"the directory could not be listed "
                            f"({type(exc).__name__})")
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                # Follow symlinked artifact directories and files.
                is_dir = entry.is_dir()
                is_file = entry.is_file()
                is_link = entry.is_symlink()
            except OSError as exc:
                record(path, f"the entry could not be classified as a file or "
                             f"a directory ({type(exc).__name__})")
                continue
            if is_dir:
                if entry.name in IGNORED_PARTS:
                    continue
                real = _real(path)
                if real in excluded:
                    continue
                if real in ancestors:
                    record(path, "the directory is a symlink back onto the "
                                 "path from the watch root, so walking it "
                                 "would loop")
                    continue
                stack.append((path, ancestors | {real}))
                continue
            if _is_ignored(path.relative_to(root)):
                continue
            if _real(path) in excluded:
                continue
            if is_file:
                found.append(path)
                if limit is not None and len(found) > limit:
                    raise TooManyFiles(len(found), limit)
                continue
            if is_link:
                record(path, "the symlink target does not exist, so there is "
                             "nothing to hash")
            else:
                record(path, "the entry is neither a directory nor a regular "
                             "file (socket, fifo, or device), so its content "
                             "could not be hashed")
    return found, unmeasurable


def walk_manifest(root: Path, exclude: set[Path] | None = None,
                  limit: int | None = None) -> tuple[dict, list[dict]]:
    """Return file hashes and unreadable-path records under root.

    Both are needed to compare content without reporting unread paths as clean.
    """
    files, unmeasurable = _walk(root, exclude, limit)
    return (
        {str(p.relative_to(root)): _hash_file(p) for p in files},
        sorted(unmeasurable, key=lambda entry: entry["path"]),
    )


def build_manifest(root: Path, exclude: set[Path] | None = None,
                   limit: int | None = None) -> dict:
    """Hash non-ignored files under root, skipping exclude paths.

    Exclude the manifest itself to avoid detecting its write as a scope change.
    Use walk_manifest when callers also need unreadable-path records.
    """
    return walk_manifest(root, exclude, limit)[0]


def _git(root: Path, *args: str) -> str | None:
    """Return git stdout from root, or None when git cannot answer.

    Decode raw path bytes with os.fsdecode so non-UTF-8 names round-trip through
    Path operations. Strict text decoding can raise an exception that would
    otherwise exit 1, the scope-violation code.
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
    """Return index paths relative to root.

    ls-files honors -C root; porcelain status instead uses the repository root
    and needs rebasing.
    """
    out = _git(root, "ls-files", "-z")
    if out is None:
        return None
    return set(_nul_list(out))


def _git_gitlinks(root: Path) -> list[str] | None:
    """Return registered submodules as root-relative paths.

    Gitlinks have index mode 160000 and content in another repository. Treat
    them as opaque subtrees, not removed files without recoverable content.
    """
    out = _git(root, "ls-files", "--stage", "-z")
    if out is None:
        return None
    links: list[str] = []
    for record in _nul_list(out):
        # Format: <mode> <sha> <stage>\t<path>. -z paths are raw, never C-quoted;
        # unquoting would change filenames containing literal surrounding quotes.
        head, _, path = record.partition("\t")
        if head.startswith("160000") and path:
            links.append(path)
    return links


def _git_hash_candidates(root: Path) -> int | None:
    """Estimate files hashed by a git-mode snapshot for --max-files.

    Count dirty and untracked files, not clean tracked files. Limiting the full
    tracked count would narrow large repositories without saving hashing work.
    Count opaque subtrees once here; _hash_opaque enforces their individual
    budgets and records failures as unmeasurable.
    """
    status = _git_status(root)
    if status is None:
        return None
    return _candidate_count(*status)


def _candidate_count(entries: list[tuple[str, str]], opaque: list[str]) -> int:
    """Count hash candidates from an existing _git_status result.

    Reuse snapshot_state's result rather than running git status again.
    """
    hashed = [rel for _code, rel in entries if not _is_ignored(Path(rel))]
    return len(hashed) + len(opaque)


def _git_status(root: Path) -> tuple[list[tuple[str, str]], list[str]] | None:
    """Return (status code, root-relative path) entries and opaque subtree paths.

    Rebase porcelain's repository-relative paths onto root and drop paths
    outside it. --untracked-files=all expands ordinary directories to match the
    file-level manifest. --ignored=traditional includes individually listed
    ignored files; wholly ignored directories stay collapsed and skipped.

    Other collapsed directories are nested repositories git cannot inspect.
    Return them separately for hashing or an explicit not_measurable report.
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
            # Ignore dirty paths outside the watch.
            continue
        if collapsed:
            # Skip collapsed ignored trees. Other collapsed directories are opaque
            # nested repositories and must remain visible.
            if code != "!!" and not _is_ignored(relative):
                opaque.append(str(relative))
            continue
        entries.append((code, str(relative)))

    gitlinks = _git_gitlinks(root)
    if gitlinks is None:
        return None
    # ls-files paths are already root-relative; rebasing would double prefixes.
    opaque.extend(link for link in gitlinks if not _is_ignored(Path(link)))
    # A submodule can also appear as a dirty entry; the opaque handling owns it.
    opaque_set = set(opaque)
    entries = [(code, rel) for code, rel in entries if rel not in opaque_set]
    return entries, sorted(opaque_set)


def _git_changed(root: Path) -> list[str] | None:
    """Return dirty root-relative paths, or None outside a repository.

    Exclude ignored entries from the informational dirty-file list; _git_status
    still collects them for change detection.
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
    """Decode porcelain's C-quoted paths, including octal byte escapes.

    Stripping quotes alone leaves escaped names that cannot match files on disk.
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
    # Preserve non-UTF-8 bytes with surrogate escapes; replacement characters
    # would break filesystem lookups and ls-files comparisons.
    return os.fsdecode(bytes(out))


def _porcelain_path(line: str) -> str:
    """Return a porcelain entry's on-disk path.

    Keep the destination of an old -> new rename/copy and decode C-quoting.
    Otherwise nonexistent composite or escaped paths disappear from the report.
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
    """Return the artifact's primary permitted path.

    Skills permit their whole directory, including references, scripts, and
    evals. Commands, hooks, and scripts permit only their file; siblings belong
    to other artifacts. permitted_scopes adds command validator directories.
    """
    return artifact.parent if artifact_type == "skill" else artifact


def permitted_scopes(artifact: Path, artifact_type: str) -> list[Path]:
    """Return permitted paths, with the artifact first.

    Commands also permit their {name}-validator/ companion directory so a
    required validate_handoffs.py is not itself a scope violation. This adds
    only the named companion, not the whole commands directory. Hooks and
    scripts use direct tests and do not need generated validator companions.
    """
    primary = permitted_scope(artifact, artifact_type)
    if artifact_type != "command":
        return [primary]
    return [primary, artifact.parent / f"{artifact.stem}-validator"]


def install_root(artifact: Path, artifact_type: str) -> Path:
    """Return the narrow watch root outside a repository.

    For skills, use the directory of installed skills; for single-file
    artifacts, use their install directory. A hook must not widen to the parent
    configuration tree.
    """
    container = artifact.parent
    return container.parent if artifact_type == "skill" else container


def access_path(artifact: Path, artifact_type: str) -> Path:
    """Resolve the install directory while retaining the artifact's own path.

    A full realpath follows symlinked artifacts into their source checkouts,
    moving the watch away from sibling installations. Resolve install-directory
    ancestors to unify paths such as /tmp and /private/tmp, but preserve names
    below it to match _walk and _under_root.
    """
    container = install_root(artifact, artifact_type)
    return Path(_real(container)) / artifact.relative_to(container)


def derive_root(artifact: Path, artifact_type: str,
                max_files: int = DEFAULT_MAX_FILES) -> tuple[Path, dict | None]:
    """Return the watch root and any narrowing fallback record.

    Use the install directory's repository when available so sibling plugins,
    commands, and repo-root scripts are watched. Compare --max-files against
    hashing workload, not tracked count. Narrow to the install directory only
    when that workload exceeds budget, and report the loss of coverage.

    Query the install directory, not the artifact's symlink target: the latter
    may identify a different tree that excludes sibling installations.
    """
    narrow = Path(_real(install_root(artifact, artifact_type)))
    toplevel = _git_toplevel(narrow)
    if toplevel is None:
        return narrow, None
    # Normalize root aliases such as /tmp and /private/tmp before containment checks.
    toplevel = Path(_real(toplevel))
    if toplevel != narrow and toplevel not in narrow.parents:
        # Do not widen to a repository that excludes the install directory.
        return narrow, None

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
    """Snapshot root, hashing only files whose pre-edit content git cannot track.

    In git mode, hash already-dirty files, untracked files, and individually
    listed ignored files. Store nothing for clean tracked files; later git
    status detects their changes. Attribution still requires declared writes.

    Hash nested repositories and submodules fully because the outer repository
    cannot inspect their contents. Record oversized or unreadable subtrees as
    unmeasurable.
    """
    excluded = {_real(p) for p in (exclude or set())}
    status = _git_status(root)
    tracked = _git_tracked(root)
    if status is None or tracked is None:
        # Without git, walk the install directory.
        files, unmeasurable = walk_manifest(root, exclude, max_files)
        return {
            "mode": "walk",
            "files": files,
            # Retain unreadable entries so verify cannot treat them as clean.
            "unmeasurable": unmeasurable,
            # Store the budget for in-process verify as well as CLI use.
            "max_files": max_files,
        }
    entries, opaque = status
    # Enforce the actual git hashing budget. derive_root has already tried
    # narrowing; exceeding the budget here must fail.
    candidates = _candidate_count(entries, opaque)
    if candidates > max_files:
        raise TooManyFiles(candidates, max_files)

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
        # Retain the snapshot budget for in-process verification of nested trees.
        "max_files": max_files,
    }


def _hash_opaque(root: Path, opaque: list[str], exclude: set[Path] | None,
                 max_files: int) -> tuple[dict[str, dict], list[dict]]:
    """Hash opaque subtrees and record those that cannot be read.

    Return ({subtree: {subtree-relative path: digest}}, unmeasurable). Record
    oversized subtrees and unreadable entries so verify cannot report them clean.
    """
    nested: dict[str, dict] = {}
    unmeasurable: list[dict] = []
    for rel in opaque:
        subtree = root / rel
        if not subtree.is_dir():
            unmeasurable.append({"path": rel, "reason": NESTED_UNMEASURABLE})
            continue
        try:
            files, inner = walk_manifest(subtree, exclude, max_files)
        except (TooManyFiles, OSError):
            unmeasurable.append({"path": rel, "reason": NESTED_UNMEASURABLE})
            continue
        nested[rel] = files
        unmeasurable.extend(
            {"path": str(Path(rel) / entry["path"]), "reason": entry["reason"]}
            for entry in inner
        )
    return nested, sorted(unmeasurable, key=lambda entry: entry["path"])


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
    """Return (modified, added, removed, git_changed, blind, unreadable).

    Detect changes only; verify attributes them using declarations. Return blind
    subtrees alongside observed changes so partial coverage retains findings.
    Raise Blind when git stops answering entirely; empty changes would imply a
    clean tree that was never checked.
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
    # Recheck saved paths missing from git status: restoring HEAD content or
    # deleting untracked files can hide real changes there. Currently opaque
    # paths belong to _classify_nested to avoid duplicate findings.
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
            # A newly dirty tracked file without a pre-image was clean at snapshot.
            # This detects a change; declarations still identify its writer.
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
    """Flatten all snapshot pre-images into root-relative paths.

    Files can move between ordinary and opaque git stores without changing
    content, eg after git init or submodule removal. Both classifiers need the
    same map to avoid falsely classifying those files as added.
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
    """Return (mod, add, rm, blind, unreadable) for currently opaque subtrees.

    Previously opaque paths now visible to git use the shared pre-image map
    and top-level classifier. Newly opaque clean tracked files have no saved
    hash; tracked_now identifies them so missing pre-images mean unknown prior
    content, not newly added files.

    Report blind reasons for subtrees unreadable at either snapshot or verify.
    """
    unmeasurable = _unmeasurable(state)
    unmeasurable_paths = {entry["path"] for entry in unmeasurable}
    # Use the snapshot budget to catch subtrees that have grown beyond it.
    limit = state.get("max_files")
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = DEFAULT_MAX_FILES
    blind = [
        f"{entry['path']!r} was not read when the snapshot was taken because "
        f"{entry['reason']}, so changes there were not checked"
        for entry in unmeasurable
    ]

    modified: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    unreadable: list[str] = []
    for rel in sorted(set(opaque_now) - unmeasurable_paths):
        subtree = root / rel
        if subtree.is_dir():
            try:
                now, inner = walk_manifest(subtree, exclude, limit)
            except (TooManyFiles, OSError):
                blind.append(
                    f"subtree {rel!r} could not be hashed at verify time, so "
                    f"changes inside it were not checked")
                continue
            blind.extend(
                f"{str(Path(rel) / entry['path'])!r} could not be read at "
                f"verify time because {entry['reason']}, so whether it "
                f"changed during the round is unknown"
                for entry in inner
            )
        else:
            # Git reports an opaque path that is not a directory; its contents remain unknown.
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
                # A clean tracked file without a saved hash predates the run, but its
                # prior content is unknown. Report unreadable rather than inventing added.
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
    """Normalize a root-relative path for comparison only.

    Normalize ./b, a/../b, and NFC/NFD spellings so declarations match detected
    paths. A mismatch would make a declared change unattributed and halt the
    run. Reports retain the original spelling so paths still open correctly.
    """
    return unicodedata.normalize("NFC", os.path.normpath(rel))


class Declaration:
    """Represent declared writes, explicit no-writes, or no declaration.

    Present with an empty set means the run says it wrote nothing; absent means
    it gave no statement and cannot pass verification. Membership is exact,
    never a directory prefix that could attribute another writer's files.
    normalize_declared rejects directories.
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


def _under_root(candidate: Path, root_real: Path) -> str | None:
    """Return candidate relative to root, preserving symlinks beneath root.

    Resolve ancestors until one matches root, retaining names below it to match
    the walk. Fully resolving an installed skill can move it into a checkout
    outside root and break scope matching. Return None when no ancestor matches;
    the caller then tries the fully resolved path to accept target-side spellings.
    """
    parts: list[str] = []
    current = candidate
    target = str(root_real)
    while True:
        if _real(current) == target:
            return os.path.join(*reversed(parts)) if parts else "."
        parent = current.parent
        if parent == current:
            return None
        parts.append(current.name)
        current = parent


def normalize_declared(raw: list[str], root: Path) -> Declaration:
    """Normalize declarations into the detected paths' namespace.

    Accept absolute, root-relative, tilde-prefixed, and symlinked spellings.
    Resolve relative paths against the watch root, not the working directory;
    prefer absolute declarations. Use _under_root to retain symlinks beneath
    root while resolving aliases such as /tmp and /private/tmp.

    Keep outside-root declarations separate: the snapshot cannot check them,
    so report them without silently discarding or attributing changes.
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
        rel = _under_root(candidate, root_real)
        if rel is None:
            try:
                rel = str(resolved.relative_to(root_real))
            except ValueError:
                outside.append(text)
                continue
        inside.add(rel)
    return Declaration(inside, outside)


def verify(root: Path, state: dict, scope: list[str],
           exclude: set[Path] | None = None,
           declared: Declaration | None = None) -> dict:
    """Compare live state with the snapshot and classify changes.

    clean requires measurable coverage and attribution with no violations.
    scope_violation identifies out-of-scope changes declared by this run.
    not_measurable covers unreadable state, undeclared changes, or absent
    attribution. A tree diff cannot distinguish this run from another writer;
    only declarations authorize attribution and a possible restore.
    """
    blind: list[str] = []
    unreadable: list[str] = []
    # Collect unreadable walk paths at both snapshots. Git-mode blind paths
    # come from _classify_nested.
    unmeasurable: list[dict] = []
    mode = state.get("mode")
    declaration = declared if declared is not None else ABSENT
    if mode == "git":
        try:
            (modified, added, removed, git_changed,
             blind, unreadable) = _classify_git(root, state, exclude)
        except Blind as exc:
            return _blind_report(root, scope, state, exc.reasons, declaration)
        # Pre-images cover files already dirty or untracked at snapshot.
        pre_image_paths = set(_pre_images(state))
    elif mode == "walk":
        manifest = state.get("files", {})
        # Bound verify by the snapshot budget, including trees that have grown.
        limit = state.get("max_files")
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = DEFAULT_MAX_FILES
        try:
            current, walked = walk_manifest(root, exclude, limit)
        except TooManyFiles as exc:
            return _blind_report(root, scope, state, [
                f"the watched tree holds more than {exc.limit} file(s) at "
                f"verify time, over the budget its snapshot was taken under, "
                f"so it was not hashed"], declaration)
        # An unreadable path at either end prevents comparison.
        unmeasurable = _unmeasurable(state) + walked
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
        # Walk hashes supply verification; git only adds informational pre-existing dirt.
        git_changed = _git_changed(root)
        # Walk-mode files have pre-images but no repository HEAD to restore.
        pre_image_paths = set(manifest)
    else:
        # Unknown modes cannot support comparison. Falling through to an empty
        # walk manifest would falsely classify the whole tree as added.
        return _blind_report(root, scope, state, [
            f"the manifest records mode {mode!r}, which this guard cannot "
            f"read, so nothing in the watched tree was compared"], declaration)

    changed = sorted(set(modified) | set(added) | set(removed))
    out_of_scope = [p for p in changed if not _in_scope(p, scope)]
    # Attribute only declared writes. Undeclared changes may belong to another writer.
    violations = sorted(p for p in out_of_scope if p in declaration)
    unattributed = sorted(p for p in out_of_scope if p not in declaration)
    # A HEAD restore is valid only for originally clean tracked files. Dirty
    # files need their saved contents; newly created files have no prior HEAD
    # content. Keep these declared violations but require manual restoration.
    added_set = set(added)
    violations_manual = sorted(
        p for p in violations if p in pre_image_paths or p in added_set)
    declared_out_of_scope = sorted(
        p for p in (declaration.paths or set()) if not _in_scope(p, scope))
    in_scope_new = sorted(p for p in added if _in_scope(p, scope))
    # Undeclared in-scope changes show an incomplete declaration, which cannot
    # reliably attribute out-of-scope changes.
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
    # A declared out-of-scope write without a detected change blocks clean,
    # but provides no basis to restore or delete a possibly pre-existing file.
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
    # Unknown file content at either end is unmeasurable outside scope.
    # In-scope files remain permitted edits.
    blind.extend(
        f"{path!r} could not be read, so whether it changed during the round "
        f"is unknown" for path in sorted(unreadable) if not _in_scope(path, scope)
    )
    # Unwalkable entries (module cases 4-8) are unmeasurable outside scope.
    # An in-scope unreadable directory hides only permitted descendants because
    # _in_scope matches prefixes.
    seen: set[tuple[str, str]] = set()
    for entry in sorted(unmeasurable, key=lambda item: item["path"]):
        key = (entry["path"], entry["reason"])
        if key in seen or _in_scope(entry["path"], scope):
            continue
        seen.add(key)
        blind.append(
            f"{entry['path']!r} could not be read during the round because "
            f"{entry['reason']}, so whether anything changed there is unknown")
    # An ignored scope makes authorized edits invisible and cannot pass verification.
    blind.extend(
        f"the permitted scope {entry!r} lies under an ignored directory name, "
        f"so nothing inside it was read" for entry in scope
        if _is_ignored(Path(entry))
    )

    preexisting = []
    if git_changed is not None:
        # A dirty file matching its snapshot predates this run. Report it as
        # context, never a violation or restoration target.
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
        # These violations need saved-content restoration; HEAD would discard
        # pre-existing edits or cannot restore an untracked file.
        "violations_manual_revert": violations_manual,
        # Undeclared out-of-scope changes: report without restoring.
        "unattributed_out_of_scope": unattributed,
        # Changed in scope without being declared. The declaration is incomplete.
        "undeclared_in_scope": undeclared_in_scope,
        # Dirty in git before this run started. Informational: never revert these.
        "preexisting_dirty_out_of_scope": preexisting,
        # Include all declared out-of-scope writes, even without detected changes.
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
    """Return a non-passing report when the tree could not be read.

    Keep the normal schema with empty change lists; these do not imply clean.
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
    """Compare the recorded root with root, literally then after resolution.

    Resolution accepts symlinked and tilde-prefixed spellings.
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
    """Make text printable under the terminal encoding.

    Filesystem paths can contain surrogate escapes. Sanitize human-readable
    output to avoid UnicodeEncodeError; JSON already escapes surrogates.
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
                        help=f"Budget for the files a snapshot hashes "
                             f"(default {DEFAULT_MAX_FILES}): every file under "
                             f"the root outside a repo, the dirty and untracked "
                             f"ones inside one. Inside a repo the root falls "
                             f"back to the install directory and says so; when "
                             f"it cannot, the snapshot bails.")
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
        artifact = Path(os.path.abspath(str(Path(args.artifact).expanduser())))
        if not artifact.exists():
            _fail(f"artifact not found: {artifact}")
        if artifact.is_dir():
            _fail(f"--artifact must be a file, not a directory: {artifact}. "
                  "For a skill, pass the SKILL.md inside it.")
        # Preserve the install namespace instead of following artifact symlinks
        # into their source checkouts; see access_path.
        artifact = access_path(artifact, args.artifact_type)

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
    real_root = Path(_real(root))
    resolved: list[str] = []
    for index, scope_abs in enumerate(permitted_scopes(artifact, artifact_type)):
        try:
            resolved.append(str(scope_abs.relative_to(real_root)))
        except ValueError:
            if index == 0:
                _fail(f"artifact {artifact} is not inside the watch root {root}")
                return []  # unreachable; _fail exits
            # Companions outside the watch need no scope entry; an artifact outside
            # the watch is an error.
    return resolved


def _declared_paths(args) -> list[str] | None:
    """Return CLI declarations, or None when absent.

    --declared-file accepts a list, {"edited_paths": [...]}, or workflow state
    with applied_edits.edited_paths. Missing declaration fields are usage errors,
    not an implicit claim that the run wrote nothing.
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
            # Empty edited_paths is not an implicit --declared-none. Handoff state
            # requires non-empty paths with edit_count >= 1.
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
        _fail(f"watch root {root} needs to hash {exc.count} file(s), over "
              f"--max-files {exc.limit}, and cannot be narrowed further; pass "
              f"a smaller --root or raise --max-files")
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
    unmeasurable = _unmeasurable(state)
    report = {
        "verdict": "snapshot",
        "files_recorded": recorded,
        "manifest": args.manifest,
        "root": str(root),
        "scope": scope,
        "mode": state.get("mode"),
        "nested_repos": sorted(state.get("nested", {}) or {}),
        # Keep reasons in the manifest and text output; this payload lists paths only.
        "unmeasurable": [entry["path"] for entry in unmeasurable],
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
        for entry in unmeasurable:
            print(f"  WARNING: {_display(entry['path'])} could not be read "
                  f"because {entry['reason']}; --verify will report "
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

    # Reload root and scope from the manifest; shell state does not persist
    # between snapshot and verify calls.
    if args.root:
        root = Path(args.root).expanduser()
        # Reject root mismatches; comparing different trees would fabricate
        # removed and added paths across both.
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
        # Include unattributed changes so the summary does not imply none occurred.
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
            # Only walk manifests provide whole-tree hashes. If git-mode verification
            # loses git, nothing was checked and the verdict is not_measurable.
            print("  NOTE: git unavailable here; hash manifest was the only check")

    sys.exit(VERDICT_EXITS.get(report["verdict"], 1))


def _cli() -> None:
    """Run main, mapping uncaught errors to exit 2 rather than exit 1.

    Exit 1 means scope_violation and needs a readable violations report. On an
    unexpected error, halt without restoring files. Preserve SystemExit so
    intentional verdict codes reach the shell.
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
