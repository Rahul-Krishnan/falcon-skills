#!/usr/bin/env python3
"""Synthetic fixture: aggregate integer inputs from stdin."""

import json
import sys

values = json.load(sys.stdin)
print(json.dumps({"count": len(values), "total": sum(set(values))}))
