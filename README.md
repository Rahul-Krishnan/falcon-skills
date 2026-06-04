# falcon-skills

A small marketplace of practical Claude Code skills. Currently ships one plugin: **hindsight**.

## Quick Start

Add the marketplace, then install the plugin:

```
/plugin marketplace add Rahul-Krishnan/falcon-skills
/plugin install hindsight@falcon-skills
```

Then run it:

```
/hindsight
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

## License

MIT. See [LICENSE](LICENSE).
