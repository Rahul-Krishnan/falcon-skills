---
type: llm
weight: 2
---

Traces the Phase 1 execution path step by step, showing which steps run (discover, structural audit, check criteria, generate/audit criteria, enrich, side-effect guard, eval, scoring, spec artifacts, reference validation, report) and identifies which steps would be skipped based on artifact type and complexity tier

{'1': 'Lists fewer than 4 steps or gets the ordering wrong', '2': 'Lists most steps but treats them as a flat list without explaining conditional logic', '3': 'Shows the step sequence and mentions that some steps are conditional (eg skip structural audit for lightweight)', '4': 'Traces the full sequence with correct conditional logic for a standard-tier skill artifact', '5': 'Full trace including gate checklists between steps, handoff data schemas, and script invocation commands'}
