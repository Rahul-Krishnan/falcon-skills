#!/usr/bin/env python3
"""Canonical pipeline skill/command name sets.

Single source of truth shared by side_effect_guard.py (which decides what to
sandbox during eval runs) and validate_eval_criteria.py (which flags eval
criteria that expect full pipeline execution). These two lists previously
lived in each script independently and drifted apart; keep membership changes
here so both consumers stay in sync.

These are common names from a plan/implement/publish style pipeline. Extend
the set without editing plugin source (edits are lost on update) via the
HONE_SIDE_EFFECTING_SKILLS environment variable: a comma-separated list of
skill/command names (leading slashes optional). The guard additionally fails
closed: any slash-command delegation it detects that is NOT in this set is
still sandboxed (see side_effect_guard.py), so this list is a refinement, not
the safety boundary.

Stdlib only.
"""

from __future__ import annotations

import os

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

def _extra_skills_from_env() -> frozenset[str]:
    """Names added via HONE_SIDE_EFFECTING_SKILLS (comma-separated)."""
    raw = os.environ.get("HONE_SIDE_EFFECTING_SKILLS", "")
    return frozenset(
        name.strip().lstrip("/").lower()
        for name in raw.split(",")
        if name.strip()
    )


# Skills whose invocation must be sandboxed during evals. The guard's
# definition of a side effect is broad (git push down to mkdir), and even
# planning-stage commands can delegate to publishing stages, so every pipeline
# command is included. The sandbox header is inert for a run that never
# reaches a guarded command.
SIDE_EFFECTING_SKILLS = frozenset(PIPELINE_COMMANDS) | _extra_skills_from_env()
