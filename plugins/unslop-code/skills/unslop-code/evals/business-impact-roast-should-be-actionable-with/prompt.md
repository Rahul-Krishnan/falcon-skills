---
name: "Business impact — roast should be actionable with specific fixes"
tags: ["business_impact"]
max_turns: 15
allowed_tools: [Read, Skill]
append_system_prompt: "SIMULATION MODE: do not issue real tool calls.\n\nYou are executing the unslop-code skill. Read ~/.claude/skills/unslop-code/SKILL.md.\n\nWhen you attempt to run 'git status --porcelain', return:\n M utils/db_utils.py\n\nWhen you attempt to read utils/db_utils.py, return:\n\n```python\n# Database utility functions\n# This module provides utility functions for database operations\n\n# Step 1: Initialize\n# Step 2: Connect\n# Step 3: Execute\n\ndef connect_to_database(host, port, dbname):\n    # Connect to the database using host and port\n    conn = psycopg2.connect(host=host, port=port, dbname=dbname)\n    # Return the connection object\n    return conn\n\ndef execute_query(conn, query):\n    # Execute the SQL query using the connection\n    cursor = conn.cursor()\n    # Execute the query\n    cursor.execute(query)\n    # Return the cursor\n    return cursor\n```\n\nDo NOT run any real git commands or read real files."
---

Run /unslop-code
