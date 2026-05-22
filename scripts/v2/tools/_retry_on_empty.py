"""Shared retry-on-empty helper for filter-stepped tools.

E14 (R-22 follow-up) — `compare_authors` had Q15 step-down chain
(500→200→100→50) when both sides empty. Same UX-class problem exists
in `affinity_by_book`, `affinity_by_author`, `learning_words` — strict
`min_corpus_count` for a narrow scope returns empty, but lowering would
surface real signals.

This helper lifts the pattern. Tools invoke:

    raw = retry_with_lower_threshold(
        v1_fn=_v1,
        v1_args=dict(pg_id=pg_id, top=top, ...),
        threshold_arg="min_corpus_count",
        steps=(100, 50, 20, 10),
        is_empty_fn=lambda raw: not (raw or {}).get("top_words"),
    )

The helper:
  1. Calls v1_fn with current args
  2. If is_empty_fn(raw) — tries each step value in `steps` (must be
     lower than current threshold)
  3. First non-empty wins; raw gets `min_corpus_count_used` +
     `min_corpus_count_requested` + `_threshold_auto_lowered=True`
  4. If all steps empty — returns original empty (no fabrication)

Architectural goal: ONE place where empty-on-strict-filter is handled.
Tool wrappers stay focused on result shaping, not retry logic.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("wordcracker.v2.tools.retry_on_empty")


def retry_with_lower_threshold(
    *,
    v1_fn: Callable[..., dict],
    v1_args: dict,
    threshold_arg: str,
    steps: tuple[int, ...],
    is_empty_fn: Callable[[dict], bool],
    min_initial: int | None = None,
) -> dict:
    """Run v1_fn; if result empty, retry with lower threshold values.

    Args:
      v1_fn          — the v1 callable (e.g. `scripts.learning_tools.
                       affinity_by_book`)
      v1_args        — kwargs for v1_fn. The current threshold value
                       must already be in this dict under `threshold_arg`.
      threshold_arg  — name of the threshold parameter (e.g.
                       «min_corpus_count»)
      steps          — descending sequence of fallback values. Values
                       must be strictly LESS THAN the initial threshold;
                       any larger are skipped.
      is_empty_fn    — predicate on v1's return dict, True if result
                       considered empty
      min_initial    — optional; if set, only retry when initial
                       threshold ≥ this (avoid retry-cascade for
                       already-low thresholds)

    Returns: dict from v1_fn, possibly with annotations:
      `_threshold_auto_lowered` — True if retry succeeded
      `<threshold_arg>_used`    — actual value used (None if initial worked)
      `<threshold_arg>_requested` — initial value (when retry succeeded)
    """
    initial = v1_args.get(threshold_arg)

    # First call with caller's threshold
    raw = v1_fn(**v1_args)

    if not isinstance(raw, dict):
        return raw  # error / malformed — don't retry

    # Honor min_initial gate
    if min_initial is not None and (initial is None or initial < min_initial):
        return raw

    try:
        empty = is_empty_fn(raw)
    except Exception:
        return raw

    if not empty:
        return raw

    # Try step-down values
    for retry_val in steps:
        if initial is not None and retry_val >= initial:
            continue  # not actually lower

        log.info(
            "retry_on_empty: %s — re-trying with %s=%d (was %s)",
            getattr(v1_fn, "__name__", "v1_fn"),
            threshold_arg, retry_val, initial,
        )

        retry_args = dict(v1_args)
        retry_args[threshold_arg] = retry_val

        try:
            retry_raw = v1_fn(**retry_args)
        except Exception as e:
            log.warning("retry_on_empty: v1 raised at %s=%d: %s",
                        threshold_arg, retry_val, e)
            retry_raw = None
            continue

        if not isinstance(retry_raw, dict):
            continue

        try:
            still_empty = is_empty_fn(retry_raw)
        except Exception:
            continue

        if not still_empty:
            # Found non-empty result — annotate + return
            retry_raw[f"{threshold_arg}_used"] = retry_val
            retry_raw[f"{threshold_arg}_requested"] = initial
            retry_raw["_threshold_auto_lowered"] = True
            return retry_raw

    # All step-downs empty — return original empty + annotate
    raw[f"{threshold_arg}_requested"] = initial
    raw["_retry_exhausted"] = True
    return raw
