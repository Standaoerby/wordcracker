"""Autonomy machinery — runner / tripwire / kill-switch (AUTONOMY_RUNBOOK_R-30).

Self-protected: ``DENY scripts/autonomy/**`` in
``scripts/scope_fence/denylist.txt`` makes any edit under this package a
scope-fence violation, so agents cannot reach into their own deploy/merge
machinery (runbook §5). The WP-0 bootstrap PRs that build this package are the
single hand-merge exception (Stan reviews + merges).

WP-0 build order (sprint plan):
  - #2  ``deploy_runner.py``  — post-merge deploy + verify + smoke runner with
        auto-rollback (runbook §4). Stdlib-only; shells out to the existing
        ``scripts/deploy.sh`` + ``scripts/verify_deployed_image.sh``. Leaves
        clean seams for smoke-as-code (#3), eval-tripwire (#4), audit-log /
        kill-switch (#5).
  - #4  eval-tripwire + committed baseline (runbook §6).
  - #5  audit-log (``AUTONOMY_LOG.md``) + kill-switch (runbook §7).
"""
