# Agent Skills Open Standard — Key Requirements

Extracted from [agentskills.io/specification](https://agentskills.io/specification) for use by hone when evaluating and improving skills.

## Directory Structure

```
skill-name/
├── SKILL.md          # Required: metadata + instructions
├── scripts/          # Optional: executable code
├── references/       # Optional: documentation
├── assets/           # Optional: templates, resources
└── ...               # Any additional files or directories
```

## Frontmatter (YAML)

| Field | Required | Constraints |
|-------|----------|-------------|
| `name` | Yes | 1-64 chars. Lowercase letters, numbers, hyphens only. No start/end hyphen. No consecutive hyphens. Must match parent directory name. |
| `description` | Yes | 1-1024 chars. Non-empty. Describes what the skill does AND when to use it. |
| `license` | No | License name or reference to bundled file. |
| `compatibility` | No | 1-500 chars. Environment requirements. |
| `metadata` | No | Arbitrary key-value map (string→string). For custom fields. |
| `allowed-tools` | No | Space-delimited list of pre-approved tools. Experimental. |

**Any non-spec field at the frontmatter root level is a violation.** Move custom fields to `metadata`.

## Description Best Practices

- **Use imperative phrasing:** "Use this skill when..." not "This skill does..."
- **Focus on user intent, not implementation:** Describe what the user is trying to achieve.
- **Be pushy about activation:** List contexts where the skill applies, including non-obvious cases.
- **Include anti-patterns:** Describe when NOT to use the skill (overlap with similar skills).
- **Keep concise:** A few sentences to a short paragraph. Hard limit: 1024 chars.

## Progressive Disclosure

Three levels of context loading:

1. **Metadata** (~100 tokens): `name` + `description` loaded at startup for ALL skills.
2. **Instructions** (< 5000 tokens recommended): Full SKILL.md body loaded when skill activates.
3. **Resources** (as needed): Files in `scripts/`, `references/`, `assets/` loaded only when required.

**SKILL.md body should be under 500 lines.** Move detailed reference material to `references/`.

## Body Content Best Practices

- **Add what the agent lacks, omit what it knows.** Don't explain common concepts.
- **Design coherent units.** One skill = one coherent unit of work.
- **Aim for moderate detail.** Concise stepwise guidance > exhaustive documentation.
- **Match specificity to fragility.** Be prescriptive for fragile operations, flexible for judgment calls.
- **Provide defaults, not menus.** Pick one approach, mention alternatives briefly.
- **Favor procedures over declarations.** Teach how to approach problems, not what to produce.

## Patterns

- **Gotchas sections:** Concrete corrections to mistakes the agent WILL make.
- **Templates:** Concrete output format examples (pattern-match friendly).
- **Checklists:** Track progress in multi-step workflows.
- **Validation loops:** Do work → validate → fix → repeat.
- **Plan-validate-execute:** For batch/destructive operations.

## File References

Use relative paths from skill root. Keep one level deep. The paths in the block
below are illustrative spec syntax, not live references into this skill.
```markdown
See [the reference guide](references/REFERENCE.md) for details.
Run: scripts/structural_audit.py
```

## Validation

```bash
skills-ref validate ./my-skill
```
