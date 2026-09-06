#!/usr/bin/env python3
"""Known-correct control for the verifier, never executor input."""

import json
import sys

values = json.load(sys.stdin)
print(json.dumps({"count": len(values), "total": sum(values)}))
