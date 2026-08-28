#!/usr/bin/env python3
"""Scope guard for hone Phase 2 edits.

Preference 11 (stale-write protection) stops hone from clobbering a file
someone else changed. Nothing stops hone from changing a file it was never
asked to touch: a shared reference, a sibling skill, a script two directories
over. Those edits are invisible in the run report and land in the working tree
next to the intended ones.

Git alone is not enough. A file git does not track produces no diff, so an
edit to an untracked file inside or outside scope is silent. This script
therefore pairs a git diff (for tracked files) with a content-hash manifest
(for untracked ones), which is the arrangement trailofbits/skills skill-improver
arrived at for the same reason.

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
import subprocess
import sys
from pathlib import Path

# Files that change as a side effect of normal tool use and would otherwise
# produce a violation on every run.
IGNORED_NAMES = {".DS_Store"}
IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache"}

# Backup and working files hone and workout create by design.
IGNORED_SUFFIXES = (".pyc", ".pre-hone", ".pre-workout", ".pre-audit", ".pre-enrich")


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


def _walk(root: Path, exclude: set[Path] | None = None) -> list[Path]:
    exclude = exclude or set()
    return [
        p for p in root.rglob("*")
        if p.is_file()
        and p.resolve() not in exclude
        and not _is_ignored(p.relative_to(root))
    ]


def build_manifest(root: Path, exclude: set[Path] | None = None) -> dict:
    """Hash every non-ignored file under root.

    `exclude` holds resolved paths to skip. The manifest itself belongs there:
    writing it inside the guarded tree would otherwise register as a new
    out-of-scope file on the very next verify.
    """
    return {
        str(p.relative_to(root)): _hash_file(p)
        for p in _walk(root, exclude)
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


def _git_changed(root: Path) -> list[str] | None:
    """Paths git reports as dirty, relative to `root`, or None outside a repo.

    `git status --porcelain` prints paths relative to the *repository* root,
    never to `--root`. The manifest, `--scope`, and `_in_scope` all speak
    root-relative paths, so comparing the two namespaces directly matched
    nothing whenever root sits below the repo root -- which is the documented
    invocation (`--root ~/.claude/skills` inside a repo rooted at
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
    """
    stdout = _git(root, "status", "--porcelain", "--untracked-files=all")
    if stdout is None:
        return None
    toplevel = _git_toplevel(root)
    if toplevel is None:
        return None
    try:
        root_resolved = root.resolve()
        toplevel_resolved = toplevel.resolve()
    except OSError:
        return None

    paths = []
    for line in stdout.splitlines():
        if len(line) <= 3:
            continue
        entry = _porcelain_path(line)
        try:
            relative = (toplevel_resolved / entry).relative_to(root_resolved)
        except ValueError:
            # Dirty, but outside the guarded tree entirely. Not this run's
            # business either way, and reporting it would be noise.
            continue
        paths.append(str(relative))
    return paths


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


def verify(root: Path, manifest: dict, scope: list[str],
           exclude: set[Path] | None = None) -> dict:
    current = build_manifest(root, exclude)

    modified = sorted(
        p for p in set(manifest) & set(current) if manifest[p] != current[p]
    )
    added = sorted(set(current) - set(manifest))
    removed = sorted(set(manifest) - set(current))

    changed = modified + added + removed
    violations = sorted(p for p in changed if not _in_scope(p, scope))
    in_scope_new = sorted(p for p in added if _in_scope(p, scope))

    git_changed = _git_changed(root)
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
        preexisting = sorted(
            p for p in git_changed
            if p not in set(changed) and not _is_ignored(Path(p))
            and not _in_scope(p, scope)
        )

    clean = not violations
    return {
        "verdict": "clean" if clean else "scope_violation",
        "scope": scope,
        "modified_in_scope": [p for p in modified if _in_scope(p, scope)],
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Scope guard for hone Phase 2 edits")
    parser.add_argument("--root", default=str(Path.home() / ".claude" / "skills"),
                        help="Directory tree to guard (default: ~/.claude/skills)")
    parser.add_argument("--manifest", required=True,
                        help="Path to the manifest file to write or read")
    parser.add_argument("--scope", action="append", default=[],
                        help="Repeatable. Root-relative path the run may change.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true", help="Record pre-edit state")
    group.add_argument("--verify", action="store_true", help="Check against manifest")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"ERROR: root is not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    manifest_path = Path(args.manifest).expanduser()
    try:
        excluded = {manifest_path.resolve()}
    except OSError:
        excluded = set()

    if args.snapshot:
        manifest = build_manifest(root, excluded)
        payload = {"root": str(root), "files": manifest}
        try:
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot write manifest {args.manifest}: {exc}", file=sys.stderr)
            sys.exit(2)
        report = {"verdict": "snapshot", "files_recorded": len(manifest),
                  "manifest": args.manifest, "root": str(root)}
        if args.json:
            json.dump(report, sys.stdout, indent=2)
            print()
        else:
            print(f"SNAPSHOT: {len(manifest)} file(s) recorded to {args.manifest}")
        sys.exit(0)

    if not args.scope:
        print(
            "ERROR: --verify needs at least one --scope entry naming what the "
            "run was allowed to change (for example --scope hone)",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR: manifest not found: {args.manifest}; run --snapshot first",
              file=sys.stderr)
        sys.exit(2)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read manifest {args.manifest}: {exc}", file=sys.stderr)
        sys.exit(2)

    report = verify(root, payload.get("files", {}), args.scope, excluded)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"VERDICT: {report['verdict']} (scope: {', '.join(report['scope'])})")
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
        if not report["git_available"]:
            print("  NOTE: git unavailable here; hash manifest was the only check")

    sys.exit(0 if report["verdict"] == "clean" else 1)


if __name__ == "__main__":
    main()
