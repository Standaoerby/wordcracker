"""Golden v1 fixtures, recorded once with `record_fixtures.py`.

Each fixture is a JSON file named `<v1_fn>.json` containing one captured
real v1 output. The contract sweep validates these against the declared
schema — when v1 renames a key, the fixture goes stale, the sweep fails,
and we know to update both schema AND fixture together.

Fixtures are NOT checked-in by default (real corpus data) but the
`fixtures_minimal.json` sidecar provides a tiny set of recorded outputs
sufficient for the CI sweep.
"""
