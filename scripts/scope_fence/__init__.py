"""Scope-fence — the cornerstone autonomy gate (AUTONOMY_RUNBOOK_R-30 §5).

The package holds two files that travel together and protect each other:

  - ``check_scope_fence.py`` — the engine + CLI run by the scope-fence CI
    job. Stdlib-only, no project import.
  - ``denylist.txt`` — the single source of truth for the 🔴 (irreversible)
    zone. Self-protected: ``DENY scripts/scope_fence/**`` inside it makes any
    edit to the fence itself a violation, so agents cannot widen their own
    fence (runbook §5).
"""
