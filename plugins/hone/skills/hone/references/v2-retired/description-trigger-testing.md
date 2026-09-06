# Description Trigger Testing

Reference for hone Phase 2 Step 4.5: testing whether a skill's description triggers correctly on realistic prompts.

## Purpose

A skill with perfect body content but a bad description never gets activated. After improving a skill's body, verify its description still triggers correctly.

## Methodology

### 1. Generate Trigger Eval Queries

Read the artifact's current `description` and body content. Generate two sets:

**Should-trigger queries (8-10):** Realistic user prompts that should activate this skill.
- Vary phrasing: direct commands, questions, partial matches
- Include non-obvious triggers mentioned in the description
- Include common synonyms for the skill's purpose
- Example for `hone`: "evaluate my recap skill", "improve the checkpoint command", "run eval on my hook"

**Should-not-trigger queries (8-10):** Near-miss prompts that share keywords but need a different skill.
- Similar domain but different action (e.g., "create a new skill" vs "evaluate a skill")
- Overlapping keywords with different intent
- Prompts that should route to a related but distinct skill
- Example for `hone`: "create a new skill called hone", "review this diff" (→ temper-review), "run tests" (→ temper-code)

### 2. Collect Competing Descriptions

Read `name` and `description` from all skills in discovery paths:
```bash
for dir in ~/.claude/skills/*/; do
  name=$(basename "$dir")
  desc=$(head -20 "$dir/SKILL.md" | grep -A5 "description:" | head -5)
  echo "$name: $desc"
done
```

This creates the catalog the LLM uses to decide which skill to activate.

### 3. Test Trigger Rates

For each query, present it alongside the full skill catalog:

> Given this user prompt: "{query}"
> And these available skills: [catalog]
> Which skill(s) would you activate? List skill names only.

Run 3 times per query (different temperature samples). Record trigger rate per query.

### 4. Score Results

- **Should-trigger:** trigger rate > 0.5 = PASS (skill activated in majority of samples)
- **Should-not-trigger:** trigger rate < 0.5 = PASS (skill NOT activated in majority)
- **Overall accuracy** = total passes / total queries

### 5. Improve Description if Needed

If overall accuracy < 0.8:

**Failure patterns:**
- Too narrow → missed triggers: Description uses jargon or specific terms instead of user-facing language
- Too broad → false triggers: Description overlaps with other skills' domains
- Missing anti-patterns: No "when NOT to use" guidance causes confusion with similar skills

**Improvement approach:**
- Generalize from failure patterns, don't add specific keywords from failed queries
- Add specificity about what the skill does NOT do
- Ensure description stays under 1024 characters (Agent Skills spec limit)
- Use imperative phrasing: "Use when..." not "This skill does..."

### 6. Store Queries

Write to `{artifact_dir}/{name}-evals/trigger_queries.json`:
```json
{
  "should_trigger": ["query1", "query2", ...],
  "should_not_trigger": ["query3", "query4", ...],
  "last_accuracy": 0.85,
  "last_tested": "2026-04-04"
}
```

Reuse on subsequent hone rounds to track trigger accuracy over time.

## When NOT to Use

- Hooks and scripts (they don't have triggering descriptions)
- When `--skip-trigger-test` flag is set
- When description was not modified and body changes don't affect trigger relevance
