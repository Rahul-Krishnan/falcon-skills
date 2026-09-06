# Agent Skills Open Standard requirements

Requirements from [agentskills.io/specification](https://agentskills.io/specification) for evaluating and improving skills.

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

Move non-spec frontmatter fields to `metadata`; root-level custom fields violate the spec.

## Description Best Practices

- **Use imperative phrasing:** "Use this skill when..." not "This skill does..."
- **Focus on user intent, not implementation:** Describe what the user is trying to achieve.
- **Activation:** List applicable contexts, including non-obvious cases.
- **Include anti-patterns:** Describe when NOT to use the skill (overlap with similar skills).
- **Length:** A few sentences; at most 1024 chars.

## Progressive Disclosure

Three levels of context loading:

1. **Metadata** (~100 tokens): `name` + `description` loaded at startup for ALL skills.
2. **Instructions** (< 5000 tokens recommended): Full SKILL.md body loaded when skill activates.
3. **Resources** (as needed): Files in `scripts/`, `references/`, `assets/` loaded only when required.

**SKILL.md body should be under 500 lines.** Move detailed reference material to `references/`.

## Body Content Best Practices

- **Assume common knowledge.** Add only what the agent needs.
- **Scope:** One skill per coherent unit of work.
- **Detail:** Give concise steps.
- **Match specificity to fragility.** Be prescriptive for fragile operations, flexible for judgment calls.
- **Provide defaults, not menus.** Pick one approach, mention alternatives briefly.
- **Favor procedures over declarations.** Teach how to approach problems, not what to produce.

## Patterns

- **Gotchas:** Corrections to predictable mistakes.
- **Templates:** Output format examples.
- **Checklists:** Track progress in multi-step workflows.
- **Validation loops:** Do work → validate → fix → repeat.
- **Plan-validate-execute:** For batch/destructive operations.

## File References

Use paths relative to the skill root, one level deep. These examples illustrate
the syntax; they do not reference this skill:
```markdown
See [the reference guide](references/REFERENCE.md) for details.
Run: scripts/structural_audit.py
```

## Validation

```bash
skills-ref validate ./my-skill
```
