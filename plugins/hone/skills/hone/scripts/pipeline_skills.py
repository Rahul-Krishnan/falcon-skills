#!/usr/bin/env python3
"""Canonical pipeline skill/command name sets.

Single source of truth shared by side_effect_guard.py (which decides what to
sandbox during eval runs) and validate_eval_criteria.py (which flags eval
criteria that expect full pipeline execution). These two lists previously
lived in each script independently and drifted apart; keep membership changes
here so both consumers stay in sync.

These are common names from a plan/implement/publish style pipeline; add your
own here, since the guard can only sandbox a delegated skill whose name it
recognizes.

Stdlib only.
"""

from __future__ import annotations

# Commands that run a multi-stage pipeline. Eval criteria that invoke one of
# these and assert on full pipeline output (landed diff, submitted PR, CI
# status) are flagged by validate_eval_criteria.py.
PIPELINE_COMMANDS = frozenset(
    {
        "forge",
        "smelt",
        "temper-code",
        "temper-plan",
        "smithy",
        "ship",
        "present",
        "quench",
        "quick-fix",
    }
)

# Skills whose invocation must be sandboxed during evals. The guard's
# definition of a side effect is broad (git push down to mkdir), and even
# planning-stage commands can delegate to publishing stages, so every pipeline
# command is included. The sandbox header is inert for a run that never
# reaches a guarded command.
SIDE_EFFECTING_SKILLS = frozenset(PIPELINE_COMMANDS)
