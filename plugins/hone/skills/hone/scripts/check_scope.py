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

Exit codes: 0 clean, 1 scope violation, 2 usage error.

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
from pathlib import Path

# Files that change as a side effect of normal tool use and would otherwise
# produce a violation on every run.
IGNORED_NAMES = {".DS_Store"}
IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache"}

# Backup and working files hone and workout create by design.
IGNORED_SUFFIXES = (".pyc", ".pre-hone", ".pre-workout", ".pre-audit", ".pre-enrich")

ARTIFACT_TYPES = ("skill", "command", "hook", "script")

DEFAULT_MAX_FILES = 20000

# Sentinel for "this file existed at snapshot time and was clean in git, so no
# hash was stored". Git could attribute any later change to this run on its
# own, which is exactly why hashing it would have been wasted work.
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


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


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
    """Run git in `root` and return stdout, or None when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


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


def _git_file_count(root: Path) -> int | None:
    """How many files git would enumerate under `root` (tracked + untracked).

    This is the cost estimate `--max-files` is compared against. It is two
    index/dir-walk queries, not a hash of anything.
    """
    tracked = _git(root, "ls-files", "-z")
    others = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or others is None:
        return None
    return len(_nul_list(tracked)) + len(_nul_list(others))


def _git_status_entries(root: Path) -> list[tuple[str, str]] | None:
    """(status code, root-relative path) for everything git reports dirty.

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
    for line in stdout.splitlines():
        if len(line) <= 3:
            continue
        code = line[:2]
        entry = _porcelain_path(line)
        if entry.endswith("/"):
            # A collapsed directory (an ignored tree). Not a file path.
            continue
        try:
            relative = (toplevel_resolved / entry).relative_to(root_resolved)
        except ValueError:
            # Dirty, but outside the guarded tree entirely. Not this run's
            # business either way, and reporting it would be noise.
            continue
        entries.append((code, str(relative)))
    return entries


def _git_changed(root: Path) -> list[str] | None:
    """Root-relative paths git reports as dirty, or None outside a repo.

    `.gitignore`d entries are excluded: they are tracked for *attribution*
    (see `_git_status_entries`) but calling them "dirty" would flood
    `preexisting_dirty_out_of_scope` with build output.
    """
    entries = _git_status_entries(root)
    if entries is None:
        return None
    return [path for code, path in entries if code != "!!"]


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
    return out.decode("utf-8", "replace")


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
    cannot attribute; when even the enumeration is too big, narrow to the
    install directory and say so out loud.
    """
    narrow = install_root(artifact, artifact_type)
    toplevel = _git_toplevel(artifact.parent)
    if toplevel is None:
        return narrow, None
    # `artifact` arrives already resolved; put the repo root in the same
    # namespace or the `is this below that` test below compares /tmp against
    # /private/tmp and silently declines to narrow.
    toplevel = Path(_real(toplevel))

    count = _git_file_count(toplevel)
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
    """
    excluded = {_real(p) for p in (exclude or set())}
    entries = _git_status_entries(root)
    tracked = _git_tracked(root)
    if entries is None or tracked is None:
        # Not a repository (or git is unavailable): walk it. These roots are
        # install directories, which are small.
        return {
            "mode": "walk",
            "files": build_manifest(root, exclude, max_files),
        }

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
    return {
        "mode": "git",
        "dirty_tracked": dirty_tracked,
        "untracked": untracked,
    }


def state_size(state: dict) -> int:
    if state.get("mode") == "git":
        return len(state.get("dirty_tracked", {})) + len(state.get("untracked", {}))
    return len(state.get("files", {}))


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def _classify_git(root: Path, state: dict,
                  exclude: set[Path] | None) -> tuple[list[str], list[str],
                                                      list[str], list[str] | None]:
    """(modified, added, removed, git_changed) for a git-mode manifest."""
    excluded = {_real(p) for p in (exclude or set())}
    dirty_snapshot = state.get("dirty_tracked", {})
    untracked_snapshot = state.get("untracked", {})

    entries = _git_status_entries(root)
    tracked_now = _git_tracked(root)
    if entries is None or tracked_now is None:
        return [], [], [], None

    dirty_now = {rel for _code, rel in entries if not _is_ignored(Path(rel))}
    # A file recorded at snapshot that git no longer reports still needs
    # checking: reverting a dirty file to its HEAD content, or deleting an
    # untracked one, both drop it out of `git status` while being changes this
    # run made.
    candidates = dirty_now | set(dirty_snapshot) | set(untracked_snapshot)

    modified: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    for rel in sorted(candidates):
        if _is_ignored(Path(rel)):
            continue
        path = root / rel
        if _real(path) in excluded:
            continue
        if rel in dirty_snapshot:
            before: object = dirty_snapshot[rel]
            existed = before is not None
        elif rel in untracked_snapshot:
            before = untracked_snapshot[rel]
            existed = before is not None
        elif rel in tracked_now:
            # Tracked now and absent from the dirty snapshot means it was
            # clean when we started. Git says it is dirty now, so this run is
            # the only thing that can have changed it -- no stored hash needed.
            before, existed = CLEAN_TRACKED, True
        else:
            before, existed = None, False

        exists_now = path.is_file()
        if existed and not exists_now:
            removed.append(rel)
        elif not existed and exists_now:
            added.append(rel)
        elif existed and exists_now:
            if before is CLEAN_TRACKED:
                modified.append(rel)
            elif _hash_file(path) != before:
                modified.append(rel)

    git_changed = [path for code, path in entries if code != "!!"]
    return modified, added, removed, git_changed


def verify(root: Path, state: dict, scope: list[str],
           exclude: set[Path] | None = None) -> dict:
    """Compare the live tree against a snapshot and bucket every change."""
    if state.get("mode") == "git":
        modified, added, removed, git_changed = _classify_git(root, state, exclude)
    else:
        manifest = state.get("files", {})
        current = build_manifest(root, exclude)
        modified = sorted(
            p for p in set(manifest) & set(current) if manifest[p] != current[p]
        )
        added = sorted(set(current) - set(manifest))
        removed = sorted(set(manifest) - set(current))
        git_changed = _git_changed(root)

    changed = sorted(set(modified) | set(added) | set(removed))
    violations = sorted(p for p in changed if not _in_scope(p, scope))
    in_scope_new = sorted(p for p in added if _in_scope(p, scope))

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

    clean = not violations
    report = {
        "verdict": "clean" if clean else "scope_violation",
        "root": str(root),
        "scope": scope,
        "modified_in_scope": sorted(p for p in modified if _in_scope(p, scope)),
        "new_files_in_scope": in_scope_new,
        "violations": violations,
        # Dirty in git before this run started. Informational: never revert these.
        "preexisting_dirty_out_of_scope": preexisting,
        "git_available": git_changed is not None,
        "counts": {
            "modified": len(modified),
            "added": len(added),
            "removed": len(removed),
            "violations": len(violations),
            "preexisting_dirty": len(preexisting),
        },
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
        f"{fallback.get('intended_root')} holds {fallback.get('candidate_count')} "
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
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true", help="Record pre-edit state")
    group.add_argument("--verify", action="store_true", help="Check against manifest")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    if bool(args.artifact) != bool(args.artifact_type):
        _fail("--artifact and --type must be given together")

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
    report = {
        "verdict": "snapshot",
        "files_recorded": recorded,
        "manifest": args.manifest,
        "root": str(root),
        "scope": scope,
        "mode": state.get("mode"),
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

    report = verify(root, payload, scope, excluded)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']} (root: {root}; "
              f"scope: {', '.join(report['scope'])})")
        counts = report["counts"]
        print(f"  {counts['modified']} modified, {counts['added']} added, "
              f"{counts['removed']} removed, {counts['violations']} out of scope")
        for path in report["violations"]:
            print(f"  VIOLATION: {path}")
        preexisting = report["preexisting_dirty_out_of_scope"]
        if preexisting:
            print(f"  NOTE: {len(preexisting)} file(s) were already uncommitted "
                  "before this run and are unchanged by it. Do not revert them:")
            for path in preexisting:
                print(f"    pre-existing: {path}")
        if report.get("root_fallback"):
            print("  " + _fallback_note(report["root_fallback"]))
        if not report["git_available"]:
            print("  NOTE: git unavailable here; hash manifest was the only check")

    sys.exit(0 if report["verdict"] == "clean" else 1)


if __name__ == "__main__":
    main()
