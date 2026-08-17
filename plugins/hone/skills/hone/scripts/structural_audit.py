#!/usr/bin/env python3
"""Structural audit of skill/command/hook/script markdown files.

15-pillar static analysis. Deterministic regex-based scoring.
No LLM judgment, fully reproducible.

Usage:
    structural_audit.py <artifact_path> --type skill [--json]
    structural_audit.py <artifact_path> --type hook [--json]
    structural_audit.py <artifact_path> --type skill --scripts-dir /path/to/scripts [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Pillar names
PROGRESS_GATES = "progress_gates"
HANDOFF_INTERFACES = "handoff_interfaces"
STATE_PERSISTENCE = "state_persistence"
SCHEMA_VALIDATION = "schema_validation"
ANTI_LAZINESS = "anti_laziness"
RESEARCH_DEPTH = "research_depth"
COMPLEXITY_AWARE = "complexity_aware"
DATA_PROVENANCE = "data_provenance"
SECURITY = "security"
DESCRIPTION_GUARDRAILS = "description_guardrails"
SCRIPT_QUALITY = "script_quality"
COMPACTION_PROTECTION = "compaction_protection"
SPEC_COMPLIANCE = "spec_compliance"
AUTONOMOUS_EXECUTION = "autonomous_execution"
GATE_EVENTS = "gate_events"
STEP_NUMBERING = "step_numbering"

# Types that have multi-step workflow structure
WORKFLOW_TYPES = {"skill", "command"}

# Pillars that surface findings but don't affect the structural score.
# They appear in output as WARN instead of PASS/FAIL.
# ANTI_LAZINESS and AUTONOMOUS_EXECUTION demoted 2026-08-13:
# they scored old-model failure-mode insurance, not present-day defects.
WARNING_ONLY_PILLARS = {
    DESCRIPTION_GUARDRAILS,
    SCRIPT_QUALITY,
    COMPACTION_PROTECTION,
    SPEC_COMPLIANCE,
    GATE_EVENTS,
    STEP_NUMBERING,
    ANTI_LAZINESS,
    AUTONOMOUS_EXECUTION,
}

# Scope-aware pillar priority matrix.
# Each pillar maps to an effective_priority per complexity tier.
# "HIGH" = drives Phase 2 improvements, "LOW" = advisory only, "skip" = excluded.
# Re-derived 2026-08-13: structural insurance (state, gates,
# handoffs) applies only to complex/unattended artifacts; old-model
# failure-mode pillars (anti-laziness, compaction, step numbering) no
# longer drive improvements at any tier.
PILLAR_PRIORITY_MATRIX: dict[str, dict[str, str]] = {
    SECURITY: {"lightweight": "HIGH", "standard": "HIGH", "complex": "HIGH"},
    STATE_PERSISTENCE: {"lightweight": "skip", "standard": "skip", "complex": "HIGH"},
    DATA_PROVENANCE: {"lightweight": "skip", "standard": "LOW", "complex": "HIGH"},
    ANTI_LAZINESS: {"lightweight": "skip", "standard": "skip", "complex": "skip"},
    PROGRESS_GATES: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    HANDOFF_INTERFACES: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    SCHEMA_VALIDATION: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    RESEARCH_DEPTH: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    COMPLEXITY_AWARE: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    DESCRIPTION_GUARDRAILS: {"lightweight": "LOW", "standard": "LOW", "complex": "LOW"},
    SCRIPT_QUALITY: {"lightweight": "skip", "standard": "LOW", "complex": "LOW"},
    COMPACTION_PROTECTION: {"lightweight": "skip", "standard": "skip", "complex": "skip"},
    SPEC_COMPLIANCE: {"lightweight": "LOW", "standard": "LOW", "complex": "LOW"},
    AUTONOMOUS_EXECUTION: {"lightweight": "skip", "standard": "skip", "complex": "LOW"},
    GATE_EVENTS: {"lightweight": "skip", "standard": "skip", "complex": "HIGH"},
    STEP_NUMBERING: {"lightweight": "skip", "standard": "skip", "complex": "skip"},
}

VALID_TIERS = {"lightweight", "standard", "complex"}

# Step marker patterns (broad coverage, case-insensitive so STAGE/Stage/stage all match)
STEP_PATTERNS = [
    re.compile(r"^#{2,4}\s+(?:Step|Phase|Stage)\s+\d+", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^#{2,4}\s+Part\s+[A-Z]", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^#{2,4}\s+\d+\.\s+", re.MULTILINE),
]

# Gate indicators near step transitions
GATE_PATTERNS = [
    re.compile(r"\*\*Gate[:\s]", re.IGNORECASE),
    re.compile(r"gate.*checklist", re.IGNORECASE),
    re.compile(r"- \[ \]"),
    re.compile(r"\bSTOP\b"),
    re.compile(r"exit gate", re.IGNORECASE),
    re.compile(r"\brubric\b", re.IGNORECASE),
    re.compile(r"\binteraction schema\b", re.IGNORECASE),
]

# Handoff interface indicators
HANDOFF_PATTERNS = [
    re.compile(r"\*\*Handoff interface", re.IGNORECASE),
    re.compile(r"handoff.*interface", re.IGNORECASE),
    re.compile(r"output contract", re.IGNORECASE),
]

# State persistence indicators
STATE_PATTERNS = [
    re.compile(r"/tmp/workflow-"),
    re.compile(r"workflow.*state", re.IGNORECASE),
    re.compile(r"state.*file", re.IGNORECASE),
    re.compile(r"state\.json", re.IGNORECASE),
    re.compile(r"Write state to", re.IGNORECASE),
]

# Security threat patterns
CREDENTIAL_PATTERNS = [
    re.compile(r"~/\.ssh/"),
    re.compile(r"~/\.netrc"),
    re.compile(r"~/\.arcrc"),
    re.compile(r"\.env\b"),
    re.compile(r"credentials", re.IGNORECASE),
    re.compile(r"token\b.*file", re.IGNORECASE),
]

EXFIL_PATTERNS = [
    re.compile(r"\bcurl\b.*--data.*@"),
    re.compile(r"\bwget\b.*--post"),
    re.compile(r"\bnc\b.*-e"),
    re.compile(r"(?<![/\w-])\beval\b.*\$\("),
    re.compile(r"(?<![/\w-])\bexec\b.*http"),
]

BASE64_PATTERNS = [
    re.compile(r"base64\s+-d"),
    re.compile(r"base64\s+--decode"),
    re.compile(r"atob\("),
]

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore previous instructions", re.IGNORECASE),
    re.compile(r"you are now a\s", re.IGNORECASE),
    re.compile(r"system:\s*override", re.IGNORECASE),
]

# When an injection phrase appears as a quoted example or inside security
# documentation (a trust-boundary / "treat this as untrusted" section), the
# artifact is teaching the model to DETECT injection, not attempting it. These
# keywords near a match mark that defensive context so it is not flagged.
DEFENSIVE_INJECTION_KEYWORDS = [
    "trust boundary",
    "untrusted",
    "prompt-injection",
    "prompt injection",
    "injection finding",
    "do not obey",
    "do not follow",
    "do not execute",
    "never execute",
    "report it",
    "report them",
    "is itself a",
    "to be reviewed",
    "treat as data",
    "such content",
    "adversarial",
]

# Research phase indicators
RESEARCH_PHASE_PATTERNS = [
    re.compile(
        r"^#{2,4}\s+.*(?:Stage|Phase|Step)\s+\d+.*Research",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(r"^#{2,4}\s+Research\b", re.MULTILINE | re.IGNORECASE),
    re.compile(r"invoke\s+/deep-research", re.IGNORECASE),
]

# Data provenance indicators
SCORE_FIELD_PATTERNS = [
    re.compile(r"\bscore\b.*:\s*(?:number|float|int)", re.IGNORECASE),
    re.compile(r"\bgrade\b.*:\s*(?:string|\"[A-F]\")", re.IGNORECASE),
    re.compile(r"composite_score", re.IGNORECASE),
    re.compile(r"before_score|after_score", re.IGNORECASE),
]

VERIFIED_SOURCE_PATTERNS = [
    re.compile(r"read from.*\.json", re.IGNORECASE),
    re.compile(r"compute.*from.*scores", re.IGNORECASE),
    re.compile(r"extract.*from.*output", re.IGNORECASE),
    re.compile(r"results\.json", re.IGNORECASE),
    re.compile(r"state file", re.IGNORECASE),
]

# Description extraction: YAML frontmatter between --- delimiters
# \A anchors to absolute start of file (not just line start) to avoid matching
# horizontal rules or --- inside code blocks mid-document
FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
DESCRIPTION_FIELD_PATTERN = re.compile(
    r"^description:\s*(.+?)$", re.MULTILINE | re.IGNORECASE
)
# Multi-line description: description: | or description: > followed by indented lines
DESCRIPTION_BLOCK_PATTERN = re.compile(
    r"^description:\s*[|>]\s*\n((?:\s+.+\n?)+)", re.MULTILINE | re.IGNORECASE
)

# Anti-pattern / "when NOT to use" guidance patterns in descriptions
ANTI_PATTERN_INDICATORS = [
    re.compile(r"\bdo\s+NOT\s+use\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+use\b", re.IGNORECASE),
    re.compile(r"\bnever\s+use\b", re.IGNORECASE),
    re.compile(r"\bnot\s+for\b", re.IGNORECASE),
    re.compile(r"\bnot\s+intended\s+for\b", re.IGNORECASE),
    re.compile(r"\bwhen\s+NOT\s+to\b", re.IGNORECASE),
    re.compile(r"\bwhen\s+not\s+to\s+use\b", re.IGNORECASE),
    re.compile(r"\binstead\s+use\b", re.IGNORECASE),
    re.compile(r"\bprefer\s+\S+\s+over\s+this\b", re.IGNORECASE),
    re.compile(r"\bwrong\s+tool\s+for\b", re.IGNORECASE),
    re.compile(r"\bdo\s+NOT\s+trigger\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+trigger\b", re.IGNORECASE),
    re.compile(r"\bdo\s+NOT\s+invoke\b", re.IGNORECASE),
]


@dataclass
class PillarResult:
    name: str
    passed: bool
    applicable: bool
    count_found: int
    count_expected: int
    evidence: list[str] = field(default_factory=list)


def _find_step_transitions(content: str) -> list[str]:
    """Find all step/phase/stage transitions in the content."""
    transitions = []
    for pattern in STEP_PATTERNS:
        for match in pattern.finditer(content):
            transitions.append(match.group().strip())
    return transitions


# Pattern to extract step label from a heading like "### Step 3:", "## Step 11:",
# "### Step 6b:", or "### Step 1.2:". Captures the full label (digits + optional
# decimal or single letter suffix) so non-flat labels can be flagged explicitly.
STEP_NUMBER_RE = re.compile(
    r"^#{2,4}\s+Step\s+(\d+(?:\.\d+)?[a-z]?)", re.MULTILINE | re.IGNORECASE
)


def _validate_step_sequence(content: str) -> list[str]:
    """Validate step headings are sequential integers: no gaps, decimals,
    non-flat labels (e.g. "6b"), or duplicates.

    Returns a list of finding strings (empty = clean).
    """
    findings: list[str] = []
    matches = STEP_NUMBER_RE.findall(content)
    if len(matches) < 2:
        return findings

    # Non-flat label detection (decimals and letter suffixes).
    for step_num in matches:
        if "." in step_num:
            findings.append(f"Non-integer step number: Step {step_num}")
        elif not step_num.isdigit():
            # trailing letter suffix like "6b" or "3a"
            findings.append(f"Non-flat step label: Step {step_num}")

    # Extract flat integers only for sequence / duplicate checks.
    integers: list[int] = []
    for step_num in matches:
        if step_num.isdigit():
            integers.append(int(step_num))

    if not integers:
        return findings

    # Duplicate detection on the flat-integer projection.
    seen: set[int] = set()
    duplicates: list[int] = []
    for n in integers:
        if n in seen and n not in duplicates:
            duplicates.append(n)
        seen.add(n)
    for n in duplicates:
        findings.append(f"Duplicate step number: Step {n}")

    # Sequence gap detection over deduplicated, order-preserving integers.
    unique_in_order: list[int] = []
    seen_for_order: set[int] = set()
    for n in integers:
        if n not in seen_for_order:
            unique_in_order.append(n)
            seen_for_order.add(n)

    for index in range(len(unique_in_order) - 1):
        current_step = unique_in_order[index]
        next_step = unique_in_order[index + 1]
        if next_step != current_step + 1:
            findings.append(
                f"Step sequence gap: Step {current_step} -> Step {next_step}"
            )

    return findings


def audit_step_numbering(content: str, artifact_type: str) -> PillarResult:
    """Pillar: Flat integer step labels with no gaps or duplicates.

    WARNING_ONLY — does not affect structural score denominator, but HIGH
    effective priority for standard/complex tiers drives Phase 2 improvements.
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(STEP_NUMBERING, True, False, 0, 0)

    findings = _validate_step_sequence(content)
    if not findings:
        return PillarResult(STEP_NUMBERING, True, True, 1, 1)

    evidence = [f"SEQUENCE: {f}" for f in findings]
    return PillarResult(STEP_NUMBERING, False, True, 0, 1, evidence)


def _count_pattern_matches(
    content: str, patterns: list[re.Pattern]
) -> tuple[int, list[str]]:
    """Count matches and return evidence strings."""
    count = 0
    evidence = []
    for pattern in patterns:
        for match in pattern.finditer(content):
            count += 1
            text = match.group().strip()[:80]
            evidence.append(text)
    return count, evidence


def audit_progress_gates(content: str, artifact_type: str) -> PillarResult:
    """Pillar 1: Check step transitions have gates."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(PROGRESS_GATES, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    if not transitions:
        return PillarResult(
            PROGRESS_GATES, True, False, 0, 0, ["No step transitions found"]
        )

    expected = max(0, len(transitions) - 1)
    if expected == 0:
        return PillarResult(
            PROGRESS_GATES, True, True, 0, 0, ["Single step, no transitions to gate"]
        )

    gate_count, gate_evidence = _count_pattern_matches(content, GATE_PATTERNS)
    found = min(gate_count, expected)
    passed = found >= expected * 0.5

    evidence = gate_evidence[:5]
    if found < expected:
        evidence.append(
            f"Ungated: {expected - found} of {expected} transitions lack gates"
        )
    # Sequence findings are owned by audit_step_numbering (WARNING_ONLY).
    # Progress_gates no longer flips on sequence issues, keeping concerns
    # orthogonal: gates = "transitions gated?", step_numbering = "labels clean?".

    return PillarResult(PROGRESS_GATES, passed, True, found, expected, evidence)


def audit_handoff_interfaces(content: str, artifact_type: str) -> PillarResult:
    """Pillar 2: Check phase boundaries have typed interfaces."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(HANDOFF_INTERFACES, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    expected = max(0, len(transitions) - 1)
    if expected == 0:
        return PillarResult(HANDOFF_INTERFACES, True, False, 0, 0)

    found, evidence = _count_pattern_matches(content, HANDOFF_PATTERNS)
    found = min(found, expected)
    passed = found >= expected * 0.5
    if found < expected:
        evidence.append(
            f"Missing: {expected - found} of {expected} handoffs lack typed interfaces"
        )
    return PillarResult(HANDOFF_INTERFACES, passed, True, found, expected, evidence)


def audit_state_persistence(content: str, artifact_type: str) -> PillarResult:
    """Pillar 3: Check for workflow state file usage."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(STATE_PERSISTENCE, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    needs_state = len(transitions) >= 2

    if not needs_state:
        return PillarResult(
            STATE_PERSISTENCE,
            True,
            False,
            0,
            0,
            ["Too few steps to require state persistence"],
        )

    found, evidence = _count_pattern_matches(content, STATE_PATTERNS)
    passed = found > 0
    if not passed:
        evidence.append(
            "No workflow state file references found in multi-step artifact"
        )
    return PillarResult(STATE_PERSISTENCE, passed, True, min(found, 1), 1, evidence)


def audit_schema_validation(content: str, artifact_type: str) -> PillarResult:
    """Pillar 4: Check handoff interfaces have validation."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(SCHEMA_VALIDATION, True, False, 0, 0)

    handoff_count, _ = _count_pattern_matches(content, HANDOFF_PATTERNS)
    if handoff_count == 0:
        return PillarResult(
            SCHEMA_VALIDATION, True, False, 0, 0, ["No handoff interfaces to validate"]
        )

    validation_patterns = [
        re.compile(r"validate[sd]?.*(?:shape|schema|field|interface)", re.IGNORECASE),
        re.compile(
            r"(?:verify|check).*(?:exist|non-empty|missing|malformed)", re.IGNORECASE
        ),
        re.compile(r"pre-(?:step|stage|phase) validation", re.IGNORECASE),
        re.compile(r"if.*missing.*STOP", re.IGNORECASE),
    ]
    found, evidence = _count_pattern_matches(content, validation_patterns)
    found = min(found, handoff_count)
    passed = found >= handoff_count * 0.3
    if found < handoff_count:
        evidence.append(
            f"{handoff_count - found} handoff(s) may lack schema validation"
        )
    return PillarResult(SCHEMA_VALIDATION, passed, True, found, handoff_count, evidence)


def audit_anti_laziness(content: str, artifact_type: str) -> PillarResult:
    """Pillar 5: Check for anti-laziness mechanism (self-check or mechanical exit gate)."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(ANTI_LAZINESS, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    if len(transitions) < 2:
        return PillarResult(ANTI_LAZINESS, True, False, 0, 0)

    patterns = [
        re.compile(r"ANTI-LAZINESS SELF-CHECK", re.IGNORECASE),
        re.compile(r"MECHANICAL EXIT GATE", re.IGNORECASE),
    ]
    total_matches = 0
    evidence = []
    for pattern in patterns:
        matches = pattern.findall(content)
        if matches:
            total_matches += len(matches)
            evidence.append(f"Found: {pattern.pattern} ({len(matches)} reference(s))")
    passed = total_matches > 0
    if not passed:
        evidence = [
            "Missing anti-laziness mechanism (ANTI-LAZINESS SELF-CHECK or MECHANICAL EXIT GATE)"
        ]
    return PillarResult(ANTI_LAZINESS, passed, True, min(total_matches, 1), 1, evidence)


def audit_research_depth(content: str, artifact_type: str) -> PillarResult:
    """Pillar 6: Check research phases invoke temper-research."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(RESEARCH_DEPTH, True, False, 0, 0)

    has_research_phase = any(p.search(content) for p in RESEARCH_PHASE_PATTERNS)
    if not has_research_phase:
        return PillarResult(
            RESEARCH_DEPTH, True, False, 0, 0, ["No dedicated research phase found"]
        )

    temper_pattern = re.compile(r"temper-research", re.IGNORECASE)
    matches = temper_pattern.findall(content)
    passed = len(matches) > 0
    evidence = (
        [f"temper-research invoked ({len(matches)} reference(s))"]
        if passed
        else ["Research phase exists but temper-research not invoked"]
    )
    return PillarResult(RESEARCH_DEPTH, passed, True, min(len(matches), 1), 1, evidence)


def audit_complexity_aware(
    content: str, artifact_type: str, artifact_name: str = ""
) -> PillarResult:
    """Pillar 7: Check temper-review has complexity-triggered analysis."""
    is_temper_review = (
        artifact_name == "temper-review" or "temper-review" in artifact_name
    )
    if not is_temper_review:
        return PillarResult(COMPLEXITY_AWARE, True, False, 0, 0)

    big_brain = re.compile(r"big-brain", re.IGNORECASE)
    complexity_trigger = re.compile(
        r"3\+\s*files|schema\s*change|complex", re.IGNORECASE
    )
    has_big_brain = bool(big_brain.search(content))
    has_trigger = bool(complexity_trigger.search(content))
    passed = has_big_brain and has_trigger
    evidence = []
    if has_big_brain:
        evidence.append("big-brain reference found")
    if has_trigger:
        evidence.append("Complexity trigger condition found")
    if not passed:
        evidence.append("Missing complexity-aware deep analysis step")
    return PillarResult(COMPLEXITY_AWARE, passed, True, 1 if has_big_brain else 0, 1, evidence)


def audit_data_provenance(content: str, artifact_type: str) -> PillarResult:
    """Pillar 8: Check score fields come from verified sources."""
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(DATA_PROVENANCE, True, False, 0, 0)

    score_count, _ = _count_pattern_matches(content, SCORE_FIELD_PATTERNS)
    if score_count == 0:
        return PillarResult(
            DATA_PROVENANCE,
            True,
            False,
            0,
            0,
            ["No score/grade fields in handoff interfaces"],
        )

    source_count, evidence = _count_pattern_matches(content, VERIFIED_SOURCE_PATTERNS)
    passed = source_count > 0
    if not passed:
        evidence.append(
            "Score fields found but no verified source references (results.json, state file, etc.)"
        )
    return PillarResult(
        DATA_PROVENANCE, passed, True, min(source_count, 1), 1, evidence
    )


def _is_defensive_injection_context(content: str, start: int, end: int) -> bool:
    """True if a prompt-injection match is a quoted example or sits inside
    defensive security documentation, rather than a live injection attempt.

    Two signals, either is sufficient:
    1. The matched phrase is immediately wrapped in quotes or backticks (a cited
       example, e.g. `"ignore previous instructions"`).
    2. A defensive keyword (trust boundary, untrusted, "do not obey", ...) appears
       in the surrounding window (same sentence/paragraph)."""
    quote_chars = {'"', "'", "`"}
    before = content[start - 1] if start > 0 else ""
    after = content[end] if end < len(content) else ""
    if before in quote_chars and after in quote_chars:
        return True
    window = content[max(0, start - 240): min(len(content), end + 240)].lower()
    return any(kw in window for kw in DEFENSIVE_INJECTION_KEYWORDS)


def audit_security(content: str, artifact_type: str) -> PillarResult:
    """Pillar 9: Scan for security threat patterns."""
    findings = []

    for pattern in CREDENTIAL_PATTERNS:
        for match in pattern.finditer(content):
            findings.append(f"CREDENTIAL: {match.group().strip()[:60]}")

    for pattern in EXFIL_PATTERNS:
        for match in pattern.finditer(content):
            findings.append(f"EXFILTRATION: {match.group().strip()[:60]}")

    for pattern in BASE64_PATTERNS:
        for match in pattern.finditer(content):
            findings.append(f"BASE64: {match.group().strip()[:60]}")

    for pattern in PROMPT_INJECTION_PATTERNS:
        for match in pattern.finditer(content):
            if _is_defensive_injection_context(content, match.start(), match.end()):
                continue
            findings.append(f"PROMPT_INJECTION: {match.group().strip()[:60]}")

    passed = len(findings) == 0
    return PillarResult(
        SECURITY,
        passed,
        True,
        len(findings),
        0,
        findings[:10] if findings else ["No security issues found"],
    )


def _extract_description(content: str) -> str:
    """Extract the description field from YAML frontmatter or first paragraph.

    For skills: extracts the `description:` field from YAML frontmatter.
    For commands: extracts the `description:` field or falls back to first 500 chars.
    """
    # Try YAML frontmatter first
    fm_match = FRONTMATTER_PATTERN.search(content)
    if fm_match:
        frontmatter = fm_match.group(1)
        # Try multi-line description (description: | or description: >)
        block_match = DESCRIPTION_BLOCK_PATTERN.search(frontmatter)
        if block_match:
            return block_match.group(1).strip()
        # Try single-line description
        field_match = DESCRIPTION_FIELD_PATTERN.search(frontmatter)
        if field_match:
            return field_match.group(1).strip()

    # Fallback: first 500 chars (for commands without frontmatter)
    return content[:500]


def audit_description_guardrails(content: str, artifact_type: str) -> PillarResult:
    """Pillar 10: Check description includes anti-pattern guidance.

    Scans the artifact's description (from YAML frontmatter) for negation
    patterns that tell the LLM when NOT to use this artifact. Descriptions
    survive context compaction and are the primary signal for skill routing,
    so anti-pattern guidance here prevents false-positive invocations.

    Applicable to skills and commands only (hooks/scripts don't have
    triggering descriptions).
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(DESCRIPTION_GUARDRAILS, True, False, 0, 0)

    description = _extract_description(content)
    if not description:
        return PillarResult(
            DESCRIPTION_GUARDRAILS,
            False,
            True,
            0,
            1,
            ["No description found in artifact"],
        )

    found_patterns = []
    for pattern in ANTI_PATTERN_INDICATORS:
        for match in pattern.finditer(description):
            found_patterns.append(match.group().strip()[:60])

    passed = len(found_patterns) > 0
    evidence = (
        found_patterns[:5]
        if found_patterns
        else [
            "No anti-pattern guidance found in description. "
            "Add 'when NOT to use' or 'Do NOT use this for' guidance."
        ]
    )
    return PillarResult(
        DESCRIPTION_GUARDRAILS,
        passed,
        True,
        min(len(found_patterns), 1),
        1,
        evidence,
    )


# --- Compaction protection patterns (Pillar 12) ---

# Explicit compaction section or mention
COMPACTION_SECTION_PATTERNS = [
    re.compile(r"compaction\s+protection", re.IGNORECASE),
    re.compile(r"context\s+compaction", re.IGNORECASE),
    re.compile(r"after\s+compaction", re.IGNORECASE),
]

# Compaction-specific re-read instructions (tightened to avoid false positives
# from incidental "re-read" usage like "re-read the user's input")
REREAD_PATTERNS = [
    re.compile(
        r"re-read.*(?:reference|phase|state|file|artifact|from\s+disk)", re.IGNORECASE
    ),
    re.compile(r"read.*from\s+disk", re.IGNORECASE),
    re.compile(r"refresh.*from\s+disk", re.IGNORECASE),
]

# Intermediate result persistence to disk
PERSIST_INTERMEDIATE_PATTERNS = [
    re.compile(r">\s*\$OUTPUT_DIR/"),
    re.compile(r"persist.*to\s+disk", re.IGNORECASE),
    re.compile(r"\(compaction\s+protection\)", re.IGNORECASE),
    re.compile(r"save.*to\s+(?:disk|file)", re.IGNORECASE),
]

# Reference file architecture with re-read anchors (non-greedy .*? to avoid
# catastrophic backtracking on long lines)
REFERENCE_REREAD_ANCHORS = [
    re.compile(r"STOP.*?(?:MUST|must)\s+read\s+\[?references/", re.IGNORECASE),
    re.compile(r"read\s+\[references/", re.IGNORECASE),
    re.compile(r"load.*reference.*file", re.IGNORECASE),
]

# Resume/recovery instructions
RESUME_PATTERNS = [
    re.compile(r"resume\s+from", re.IGNORECASE),
    re.compile(r"first\s+non-done\s+step", re.IGNORECASE),
    re.compile(r"re-read.*state\s+file", re.IGNORECASE),
    re.compile(r"determine\s+current\s+step", re.IGNORECASE),
]


def audit_compaction_protection(content: str, artifact_type: str) -> PillarResult:
    """Pillar 12: Check multi-step artifacts have context compaction protection.

    Long-running skills and commands lose early instructions during context
    compaction. This pillar checks for patterns that enable recovery:
    explicit compaction sections, re-read instructions for reference files,
    intermediate result persistence to disk, and resume/recovery instructions.

    Applicable to multi-step skills and commands only (2+ step transitions).
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(COMPACTION_PROTECTION, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    if len(transitions) < 2:
        return PillarResult(
            COMPACTION_PROTECTION,
            True,
            False,
            0,
            0,
            ["Too few steps to require compaction protection"],
        )

    # Score across 5 categories; need 2+ to pass
    categories_found = 0
    evidence = []

    # Category 1: Explicit compaction section or mentions
    section_count, _ = _count_pattern_matches(content, COMPACTION_SECTION_PATTERNS)
    if section_count > 0:
        categories_found += 1
        evidence.append(f"Compaction section/mention ({section_count} reference(s))")

    # Category 2: Re-read instructions (compaction-specific)
    reread_count, _ = _count_pattern_matches(content, REREAD_PATTERNS)
    if reread_count > 0:
        categories_found += 1
        evidence.append(f"Re-read instructions ({reread_count} reference(s))")

    # Category 3: Intermediate result persistence
    persist_count, _ = _count_pattern_matches(content, PERSIST_INTERMEDIATE_PATTERNS)
    if persist_count > 0:
        categories_found += 1
        evidence.append(f"Intermediate persistence ({persist_count} reference(s))")

    # Category 4: Reference file re-read anchors
    anchor_count, _ = _count_pattern_matches(content, REFERENCE_REREAD_ANCHORS)
    if anchor_count > 0:
        categories_found += 1
        evidence.append(f"Reference re-read anchors ({anchor_count} reference(s))")

    # Category 5: Resume/recovery instructions
    resume_count, _ = _count_pattern_matches(content, RESUME_PATTERNS)
    if resume_count > 0:
        categories_found += 1
        evidence.append(f"Resume/recovery instructions ({resume_count} reference(s))")

    # Pass if 2+ of the 5 categories are present
    passed = categories_found >= 2
    if not passed:
        if categories_found == 0:
            evidence = [
                "No compaction protection found. Multi-step artifact needs: "
                "compaction recovery section, re-read instructions for reference "
                "files after compaction, intermediate result persistence to disk, "
                "and resume/recovery instructions."
            ]
        else:
            evidence.append(
                f"Only {categories_found}/5 compaction protection categories found "
                f"(need 2+). Missing categories weaken recovery after context "
                f"compaction."
            )

    return PillarResult(
        COMPACTION_PROTECTION,
        passed,
        True,
        categories_found,
        5,
        evidence,
    )


# --- Script quality patterns (Pillar 11) ---

# Interactive prompt patterns (should NOT be present in agentic scripts)
INTERACTIVE_PATTERNS = [
    re.compile(r"\binput\s*\("),  # Python input()
    re.compile(r"\bread\s+-p\b"),  # Bash read -p
    re.compile(r"\bselect\s+\w+\s+in\b"),  # Bash select
    re.compile(r"\b\$REPLY\b"),  # Bash $REPLY
]

# Help flag patterns (SHOULD be present)
HELP_FLAG_PATTERNS = [
    re.compile(r"--help"),
    re.compile(r"\bargparse\b"),
    re.compile(r"\bgetopts\b"),
    re.compile(r"[Uu]sage:", re.IGNORECASE),
]

# Structured output patterns (SHOULD be present)
STRUCTURED_OUTPUT_PATTERNS = [
    re.compile(r"json\.dump[s]?\b"),
    re.compile(r"\bjq\b"),
    re.compile(r"csv\.writer"),
    re.compile(r"csv\.DictWriter"),
]

# Exit code patterns (SHOULD be present)
EXIT_CODE_PATTERNS = [
    re.compile(r"sys\.exit\s*\("),
    re.compile(r"\bexit\s+[0-9]"),
]

# Self-contained dependency patterns (SHOULD be present for Python scripts)
SELF_CONTAINED_PATTERNS = [
    re.compile(r"# /// script"),  # PEP 723 inline deps
    re.compile(r"from __future__"),  # Modern Python conventions
]

# __main__ guard: marks a Python file as an invocable script rather than a
# library/constants module.
MAIN_GUARD_PATTERN = re.compile(r"__name__\s*==\s*[\"']__main__[\"']")


def audit_script_quality(scripts_dir: str) -> PillarResult:
    """Pillar 11: Check bundled scripts for agentic design principles."""
    scripts_path = Path(scripts_dir)
    if not scripts_path.exists() or not scripts_path.is_dir():
        return PillarResult(
            SCRIPT_QUALITY, True, False, 0, 0, ["Scripts directory does not exist"]
        )

    # Unit tests are not CLI scripts: grading them against help/usage,
    # structured output, and exit-code checks fills the warnings channel
    # with false positives that invite Phase 2 to add argparse to test files.
    script_files = [
        f
        for f in list(scripts_path.glob("*.py")) + list(scripts_path.glob("*.sh"))
        if not f.name.startswith("test_")
    ]
    if not script_files:
        return PillarResult(
            SCRIPT_QUALITY, True, False, 0, 0, ["No .py or .sh scripts found"]
        )

    total_passed = 0
    total_applicable = 0
    evidence = []

    for script_file in script_files:
        try:
            content = script_file.read_text()
        except OSError:
            evidence.append(f"{script_file.name}: could not read")
            continue

        is_python = script_file.suffix == ".py"

        # Python modules without a __main__ guard are libraries/constants
        # modules, not invocable scripts; the CLI checks don't apply.
        if is_python and not MAIN_GUARD_PATTERN.search(content):
            continue
        file_checks_passed = 0
        file_checks_total = 0
        missing = []

        # Check 1: No interactive prompts (should NOT match)
        file_checks_total += 1
        has_interactive = any(p.search(content) for p in INTERACTIVE_PATTERNS)
        if not has_interactive:
            file_checks_passed += 1
        else:
            missing.append("interactive prompts found")

        # Check 2: Has help/usage
        file_checks_total += 1
        has_help = any(p.search(content) for p in HELP_FLAG_PATTERNS)
        if has_help:
            file_checks_passed += 1
        else:
            missing.append("missing help/usage")

        # Check 3: Has structured output
        file_checks_total += 1
        has_structured = any(p.search(content) for p in STRUCTURED_OUTPUT_PATTERNS)
        if has_structured:
            file_checks_passed += 1
        else:
            missing.append("missing structured output")

        # Check 4: Has explicit exit codes
        file_checks_total += 1
        has_exit = any(p.search(content) for p in EXIT_CODE_PATTERNS)
        if has_exit:
            file_checks_passed += 1
        else:
            missing.append("missing exit codes")

        # Check 5: Self-contained deps (Python only)
        if is_python:
            file_checks_total += 1
            has_self_contained = any(p.search(content) for p in SELF_CONTAINED_PATTERNS)
            if has_self_contained:
                file_checks_passed += 1
            else:
                missing.append("missing self-contained deps")

        total_passed += file_checks_passed
        total_applicable += file_checks_total

        if missing:
            evidence.append(
                f"{script_file.name}: {file_checks_passed}/{file_checks_total} "
                f"checks passed (missing: {', '.join(missing)})"
            )
        else:
            evidence.append(
                f"{script_file.name}: {file_checks_passed}/{file_checks_total} "
                "checks passed"
            )

    if total_applicable == 0:
        return PillarResult(SCRIPT_QUALITY, True, False, 0, 0)

    score = total_passed / total_applicable
    passed = score >= 0.5
    return PillarResult(
        SCRIPT_QUALITY, passed, True, total_passed, total_applicable, evidence
    )


# --- Autonomous execution patterns (Pillar 14) ---

# Autonomous / non-interactive mode indicators
AUTO_MODE_PATTERNS = [
    re.compile(r"--auto\b"),
    re.compile(r"--non-interactive\b"),
    re.compile(r"\bnon-?interactive\s+mode\b", re.IGNORECASE),
    re.compile(r"\bautonomous\s+(?:mode|execution|run)\b", re.IGNORECASE),
    re.compile(r"\bwalk-?away\b", re.IGNORECASE),
    re.compile(r"\bovernight\b", re.IGNORECASE),
    re.compile(r"\bbatch\s+mode\b", re.IGNORECASE),
]

# Explicit --auto bypass: "In --auto mode: ..."
AUTO_BYPASS_PATTERNS = [
    re.compile(r"In\s+`?--auto`?\s+mode\s*:", re.IGNORECASE),
    re.compile(r"--auto\s+mode.*?(?:apply|skip|log|exit|proceed|pick|use)", re.IGNORECASE),
    re.compile(r"auto\s+mode.*?(?:apply all|skip|log|exit|proceed)", re.IGNORECASE),
]

# Mid-flow human-blocking calls (AskUserQuestion outside of STOP/Condition gate sections)
# We look for AskUserQuestion alongside blocking decision language
INTERACTIVE_BLOCK_PATTERNS = [
    re.compile(r"AskUserQuestion"),
    re.compile(r"ask.*?user.*?(?:to\s+confirm|whether|which|what|how|approve)", re.IGNORECASE),
    re.compile(r"(?:confirm|check)\s+with.*?(?:the\s+)?user", re.IGNORECASE),
    re.compile(r"get\s+user\s+(?:input|approval|confirmation)", re.IGNORECASE),
]

# Validation-gate context markers (AskUserQuestion here is correct, not a block)
VALIDATION_CONTEXT_PATTERNS = [
    re.compile(r"\bSTOP\.\s+(?:Do NOT|You MUST)", re.IGNORECASE),
    re.compile(r"Condition\s+[123]\s+[—–-]"),
    re.compile(r"\*\*Condition\s+\d+", re.IGNORECASE),
    re.compile(r"## STOP: Validate"),
]


# Gate event patterns (Pillar 15) — checks that skill writes structured gate events
GATE_EVENT_PATTERNS = [
    re.compile(r'"gates"\s*:\s*\[', re.IGNORECASE),
    re.compile(r'\bgates\[\]\b', re.IGNORECASE),
    re.compile(r'gate[_\-]event', re.IGNORECASE),
    re.compile(r'write.*?gate.*?event', re.IGNORECASE),
    re.compile(r'gate-event-schema', re.IGNORECASE),
    re.compile(r'"step".*?"judge".*?"result"', re.DOTALL),
]


def audit_gate_events(content: str, artifact_type: str) -> PillarResult:
    """Pillar 15: Check multi-step skills/commands instruct writing structured gate events.

    Skills should write gate event JSON to the workflow state file's gates[] array
    at each phase transition. This enables deterministic compliance checking by
    score_gate_compliance instead of keyword counting. See references/gate-event-schema.json.

    Surfaces as WARN (not FAIL) until gate event emission is bootstrapped across all skills.
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(GATE_EVENTS, True, False, 0, 0)

    transitions = _find_step_transitions(content)
    if not transitions:
        return PillarResult(
            GATE_EVENTS, True, False, 0, 0, ["No step transitions found"]
        )

    expected = max(0, len(transitions) - 1)
    if expected == 0:
        return PillarResult(
            GATE_EVENTS, True, True, 0, 0, ["Single step; no gate events required"]
        )

    event_count, evidence = _count_pattern_matches(content, GATE_EVENT_PATTERNS)
    passed = event_count > 0
    if not passed:
        evidence = [
            f"No structured gate event instructions found ({expected} phase transition(s) require gate events). "
            "Add instructions to append to gates[] in the workflow state file at each transition. "
            "See references/gate-event-schema.json."
        ]

    return PillarResult(GATE_EVENTS, passed, True, event_count, expected, evidence)


def audit_autonomous_execution(content: str, artifact_type: str) -> PillarResult:
    """Pillar 14: All skills and commands must document --auto mode.

    Every skill and command must advertise --auto mode — even if autonomous execution
    is inappropriate, the flag must be present and exit early with an explanation.
    This ensures the caller can always pass --auto without being silently blocked.

    When --auto is documented, also verifies that mid-flow interactive blocking calls
    (AskUserQuestion, "confirm with user") have explicit --auto bypass paths, rather
    than silently blocking forever in an unattended run.

    FAIL when: no autonomous mode is documented (required for all skills/commands).
    Hooks and scripts are exempt (they have no argument interface).
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(AUTONOMOUS_EXECUTION, True, False, 0, 0)

    # All skills and commands must document --auto mode
    auto_mode_count, auto_mode_ev = _count_pattern_matches(content, AUTO_MODE_PATTERNS)
    if auto_mode_count == 0:
        return PillarResult(
            AUTONOMOUS_EXECUTION,
            False,
            True,
            0,
            1,
            [
                "No --auto mode documented. All skills and commands must advertise "
                "--auto. If autonomous execution is inappropriate, document "
                "'--auto: exits immediately with explanation' so callers are not "
                "silently blocked."
            ],
        )

    # Artifact claims autonomous mode. Check that blocking calls have bypasses.
    block_count, _ = _count_pattern_matches(content, INTERACTIVE_BLOCK_PATTERNS)
    bypass_count, _ = _count_pattern_matches(content, AUTO_BYPASS_PATTERNS)
    validation_ctx_count, _ = _count_pattern_matches(content, VALIDATION_CONTEXT_PATTERNS)

    if block_count == 0:
        evidence = [
            f"--auto mode documented ({auto_mode_count} reference(s)); "
            "no mid-flow blocking calls found"
        ]
        return PillarResult(AUTONOMOUS_EXECUTION, True, True, 1, 1, evidence)

    if bypass_count > 0:
        evidence = [
            f"--auto mode documented; {block_count} interactive call(s) covered "
            f"by {bypass_count} explicit bypass path(s)"
        ]
        return PillarResult(AUTONOMOUS_EXECUTION, True, True, 1, 1, evidence)

    if validation_ctx_count > 0:
        # Blocking calls present but all appear to be in validation gates
        # (STOP/Condition sections), which is correct — they fire before the workflow
        # starts and are appropriate even in --auto mode.
        evidence = [
            f"--auto mode documented; {block_count} interactive call(s) appear in "
            f"validation gates ({validation_ctx_count} gate marker(s)), not mid-flow"
        ]
        return PillarResult(AUTONOMOUS_EXECUTION, True, True, 1, 1, evidence)

    # Auto mode advertised but blocking calls have no bypass and no gate context
    evidence = [
        f"--auto mode advertised but {block_count} mid-flow blocking call(s) have "
        f"no 'In --auto mode: [action]' bypass. Add bypass paths or move blocks to "
        f"pre-workflow validation gates."
    ]
    return PillarResult(AUTONOMOUS_EXECUTION, False, True, 0, 1, evidence)


# Standard Agent Skills spec frontmatter fields (root-level)
STANDARD_FRONTMATTER_FIELDS = {"name", "description", "version", "author", "license", "metadata"}

# Slash commands accept additional standard root-level frontmatter fields that
# are functional (allowed-tools grants tool permissions); flagging them as
# custom would advise moving them under metadata:, which strips their effect.
COMMAND_FRONTMATTER_FIELDS = STANDARD_FRONTMATTER_FIELDS | {
    "allowed-tools",
    "argument-hint",
    "model",
    "disable-model-invocation",
}


def audit_spec_compliance(content: str, artifact_type: str) -> PillarResult:
    """Pillar 13: Check Agent Skills spec compliance (warning-only).

    Verifies three spec limits that are deterministically measurable:
    1. Description length: 1-1024 chars
    2. Body line count: < 500 lines (skills/commands with heavy content belong in references/)
    3. No root-level custom frontmatter fields (use 'metadata:' for custom fields)

    Applicable to skills and commands only. Surfaces as WARN, not FAIL, so
    existing scores are unaffected.
    """
    if artifact_type not in WORKFLOW_TYPES:
        return PillarResult(SPEC_COMPLIANCE, True, False, 0, 0)

    checks_passed = 0
    checks_total = 3
    findings = []

    # Check 1: Description length 1-1024 chars
    description = _extract_description(content)
    desc_len = len(description)
    if 1 <= desc_len <= 1024:
        checks_passed += 1
    elif desc_len == 0:
        findings.append("Description is empty (spec requires 1-1024 chars)")
    else:
        findings.append(
            f"Description too long: {desc_len} chars (max 1024). Trim or move detail to body."
        )

    # Check 2: Body line count < 500
    body = content
    fm_match = FRONTMATTER_PATTERN.search(content)
    if fm_match:
        body = content[fm_match.end():]
    body_lines = len(body.splitlines())
    if body_lines < 500:
        checks_passed += 1
    else:
        findings.append(
            f"Body too long: {body_lines} lines (spec max 499). "
            "Move heavy content to references/ files."
        )

    # Check 3: No root-level custom frontmatter fields
    allowed_fields = (
        COMMAND_FRONTMATTER_FIELDS
        if artifact_type == "command"
        else STANDARD_FRONTMATTER_FIELDS
    )
    custom_fields = []
    if fm_match:
        frontmatter = fm_match.group(1)
        for line in frontmatter.splitlines():
            field_match = re.match(r"^([a-zA-Z][\w-]*):", line)
            if field_match:
                field_name = field_match.group(1).lower()
                if field_name not in allowed_fields:
                    custom_fields.append(field_match.group(1))
    if not custom_fields:
        checks_passed += 1
    else:
        findings.append(
            f"Non-standard root-level frontmatter fields: {', '.join(custom_fields)}. "
            "Move under 'metadata:'."
        )

    passed = len(findings) == 0
    evidence = findings if findings else [f"All {checks_total} Agent Skills spec checks passed"]
    return PillarResult(SPEC_COMPLIANCE, passed, True, checks_passed, checks_total, evidence)


def audit(
    content: str,
    artifact_type: str,
    artifact_name: str = "",
    complexity_tier: str = "standard",
    scripts_dir: str = "",
) -> dict:
    """Run all pillar audits. Returns JSON-serializable dict.

    When complexity_tier is provided, each pillar's finding gets an
    effective_priority based on PILLAR_PRIORITY_MATRIX. Pillars with
    effective_priority "skip" are marked applicable=False regardless
    of content analysis.
    """
    if complexity_tier not in VALID_TIERS:
        complexity_tier = "standard"

    if not content or not content.strip():
        return {
            "structural_score": 0.0,
            "pillars": [],
            "findings": ["Empty artifact content"],
            "complexity_tier": complexity_tier,
        }

    pillars = [
        audit_progress_gates(content, artifact_type),
        audit_handoff_interfaces(content, artifact_type),
        audit_state_persistence(content, artifact_type),
        audit_schema_validation(content, artifact_type),
        audit_anti_laziness(content, artifact_type),
        audit_research_depth(content, artifact_type),
        audit_complexity_aware(content, artifact_type, artifact_name),
        audit_data_provenance(content, artifact_type),
        audit_security(content, artifact_type),
        audit_description_guardrails(content, artifact_type),
        audit_compaction_protection(content, artifact_type),
        audit_spec_compliance(content, artifact_type),
        audit_autonomous_execution(content, artifact_type),
        audit_gate_events(content, artifact_type),
        audit_step_numbering(content, artifact_type),
    ]

    # Append script_quality if scripts_dir is provided
    if scripts_dir:
        pillars.append(audit_script_quality(scripts_dir))

    # Apply scope-aware priority: override applicability based on tier
    for pillar in pillars:
        tier_priority = PILLAR_PRIORITY_MATRIX.get(pillar.name, {}).get(
            complexity_tier, "LOW"
        )
        if tier_priority == "skip":
            pillar.applicable = False

    # Score only from non-warning pillars
    scoring_pillars = [
        p for p in pillars if p.applicable and p.name not in WARNING_ONLY_PILLARS
    ]
    if not scoring_pillars:
        structural_score = 1.0
    else:
        structural_score = sum(1 for p in scoring_pillars if p.passed) / len(
            scoring_pillars
        )

    security_pillar = next((p for p in pillars if p.name == SECURITY), None)
    if security_pillar and security_pillar.applicable and not security_pillar.passed:
        structural_score = min(structural_score, 0.3)

    findings = []
    warnings = []
    for pillar in pillars:
        if pillar.applicable and not pillar.passed:
            if pillar.name in WARNING_ONLY_PILLARS:
                warnings.extend(pillar.evidence)
            else:
                findings.extend(pillar.evidence)

    # Build pillar dicts with effective_priority
    pillar_dicts = []
    for pillar in pillars:
        pillar_dict = asdict(pillar)
        pillar_dict["effective_priority"] = PILLAR_PRIORITY_MATRIX.get(
            pillar.name, {}
        ).get(complexity_tier, "LOW")
        pillar_dicts.append(pillar_dict)

    # Handoff booleans for the structural_audit interface validated by
    # validate_handoff.py. Deterministically derived from pillar results:
    # "has" = the mechanism was found in the content (count_found > 0),
    # "needed" = the pillar applies to this artifact/tier. Without these,
    # the script's own output could not pass the schema it feeds.
    by_name = {pillar.name: pillar for pillar in pillars}
    handoff_booleans = {}
    for pillar_name, has_key, needed_key in (
        (STATE_PERSISTENCE, "has_state_persistence", "state_persistence_needed"),
        (ANTI_LAZINESS, "has_anti_laziness_check", "anti_laziness_needed"),
        (
            RESEARCH_DEPTH,
            "has_research_depth_enforcement",
            "research_depth_needed",
        ),
        (
            COMPLEXITY_AWARE,
            "has_complexity_aware_analysis",
            "complexity_aware_needed",
        ),
    ):
        pillar = by_name.get(pillar_name)
        handoff_booleans[has_key] = bool(pillar and pillar.count_found > 0)
        handoff_booleans[needed_key] = bool(pillar and pillar.applicable)

    return {
        "structural_score": round(structural_score, 4),
        "pillars": pillar_dicts,
        "findings": findings,
        "warnings": warnings,
        "complexity_tier": complexity_tier,
        **handoff_booleans,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Structural audit of skill/command/hook/script markdown"
    )
    parser.add_argument("artifact_path", help="Path to the artifact file")
    parser.add_argument(
        "--type",
        required=True,
        choices=["skill", "command", "hook", "script"],
        help="Artifact type",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON (default: human-readable)"
    )
    parser.add_argument(
        "--name", default="", help="Artifact name (for pillar 7 applicability)"
    )
    parser.add_argument(
        "--complexity-tier",
        default="standard",
        choices=["lightweight", "standard", "complex"],
        help="Complexity tier for scope-aware pillar priority (default: standard)",
    )
    parser.add_argument(
        "--scripts-dir",
        default="",
        help="Path to scripts directory for script_quality pillar",
    )
    args = parser.parse_args()

    path = Path(args.artifact_path)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(2)

    content = path.read_text()
    artifact_name = args.name or path.stem
    result = audit(
        content, args.type, artifact_name, args.complexity_tier, args.scripts_dir
    )
    result["artifact_path"] = str(path)
    result["artifact_type"] = args.type

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print(f"Structural Score: {result['structural_score']:.2f}")
        print()
        for pillar in result["pillars"]:
            is_warning = pillar["name"] in WARNING_ONLY_PILLARS
            if not pillar["applicable"]:
                status = "N/A"
            elif is_warning and not pillar["passed"]:
                status = "WARN"
            elif pillar["passed"]:
                status = "PASS"
            else:
                status = "FAIL"
            print(
                f"  [{status:4s}] {pillar['name']}: {pillar['count_found']}/{pillar['count_expected']}"
            )
            for ev in pillar["evidence"][:3]:
                print(f"         {ev}")
        if result["findings"]:
            print(f"\nFindings ({len(result['findings'])}):")
            for finding in result["findings"]:
                print(f"  - {finding}")

    usage_log = Path.home() / ".claude" / "scripts" / "usage.log"
    try:
        with open(usage_log, "a") as log_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            log_file.write(f"{timestamp} | structural_audit.py | success\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
