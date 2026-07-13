# falcon-skills

A small marketplace of practical Claude Code skills. Ships three plugins: **hindsight**, **unslop-code**, and **context-contradiction-checker**.

## Quick Start

Add the marketplace, then install the plugin you want:

```
/plugin marketplace add Rahul-Krishnan/falcon-skills
/plugin install hindsight@falcon-skills
/plugin install unslop-code@falcon-skills
/plugin install context-contradiction-checker@falcon-skills
```

Then run them:

```
/hindsight
/unslop-code
/context-contradiction-checker
```

## hindsight

Cross-session retrospective analysis for Claude Code. It reads your recent sessions, finds patterns you keep hitting (scope creep, repeated search cascades, constraint amnesia, stale rules), and proposes concrete fixes to your rules, memory, and workflow.

### What it does

- Scans your recent Claude Code transcripts (`~/.claude/projects/`) over a configurable window (default 14 days).
- Runs three parallel subagents: a transcript scanner, a memory auditor, and a workspace/config scanner.
- Synthesizes findings into a ranked list with severity and evidence, then suggests actions.
- Optionally generates a self-contained HTML report (`--viz`).
- Reports behavioral patterns only. It paraphrases, never quotes your conversations verbatim, and excludes personal, HR, and health topics.

### Usage

```
/hindsight                       # last 14 days, default analysis
/hindsight window=7d             # custom window
/hindsight --viz                 # also save a local HTML report
/hindsight --roast               # blunter tone on findings
/hindsight --human               # prioritize your-side patterns
/hindsight --ai                  # prioritize model-side patterns
/hindsight --auto                # apply safe changes without prompting
```

Flags combine, eg `/hindsight window=30d --roast --viz`.

### When to use it

- You feel like you keep making the same mistakes across sessions and want them named.
- Your CLAUDE.md / rules have drifted, contradict each other, or reference dead projects.
- You want a periodic checkup on your Claude Code workflow.

It is not for single-session questions like "what did I do last session." Those are session-history lookups, not cross-session retrospectives.

### Optional enhancements

- **session-history skill**: if installed, hindsight uses its `cclog.py` for a more robust transcript parser. Without it, hindsight falls back to a built-in parser.
- **A memory MCP server**: if you run one exposing `memory_recall`, the memory auditor will analyze your stored corrections and preferences for staleness and contradictions. Without it, that subagent is skipped.

Neither is required. Hindsight works on live transcripts alone.

### Limitations

- Analysis quality depends on how much session history is on disk. A fresh machine with few sessions gives thin results. Widen the window with `window=30d`.
- The full pipeline runs several minutes and spins up subagents.
- Findings are heuristic. Review proposed changes before applying them (or use `--auto` only when you trust the window).

## unslop-code

Detect and roast AI code slop: the redundant, unreadable, over-complicated patterns that LLMs leave behind and that make a codebase painful to maintain.

### What it does

- Scans your uncommitted changes by default (`git status --porcelain`, which includes brand-new untracked files), or any files you point it at.
- Flags slop across a 13-pattern taxonomy: comments that narrate obvious code, vacuous tests (`assertTrue(True)`), abstraction inflation, phantom parameters, defensive over-engineering, dead code, corporate jargon, and chatbot bleed (assistant chatter that leaked into source).
- Rates each finding CRITICAL / HIGH / MEDIUM / LOW, with the snippet and a proposed fix.
- Lets you choose what to fix: all, by severity, one at a time, or report only.
- **Verifies behavior held.** After applying any fix it runs the narrowest available check (project test runner, else typecheck/lint) and reverts the fix if the check fails. Removing "dead" code that turned out to be load-bearing gets caught here, not in your next deploy.

### Usage

```
/unslop-code                     # scan uncommitted changes
/unslop-code src/parser.py       # scan specific files or directories
/unslop-code --auto              # non-interactive: report only, no fixes, no prompts
/unslop-code --auto-fix          # non-interactive: apply all fixes, each gated by the behavior check
/unslop-code --review            # read-only audit of an already-drafted cleanup (no edits)
```

`--review` audits a cleanup someone already drafted: it reads the diff (uncommitted changes, or the branch against its merge base), checks for over-removal and behavior drift, and returns `APPROVE` or `CHANGES NEEDED`. It never writes. Run it in a fresh session if you want the reviewer to be someone other than the agent that wrote the fixes.

### When to use it

- After an agent (or a person) has generated a lot of code quickly and you want the cruft named.
- Before a PR, to strip the tutorial comments and tautological tests.

It only finds surface-level code-quality issues. It's not a security audit, a performance profile, an architectural review, or a linter.

### Limitations

- Fix mode edits your files. Commit (or stash) before you let it write, so `git` is your undo. The default scan target is exactly the work you haven't committed yet. Use `--auto` for a read-only pass.
- The behavior gate is only as good as your project's tests. In a repo with no test command and no typecheck, it can't verify that a fix preserved behavior, and it says so rather than pretending otherwise.
- Slop detection is heuristic and has taste baked in, so some findings are judgment calls. Read them before you accept them.

## context-contradiction-checker

Your Claude Code context accretes. You add a rule to CLAUDE.md, another to a skill, a third to a hook, and six months later they quietly contradict each other. Claude picks one and you never find out why it ignored the other. This skill finds those collisions.

### What it does

- Scans every active context source at both layers, user and project: `CLAUDE.md`, `.llms/rules/*.md`, other `~/.claude/*.md` files, skills (`~/.claude/skills/` and `./.claude/skills/`), hooks, and `settings.json` / `settings.local.json` (hook triggers, tool permissions, behavioral flags). Cross-layer collisions, where a project rule quietly overrides a user rule, are the ones it most wants to catch.
- Extracts the directives and groups them by topic.
- Classifies each collision:
  - **CONFLICT**: two directives directly oppose each other ("always use tabs" vs "always use spaces").
  - **TENSION**: not contradictory, but confusing together ("be concise" vs "always explain your reasoning in detail").
  - **OVERLAP**: the same rule stated twice, wasting context window.
- Scores each finding 0-100 for confidence and reports only those at 70 or above, so you get real collisions instead of a wall of maybes.
- Offers an interactive fix menu.

### Usage

```
/context-contradiction-checker                          # full scan of all context sources
/context-contradiction-checker ~/.claude/CLAUDE.md      # single file, internal contradictions only
/context-contradiction-checker ./CLAUDE.md ~/.claude/CLAUDE.md   # compare two files
/context-contradiction-checker --auto                   # full scan, report only, no fix menu
```

`--auto` is non-interactive and can't be combined with file path arguments.

### When to use it

- Claude keeps ignoring a rule you're sure you wrote down.
- Your CLAUDE.md has grown by accretion and you've never audited it.
- You've just merged rules from several sources and want to know what fights.

It reads instruction files, not code. It's not a linter or a code reviewer.

### Limitations

- Judgment calls are heuristic. A TENSION is a suggestion to look, not a verdict.
- It reports on the context sources it can find on disk. Rules that live only in a system prompt or an MCP server's instructions are invisible to it.
- Survives context compaction via a state file in `/tmp`, keyed on the session ID, but a very large context set still takes a while to walk.

## License

MIT. See [LICENSE](LICENSE).
