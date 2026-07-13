#!/usr/bin/env python3
"""Validate that every plugin in this marketplace follows the canonical layout.

The canonical layout:

    plugins/<name>/
      .claude-plugin/plugin.json         required
      skills/<name>/                     required, dir name == plugin name
        SKILL.md                         required
        evals/eval_criteria.json         required
        references/                      optional

`references/` is deliberately unchecked. Only hindsight has content worth splitting
out, and requiring empty dirs elsewhere would be cargo-culting.

Descriptions are checked for presence, not for prose. marketplace.json, plugin.json,
and SKILL.md each describe the same plugin to a different reader (a browser, the
loader, and the model deciding whether to invoke), so their wording should differ.
What must agree across all three is the plugin `name`.

Malformed input must produce a violation, never a traceback. This runs in CI, where a
stack trace tells you far less than "plugin.json: 'author' must be an object". Every
value read out of JSON is therefore type-checked before use.

Stdlib only, so CI needs no install step.

Usage: python3 scripts/validate_structure.py [repo_root]
Exit 0 when clean, 1 with a violation list otherwise.
"""

import json
import re
import sys
from pathlib import Path

EVAL_DIMENSIONS = {
    "task_completion",
    "invocation",
    "efficiency",
    "best_practices",
    "business_impact",
}
PLUGIN_JSON_FIELDS = ["name", "description", "version", "keywords"]
TEST_CASE_FIELDS = ["id", "name", "prompt", "checks"]

KEY_RE = re.compile(r"^( *)([\w-]+):\s*(.*?)\s*$")
SEQ_ITEM_RE = re.compile(r"^( *)-\s+(.*?)\s*$")
BLOCK_SCALARS = (">", "|", ">-", "|-", ">+", "|+")


class Violations:
    def __init__(self):
        self.items = []

    def add(self, path, message):
        self.items.append((str(path), message))

    def report(self, root):
        for path, message in self.items:
            try:
                rel = Path(path).relative_to(root)
            except ValueError:
                rel = path
            print(f"  {rel}: {message}")


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_json(path, v):
    """Read and parse a JSON file. Returns None (after logging) on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        v.add(path, "not valid UTF-8")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        v.add(path, f"invalid JSON: {exc}")
        return None


def parse_frontmatter(skill_md):
    """Return (fields, error) for the YAML frontmatter of a SKILL.md.

    Hand-rolled rather than PyYAML so CI stays dependency-free, which means it must be
    conservative about what it accepts. Two rules do the heavy lifting:

    Indent depth is tracked, not just a "somewhere under metadata" flag. Keys nested
    below `metadata:`'s immediate children are ignored, so a stray `allowed-tools`
    buried three levels deep in an unrelated block cannot satisfy the metadata check.

    Tabs in the indentation are rejected outright. YAML forbids them, so a real parser
    would fail on the file; accepting it here would green-light a SKILL.md that the
    actual loader cannot read.

    Top-level keys land as "key", metadata's immediate children as "metadata.key".
    Block scalars (`>`, `|`) become the sentinel BLOCK: for `description` that is a
    violation to report, not a value to read. Block sequences (`- item` lines) are
    joined into a comma-separated string, since callers only test them for presence.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "not valid UTF-8"

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing YAML frontmatter (no opening ---)"
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None, "unterminated YAML frontmatter (no closing ---)"

    body = lines[1:end]
    for line in body:
        if "\t" in line[: len(line) - len(line.lstrip())]:
            return None, "frontmatter indented with tabs; YAML forbids tab indentation"

    fields = {}
    in_metadata = False
    metadata_indent = None
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue

        match = KEY_RE.match(line)
        if not match:
            i += 1
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3)

        if indent == 0:
            in_metadata = key == "metadata"
            metadata_indent = None
            name = key
        elif in_metadata:
            if metadata_indent is None:
                metadata_indent = indent
            if indent != metadata_indent:
                i += 1  # nested deeper than metadata's own children; not a metadata key
                continue
            name = f"metadata.{key}"
        else:
            i += 1  # continuation of a top-level block scalar
            continue

        if value in BLOCK_SCALARS:
            fields[name] = "BLOCK"
        elif value:
            fields[name] = value
        else:
            items, j = [], i + 1
            while j < len(body):
                seq = SEQ_ITEM_RE.match(body[j])
                # YAML allows sequence items at the same indent as their key, not just
                # deeper. A sibling key cannot be mistaken for one: it has no leading "-".
                if seq and len(seq.group(1)) >= indent:
                    items.append(unquote(seq.group(2)))
                    j += 1
                elif not body[j].strip():
                    j += 1
                else:
                    break
            fields[name] = ", ".join(items)
            if items:
                i = j - 1
        i += 1

    return fields, None


def check_plugin_json(plugin_dir, name, v):
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        v.add(manifest, "missing (every plugin needs a .claude-plugin/plugin.json)")
        return
    data = load_json(manifest, v)
    if data is None:
        return
    if not isinstance(data, dict):
        v.add(manifest, f"must be a JSON object, found {type(data).__name__}")
        return

    for field in PLUGIN_JSON_FIELDS:
        if not data.get(field):
            v.add(manifest, f"missing or empty required field '{field}'")

    author = data.get("author")
    if not isinstance(author, dict):
        v.add(manifest, "'author' must be an object with a 'name' (eg {\"name\": \"...\"})")
    elif not author.get("name"):
        v.add(manifest, "missing or empty required field 'author.name'")

    if data.get("name") and data["name"] != name:
        v.add(manifest, f"name '{data['name']}' does not match plugin dir '{name}'")


def check_skill_md(skill_dir, name, v):
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        v.add(skill_md, "missing (every skill needs a SKILL.md)")
        return

    fields, error = parse_frontmatter(skill_md)
    if error:
        v.add(skill_md, error)
        return

    if "name" not in fields:
        v.add(skill_md, "frontmatter missing 'name'")
    elif unquote(fields["name"]) != name:
        v.add(skill_md, f"frontmatter name '{unquote(fields['name'])}' does not match plugin '{name}'")

    description = fields.get("description")
    if not description:
        v.add(skill_md, "frontmatter missing or empty 'description'")
    elif description == "BLOCK":
        v.add(
            skill_md,
            "description uses a folded/literal block (> or |); use a quoted string. "
            "Block scalars inject newlines into the string the model matches on to "
            "decide whether to invoke this skill.",
        )

    if "metadata.user-invocable" not in fields:
        v.add(skill_md, "frontmatter missing 'metadata.user-invocable'")
    if not fields.get("metadata.allowed-tools"):
        v.add(
            skill_md,
            "frontmatter missing or empty 'metadata.allowed-tools'; without it the "
            "skill inherits the full default toolset",
        )
    # metadata.argument-hint is optional: only skills that take arguments need one.


def check_evals(skill_dir, name, v):
    evals = skill_dir / "evals" / "eval_criteria.json"
    if not evals.is_file():
        v.add(evals, "missing (every skill needs an eval suite)")
        return
    data = load_json(evals, v)
    if data is None:
        return
    if not isinstance(data, dict):
        v.add(evals, f"must be a JSON object, found {type(data).__name__}")
        return

    if not data.get("project"):
        v.add(evals, "missing or empty 'project'")
    if data.get("skill_name") != name:
        v.add(evals, f"skill_name '{data.get('skill_name')}' does not match plugin '{name}'")

    check_test_cases(evals, data.get("test_cases"), v)
    check_dimensions(evals, data.get("dimensions"), v)


def check_test_cases(evals, test_cases, v):
    if not test_cases:
        v.add(evals, "missing or empty 'test_cases'")
        return
    if not isinstance(test_cases, list):
        v.add(evals, f"'test_cases' must be a list, found {type(test_cases).__name__}")
        return
    for index, case in enumerate(test_cases):
        if not isinstance(case, dict):
            v.add(evals, f"test case at index {index} must be an object, found {type(case).__name__}")
            continue
        missing = [f for f in TEST_CASE_FIELDS if not case.get(f)]
        if missing:
            v.add(evals, f"test case '{case.get('id', index)}' missing {', '.join(missing)}")
    # required_present / required_absent are per-case assertions, not schema. A case with
    # nothing to forbid should not carry an empty list saying so.


def check_dimensions(evals, dimensions, v):
    if not dimensions:
        v.add(evals, "missing 'dimensions' (the eval runner has no weights without it)")
        return
    if not isinstance(dimensions, dict):
        v.add(evals, f"'dimensions' must be an object, found {type(dimensions).__name__}")
        return

    if set(dimensions) != EVAL_DIMENSIONS:
        detail = []
        if EVAL_DIMENSIONS - set(dimensions):
            detail.append(f"missing {sorted(EVAL_DIMENSIONS - set(dimensions))}")
        if set(dimensions) - EVAL_DIMENSIONS:
            detail.append(f"unexpected {sorted(set(dimensions) - EVAL_DIMENSIONS)}")
        v.add(evals, f"dimensions keys wrong: {', '.join(detail)}")

    bad = [k for k, w in dimensions.items() if isinstance(w, bool) or not isinstance(w, (int, float))]
    if bad:
        v.add(evals, f"dimension weights must be numbers; {sorted(bad)} are not")
        return
    total = sum(dimensions.values())
    if abs(total - 1.0) > 1e-6:
        v.add(evals, f"dimension weights sum to {total}, expected 1.0")


def check_marketplace(root, plugin_names, v):
    marketplace = root / ".claude-plugin" / "marketplace.json"
    if not marketplace.is_file():
        v.add(marketplace, "missing")
        return
    data = load_json(marketplace, v)
    if data is None:
        return
    if not isinstance(data, dict):
        v.add(marketplace, f"must be a JSON object, found {type(data).__name__}")
        return

    entries = data.get("plugins")
    if not isinstance(entries, list):
        v.add(marketplace, f"'plugins' must be a list, found {type(entries).__name__}")
        return

    listed = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            v.add(marketplace, f"plugin entry at index {index} must be an object, found {type(entry).__name__}")
            continue
        name = entry.get("name")
        if not name:
            v.add(marketplace, f"plugin entry at index {index} missing 'name'")
            continue
        listed.add(name)
        if not entry.get("description"):
            v.add(marketplace, f"plugin '{name}' missing 'description'")
        source = entry.get("source")
        if not isinstance(source, str) or not source:
            v.add(marketplace, f"plugin '{name}' has a missing or non-string 'source'")
        elif not (root / source).is_dir():
            v.add(marketplace, f"plugin '{name}' source '{source}' is not a directory")

    for name in sorted(plugin_names - listed):
        v.add(marketplace, f"plugin '{name}' exists on disk but is not listed")
    for name in sorted(listed - plugin_names):
        v.add(marketplace, f"plugin '{name}' is listed but has no directory under plugins/")


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    plugins_dir = root / "plugins"
    if not plugins_dir.is_dir():
        print(f"No plugins/ directory under {root}", file=sys.stderr)
        return 1

    v = Violations()
    plugin_dirs = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
    names = {p.name for p in plugin_dirs}

    for plugin_dir in plugin_dirs:
        name = plugin_dir.name
        check_plugin_json(plugin_dir, name, v)

        skill_dir = plugin_dir / "skills" / name
        if not skill_dir.is_dir():
            v.add(skill_dir, f"missing (skill dir must be named after the plugin: skills/{name}/)")
            continue
        check_skill_md(skill_dir, name, v)
        check_evals(skill_dir, name, v)

    check_marketplace(root, names, v)

    if v.items:
        print(f"{len(v.items)} structure violation(s) across {len(plugin_dirs)} plugin(s):\n")
        v.report(root)
        print("\nCanonical layout is documented in README.md.")
        return 1

    print(f"{len(plugin_dirs)} plugin(s) match the canonical layout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
