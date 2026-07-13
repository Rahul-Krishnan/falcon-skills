# Subagent Prompts and Fallback Collection

## Parallelism Fallback (when Task tool unavailable)

If the Task tool is unavailable (eg running inside a nested subagent context), use a **collect-then-analyze** pattern that parallelizes the I/O-bound data collection via Bash backgrounding:

### Step A — Parallel data collection (Bash backgrounding)

Run ALL raw data collection in a single Bash call with background jobs:

```bash
# Collect all raw data in parallel
(
  # Fingerprint data (persisted across machines via ~/.claude sync)
  # Each file is a complete JSON object (not JSONL), so use glob to read individually
  python3 -c "
import json, glob, os, sys
fps = []
for path in sorted(glob.glob(os.path.expanduser('~/.claude/hindsight/fingerprints/*.json'))):
    try:
        with open(path) as f:
            fps.append(json.load(f))
    except: pass
json.dump(fps, sys.stdout)
" > /tmp/hindsight_fingerprints.json 2>/dev/null &

  # Transcript data (current machine only)
  # Enumerate projects, then fetch sessions for each
  for proj in $(~/.claude/skills/session-history/scripts/cclog.py --format=json projects 2>/dev/null | python3 -c "import sys,json; [print(p['path']) for p in json.load(sys.stdin).get('projects',[])]"); do
    ~/.claude/skills/session-history/scripts/cclog.py --format=json --project="$proj" sessions 2>/dev/null
  done > /tmp/hindsight_sessions.json &

  # Memory data
  # (memory_recall must be called via MCP, so just mark as pending)
  touch /tmp/hindsight_memory_pending &

  # Workspace files — discover and read config files from multiple locations
  # Supports Falcon personality dir, generic CC, and project-level setups
  python3 -c "
import os, json
ws = {}
# Falcon personality workspace
for n in ['MEMORY.md','ROUTING.md','PROJECTS.md','SOUL.md','IDENTITY.md','PEOPLE.md']:
    p = os.path.expanduser(f'~/Projects/falcon/personality/{n}')
    if os.path.isfile(p): ws[n] = p
    # Also check smithing dir for PROJECTS.md
    if n == 'PROJECTS.md':
        p2 = os.path.expanduser('~/Projects/falcon/.smithing/PROJECTS.md')
        if os.path.isfile(p2) and n not in ws: ws[n] = p2
# Generic CC config
for p in [os.path.expanduser('~/.claude/CLAUDE.md'), os.path.expanduser('~/.llms/rules/personal.md')]:
    if os.path.isfile(p): ws[os.path.basename(p)] = p
# Project-level
for p in ['CLAUDE.md', '.claude/CLAUDE.md']:
    if os.path.isfile(p): ws['project-CLAUDE.md'] = os.path.abspath(p); break
json.dump(ws, open('/tmp/hindsight_ws_manifest.json','w'))
# Copy each discovered file for scanner access
for name, path in ws.items():
    tag = name.replace('.md','').lower().replace(' ','-')
    try:
        import shutil; shutil.copy2(path, f'/tmp/hindsight_ws_{tag}.md')
    except: pass
" 2>/dev/null &

  wait
) && echo "Collection complete"
```

Then call `memory_recall` (MCP tool, can't be backgrounded) to collect memory data.

### Step B — Single-pass analysis

Instead of 3 separate LLM analysis passes, do ONE combined analysis pass over all collected data. Read the collected files from `/tmp/hindsight_*` and apply all 3 detection lenses (transcript patterns, memory audit, workspace scan) in a single structured output. This eliminates 2 full LLM reasoning passes worth of token overhead.

Structure the single-pass output as:
```json
{
  "transcript_findings": [...],
  "memory_findings": [...],
  "workspace_findings": [...],
  "sessions_analyzed": N
}
```

Then proceed to Phase 1.5 validation as normal.

**Why this is faster:** The bottleneck in sequential fallback is 3 separate LLM analysis passes (~30K tokens each). By collecting data in parallel via Bash and analyzing in one pass, we cut from ~90K tokens to ~40K tokens and eliminate I/O wait time entirely.

## Subagent 1: Transcript Scanner

```
prompt: |
  You are analyzing Claude Code session transcripts to find friction patterns.

  ANALYSIS WINDOW: [insert date range or "last N sessions"]
  PARSER MODE: [robust|fallback]
  TAXONOMY: [paste taxonomy.json categories section]

UNRESOLVED FINDINGS FROM LAST RETRO: [paste the unresolved_findings array from setup_context, or "none"]
Actively check whether each of these patterns recurs in this window. If you find evidence, report it as a normal finding using the SAME taxonomy `category` key, citing only evidence from the current window. This is what lets the main thread bump severity on patterns that keep coming back.

  INSTRUCTIONS:

  1. ENUMERATE SESSIONS (3 data sources, checked in order):

     A. FINGERPRINTS (persisted across machines via ~/.claude sync):
       Glob ~/.claude/hindsight/fingerprints/*.json
       Read each file — it contains pre-extracted session metadata (corrections,
       errors, tools used, files modified, timestamps).
       Filter to fingerprints within the analysis window using first_message/last_message.
       These are the PRIMARY cross-session data source when transcripts are unavailable.

     B. LIVE TRANSCRIPTS (current machine only):
       If parser mode is robust:
         Run: ~/.claude/skills/session-history/scripts/cclog.py --format=json projects
         Then for each relevant project:
         Run: ~/.claude/skills/session-history/scripts/cclog.py --format=json --project=<path> sessions
         Note: --project is a GLOBAL flag and must come BEFORE the subcommand.
       If fallback:
         Glob ~/.claude/projects/*/*.jsonl (exclude subagents/ directories)
         IMPORTANT: Use ONLY targeted glob patterns like ~/.claude/projects/*/*.jsonl.
         NEVER use deep recursive patterns like **/*.jsonl or **/.claude/** which will timeout.
       Filter to sessions within the analysis window.
       Skip sessions that already have a fingerprint (avoid double-counting).

     C. DEDUPLICATE: If a session has both a fingerprint AND a live transcript,
       prefer the live transcript (richer data). Use the fingerprint's session_id
       to match against transcript filenames.

  2. FOR EACH SESSION (skip subagent transcripts in subagents/ directories):

     If source is a FINGERPRINT:
       The fingerprint already contains: correction count, correction samples,
       error count, tools used, files modified, message counts. Use these directly
       for pattern detection. No further parsing needed.
       Note: fingerprints have less detail than full transcripts (no tool call
       sequences, no full correction context), so evidence_quality should be
       capped at MODERATE for fingerprint-only findings.

     If source is a LIVE TRANSCRIPT with robust parser:
       Run: ~/.claude/skills/session-history/scripts/cclog.py --format=json --project=<project_path> digest <session_ref>
       Review the digest for errors, tool failures, and correction patterns.
       For sessions with friction signals, run:
       ~/.claude/skills/session-history/scripts/cclog.py --format=json --project=<project_path> search "no|wrong|stop|I meant|not what I" --session=<session_ref>
       Note: --project and --format are GLOBAL flags (before the subcommand).
       --session is a SUBCOMMAND flag for search (after the subcommand).
       The pattern is a positional arg to search (not --pattern=).

     If source is a LIVE TRANSCRIPT with fallback parser:
       Read the .jsonl file directly. Look for user messages containing correction
       language and assistant messages with is_error tool results.

  3. APPLY DETECTION LENSES to each session:
     a. Repeated friction: same error type across 2+ sessions
     b. Correction drift: user corrects something that was corrected before
     c. Workflow bottlenecks: steps taking too long or requiring retries
     d. Rule effectiveness: rules being violated or overly restrictive
     e. Human patterns: user behaviors creating friction (scope creep, skipping planning)
     f. Stale context: outdated references, wrong assumptions
     g. Automation candidates: same manual action or tool sequence repeated across 3+ sessions (could be a skill or script)

  4. SIGNAL-AWARE SAMPLING for long sessions (500+ lines):
     Always include: start/end, all errors, all user corrections, all tool failures,
     all user messages with "no", "wrong", "stop", "I meant", repeated tool calls.
     Fill remaining budget with uniform sampling.

  5. OUTPUT FORMAT (strict JSON):
     {
       "parser_mode": "robust|fallback",
       "sessions_analyzed": <count>,
       "sessions_from_transcripts": <count>,
       "sessions_from_fingerprints": <count>,
       "sessions_skipped": <count>,
       "findings": [
         {
           "category": "<taxonomy category key>",
           "pattern": "<plain-language description>",
           "severity": "LOW|MEDIUM|HIGH|CRITICAL",
           "evidence_quality": "STRONG|MODERATE|WEAK",
           "evidence": [
             {"session_id": "<id>", "date": "<date>", "description": "<brief paraphrase>"}
           ],
           "impact": "<what this costs>"
         }
       ],
       "uncategorized": [
         {"pattern": "<description>", "evidence": [...], "suggested_category": "<name>"}
       ]
     }

  PRIVACY: See the Privacy Rules section at the end of this skill file. Apply those rules to all findings output.
```

## Subagent 2: Memory Auditor

```
prompt: |
  You are auditing the user's memory system for staleness, contradictions, and gaps.

  INSTRUCTIONS:

  1. CHECK FOR MEMORY DB:
     Try: memory_recall with context="all corrections and preferences" expanded_query="corrections preferences rules behavioral patterns decisions" limit=50
     Note: the full MCP tool name varies by installation (eg mcp__falcon-memory__memory_recall). Search available tools for "memory_recall" if the short name fails.
     If the tool is not available, output: {"available": false, "reason": "memory_recall not available"}

  2. IF AVAILABLE, pull memories in parallel (issue all 3 calls in a single message):
     - memory_recall context="corrections" expanded_query="correction rule violation mistake fix behavioral" limit=30
     - memory_recall context="preferences and decisions" expanded_query="preferences decisions workflow style approach" limit=30
     - memory_recall context="people and projects" expanded_query="people team projects work context collaborators" limit=20

  3. ANALYZE FOR:
     a. Duplicate entries: same correction stored multiple times with different wording
     b. Contradictions: two memories that give conflicting instructions
     c. Staleness: memories referencing things that are likely outdated (old dates, completed projects)
     d. Correction clusters: many corrections about the same topic (indicates a persistent problem)
     e. Gaps: important behavioral rules that should exist but don't
     f. Promotion candidates (3+ threshold): corrections or preferences about the same topic that appear 3+ times. These have graduated from one-off corrections to recurring patterns and should be promoted from memory DB to the relevant workspace file (MEMORY.md, ROUTING.md, SOUL.md, or a skill/command file). Tag each candidate with: the topic, the count, the target file for promotion, and a draft rule.

  4. OUTPUT FORMAT (strict JSON):
     {
       "available": true,
       "total_memories_checked": <count>,
       "findings": [
         {
           "category": "<taxonomy category key>",
           "pattern": "<description>",
           "severity": "LOW|MEDIUM|HIGH",
           "evidence_quality": "STRONG|MODERATE|WEAK",
           "evidence": [
             {"memory_id": "<id>", "content_summary": "<paraphrase>", "issue": "<what's wrong>"}
           ],
           "proposed_action": "<what to do>",
           "impact": "<what this costs>"
         }
       ]
     }
```

## Subagent 3: Workspace Scanner

```
prompt: |
  You are scanning workspace configuration files for rule drift, staleness, and contradictions.

  INSTRUCTIONS:

  1. DISCOVER WORKSPACE FILES:
     First, try reading /tmp/hindsight_ws_manifest.json (created during parallel collection).
     If it exists, it maps filename → path for all discovered config files.
     If it doesn't exist, scan these locations manually (read in parallel, skip missing):
     - ~/Projects/falcon/personality/ (MEMORY.md, ROUTING.md, SOUL.md, IDENTITY.md)
     - ~/Projects/falcon/.smithing/PROJECTS.md
     - ~/.claude/CLAUDE.md
     - ~/.llms/rules/personal.md
     - CLAUDE.md or .claude/CLAUDE.md in current directory
     These cover the Falcon personality dir, generic CC, and project-level setups. If none exist, note it and continue.

  2. ANALYZE FOR:
     a. Rule contradictions: two rules that conflict with each other
     b. Rule staleness: rules referencing completed projects, old dates, or defunct processes
     c. Rule drift: rules that are overly specific to one incident vs general principles
     d. Missing rules: common friction patterns (from general Claude Code usage) that have no rule
     e. Redundancy: same rule stated in multiple files unnecessarily
     f. File organization: rules in the wrong file (eg action-specific rules in a universal rules file instead of a routing/action-specific file, or vice versa)

  3. OUTPUT FORMAT (strict JSON):
     {
       "files_scanned": ["<list of files that existed and were read>"],
       "files_missing": ["<list of files that didn't exist>"],
       "findings": [
         {
           "category": "<taxonomy category key>",
           "pattern": "<description>",
           "severity": "LOW|MEDIUM|HIGH",
           "evidence_quality": "STRONG|MODERATE|WEAK",
           "evidence": [
             {"file": "<path>", "section": "<heading>", "issue": "<what's wrong>"}
           ],
           "proposed_action": "<what to do>",
           "impact": "<what this costs>"
         }
       ]
     }
```