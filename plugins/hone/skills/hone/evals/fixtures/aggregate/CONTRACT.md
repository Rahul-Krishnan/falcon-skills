# Integer aggregate fixture

This is a synthetic local test artifact. `aggregate.py` accepts a JSON array
of integers on stdin and prints a JSON object with exactly two integer fields:
`count`, the number of input values, and `total`, their sum. Every occurrence
counts, including repeated and negative values. Empty input produces zero
for both fields. Valid inputs must exit successfully; invalid input behavior
is outside this fixture's scope.

Improve only `aggregate.py`. Preserve this contract and `unrelated_notes.txt`
byte for byte. The notes represent a concurrent task, and their formatting
belongs to that task. Use only local files and the Python standard library.
