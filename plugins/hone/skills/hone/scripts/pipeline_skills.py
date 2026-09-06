#!/usr/bin/env python3
"""Shared pipeline skill names for sandboxing and criteria validation.

side_effect_guard.py and validate_eval_criteria.py use these sets. Extend
them through HONE_SIDE_EFFECTING_SKILLS (comma-separated, slashes optional)
without editing plugin files that updates replace. The guard also sandboxes
unknown slash-command delegations, so omissions here do not grant access.
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


# Sandbox every pipeline command: planning stages can delegate to publishing.
# The simulation header has no effect unless a guarded command is reached.
SIDE_EFFECTING_SKILLS = frozenset(PIPELINE_COMMANDS) | _extra_skills_from_env()
