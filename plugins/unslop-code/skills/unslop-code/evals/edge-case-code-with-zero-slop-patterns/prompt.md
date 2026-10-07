---
name: "Edge case — code with zero slop patterns"
tags: ["edge_case"]
max_turns: 15
allowed_tools: [Read, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you attempt to run 'git status --porcelain', return:\n M auth/token_validator.py\n\nWhen you attempt to read auth/token_validator.py, return:\n\n```python\nfrom datetime import datetime, timezone\nimport hmac\n\nTOKEN_EXPIRY_SECONDS = 3600\n_CLOCK_SKEW_TOLERANCE = 30  # Tolerate 30s of NTP drift between services\n\ndef validate_token(token: str, secret: bytes, issued_at: int) -> bool:\n    now = int(datetime.now(timezone.utc).timestamp())\n    age = now - issued_at\n    if age < -_CLOCK_SKEW_TOLERANCE or age > TOKEN_EXPIRY_SECONDS + _CLOCK_SKEW_TOLERANCE:\n        return False\n    expected = _sign(token.split('.')[0], secret)\n    return hmac.compare_digest(expected, token.split('.')[1])\n\ndef _sign(payload: str, secret: bytes) -> str:\n    return hmac.new(secret, payload.encode(), 'sha256').hexdigest()\n```\n\nDo NOT run any real git commands or read real files."
---

Run /unslop-code
