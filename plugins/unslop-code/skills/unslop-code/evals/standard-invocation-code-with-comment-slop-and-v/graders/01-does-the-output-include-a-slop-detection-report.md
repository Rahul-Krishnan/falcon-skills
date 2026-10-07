---
type: llm
weight: 2
---

Does the output include a slop detection report header (AI SLOP DETECTION REPORT or equivalent)?

{'1': 'No report header present. The agent produced generic commentary without following the output format.', '2': 'Report header exists but is missing key sections like Source, Total slop found, or Signal.', '3': 'Report header present with most sections but missing one (e.g. Signal or Verdict).', '4': 'Report header present with all documented sections but formatting slightly differs from spec.', '5': 'Full report header matching the documented format: Source, Total slop found, Signal, Slop Breakdown, Verdict.'}
