#!/usr/bin/env python3
"""Corpus-linguistics *keyness* statistics — the single source of truth.

Replaces the naive "affinity" frequency-ratio with the pair a corpus
linguist trusts and can cite:

  * **log-likelihood G²** (Dunning 1993) — statistical *significance* of the
    frequency difference between a target sub-corpus and a reference.
  * **LogRatio** (Hardie 2014) — the *effect size*, with +0.5 smoothing on a
    zero reference count so unique-to-target words get a finite, bounded
    value instead of being dumped at the top.

Used together (AntConc / WordSmith / #LancsBox / Sketch Engine convention):
rank by G² for significance, read LogRatio as the companion effect size.

This module is imported by every keyness producer so the formula lives in
ONE place (REFACTOR_BRIEF R6 — no copied stats):

  * ``scripts/spgc_author_affinity.py``      — author keyness engine (CSV)
  * ``scripts/learning_tools.affinity_by_book`` — book keyness engine (inline)
  * ``scripts/learning_tools.learning_words``   — keyness flows into rows

Reference geometry (VERIFIED on live data 2026-06-19, see RAG_TASK):
``corpus_counts.csv`` INCLUDES the target's own books, so the reference is
*corpus-minus-target*::

    o1 = target_count                      n1 = target_total
    o2 = corpus_count - target_count       n2 = corpus_total - target_total

References:
  Dunning, T. (1993). Accurate Methods for the Statistics of Surprise and
    Coincidence. Computational Linguistics 19(1).
  Hardie, A. (2014). Log Ratio — an informal introduction. CASS, Lancaster.
  Rayson, P. Log-likelihood and effect size calculator (UCREL).
"""
from __future__ import annotations

import math

# Schema version of the per-target affinity CSV. Bumped whenever the column
# set or the statistics change so stale on-demand caches regenerate (the
# reader compares the CSV header against the new column set — WP-C).
#   v1  legacy: [word, author_count, corpus_count, affinity]   (ratio only)
#   v2  keyness:[word, author_count, corpus_count, rel_freq, g2, log_ratio]
AFFINITY_SCHEMA_VERSION = 2

# G² critical values (χ², 1 degree of freedom). The default is the strict
# p<0.0001 floor recommended for large corpora (Rayson/Hardie) — it keeps
# the list defensible without crowding it with marginally-significant noise.
MIN_LL_P05 = 3.84     # p < 0.05
MIN_LL_P01 = 6.63     # p < 0.01
MIN_LL_P001 = 10.83   # p < 0.001
MIN_LL_P0001 = 15.13  # p < 0.0001
MIN_LL_DEFAULT = MIN_LL_P0001

# Hardie's smoothing constant: substitute 0.5 occurrences for a zero
# reference count so the LogRatio of a unique-to-target word is finite.
_LR_SMOOTH = 0.5


def log_likelihood_g2(o1: float, n1: float, o2: float, n2: float) -> float:
    """Dunning's log-likelihood G² for a 2x2 contingency table.

    ``o1``/``n1`` = target observed count / target total tokens.
    ``o2``/``n2`` = reference observed count / reference total tokens.

    ``G² = 2 · Σ Oi·ln(Oi/Ei)`` over the two cells, skipping any cell whose
    observed count is 0 (the ``x·ln(x)→0`` limit). Non-directional and
    always ≥ 0 — pair with :func:`log_ratio` (or its sign) for direction.
    """
    if n1 <= 0 or n2 <= 0:
        return 0.0
    total_obs = o1 + o2
    total_n = n1 + n2
    if total_obs <= 0 or total_n <= 0:
        return 0.0
    e1 = n1 * total_obs / total_n
    e2 = n2 * total_obs / total_n
    g2 = 0.0
    if o1 > 0 and e1 > 0:
        g2 += o1 * math.log(o1 / e1)
    if o2 > 0 and e2 > 0:
        g2 += o2 * math.log(o2 / e2)
    return 2.0 * g2


def log_ratio(o1: float, n1: float, o2: float, n2: float) -> float:
    """Hardie's LogRatio effect size: ``log2((o1/n1) / (o2/n2))``.

    When the reference count ``o2`` is 0 (word unique to the target),
    substitute ``0.5/n2`` for the reference normalized frequency so the
    result is finite and bounded instead of ``+inf``. Sign carries
    direction: ``> 0`` ⇒ overused in target, ``< 0`` ⇒ underused.
    """
    if n1 <= 0 or n2 <= 0 or o1 <= 0:
        return 0.0
    nf1 = o1 / n1
    nf2 = (o2 / n2) if o2 > 0 else (_LR_SMOOTH / n2)
    if nf2 <= 0:
        return 0.0
    return math.log2(nf1 / nf2)


def rel_freq_ratio(target_count: float, target_total: float,
                   corpus_count: float, corpus_total: float) -> float | None:
    """Legacy 'affinity' — ratio of the target's normalized frequency to the
    *whole-corpus* normalized frequency. Preserved as the ``rel_freq`` column
    (RAG_TASK: keep the old ratio, do not silently drop it). ``None`` when the
    word is absent from the corpus table (``corpus_count == 0``)."""
    if corpus_count <= 0 or target_total <= 0 or corpus_total <= 0:
        return None
    return (target_count / target_total) / (corpus_count / corpus_total)


def keyness(target_count: float, target_total: float,
            corpus_count: float, corpus_total: float) -> dict:
    """Keyness stats for one word against a *corpus-minus-target* reference.

    ``corpus_count`` is the whole-corpus count (it INCLUDES the target's own
    occurrences); ``corpus_total`` is the whole-corpus token total. The
    reference counts are derived here so callers never re-implement the
    subtraction:

        o2 = max(0, corpus_count - target_count)
        n2 = max(0, corpus_total - target_total)

    Returns ``{g2, log_ratio, rel_freq, overused}``. ``overused`` is True iff
    the target's normalized frequency exceeds the reference's (equivalently
    ``log_ratio > 0``) — i.e. the word is a positive key (characteristic of
    the target), the direction a "signature words" view wants.
    """
    o1 = max(0.0, float(target_count))
    n1 = float(target_total)
    o2 = max(0.0, float(corpus_count) - o1)
    n2 = max(0.0, float(corpus_total) - n1)
    g2 = log_likelihood_g2(o1, n1, o2, n2)
    lr = log_ratio(o1, n1, o2, n2)
    nf1 = (o1 / n1) if n1 > 0 else 0.0
    nf2 = (o2 / n2) if n2 > 0 else 0.0
    return {
        "g2": g2,
        "log_ratio": lr,
        "rel_freq": rel_freq_ratio(o1, n1, corpus_count, corpus_total),
        "overused": nf1 > nf2,
    }


def sort_key_for(sort_by: str):
    """Return a ``(row) -> sortable`` key for the requested ranking.

    ``sort_by`` ∈ {``keyness`` (G², default), ``logratio``, ``freq``
    (rel_freq)}. Unknown values fall back to keyness. Rows missing a metric
    sort last (treated as the lowest value)."""
    field = {
        "keyness": "g2",
        "logratio": "log_ratio",
        "freq": "rel_freq",
    }.get((sort_by or "keyness").lower(), "g2")

    def _key(row: dict):
        v = row.get(field)
        return v if isinstance(v, (int, float)) else float("-inf")

    return _key


__all__ = [
    "AFFINITY_SCHEMA_VERSION",
    "MIN_LL_P05", "MIN_LL_P01", "MIN_LL_P001", "MIN_LL_P0001",
    "MIN_LL_DEFAULT",
    "log_likelihood_g2", "log_ratio", "rel_freq_ratio", "keyness",
    "sort_key_for",
]
