"""Per-binding sample-args table for v1↔v2 contract recording + replay.

This is the single source of truth that both
[`record_fixtures.py`](scripts/v2/contracts/record_fixtures.py) (the
prod-side recorder CLI) and
[`tests/v2/test_v1_contracts.py`](tests/v2/test_v1_contracts.py)
(the CI-side replay test) consume.

Each entry maps a `v1_qualname` (the same string passed as
`v1_fn=...` in the wrapper's `@v1_contract(...)` decoration) to a
kwargs dict tuned to hit the four golden PG books named in
[tz_structural_fixes_2026-05-24.md](../../tz_structural_fixes_2026-05-24.md)
S-F2:

  PG1342 — Pride and Prejudice (Austen)
  PG174  — The Picture of Dorian Gray (Wilde)
  PG345  — Dracula (Stoker)
  PG84   — Frankenstein (Shelley)

Author regexes pin distinct surnames so author-scope contracts
exercise real-name matching (not just regex syntax). Word-scope args
sample lexically-distinct words from the same books. Book-scope args
rotate through the four PG ids so coverage spans more than one
(id, raw) pair against real corpus shape.

Adding a new wrapper backed by a v1 function requires (a) declaring
its `V1<Name>` schema in
[schemas.py](scripts/v2/contracts/schemas.py), (b) adding a
`@v1_contract(...)` binding on the wrapper, AND (c) adding an entry
here. Step (c) is enforced by
[`FixtureCoverageGate`](tests/v2/test_v1_contracts.py) — a binding
in `V1_CONTRACTS` without a corresponding LIVE_ARGS entry, or with
no recorded fixture under
[fixtures/](scripts/v2/contracts/fixtures/), fails CI red.
"""
from __future__ import annotations

from typing import Any

# Golden-book regexes — these surnames match the four PGs above.
_AUSTEN = "^Austen,"          # PG1342
_WILDE = "^Wilde, Oscar$"     # PG174
_STOKER = "^Stoker, Bram$"    # PG345
_SHELLEY = "^Shelley, Mary"   # PG84


LIVE_ARGS: dict[str, dict[str, Any]] = {
    # --------------------------------------------------------------
    # rag_tools — word-scope (words sampled from the golden corpus)
    # --------------------------------------------------------------
    "scripts.rag_tools.word_collocates":
        {"scope": {"author": _AUSTEN}, "word": "love", "window": 4},
    "scripts.rag_tools.emotion_collocates":
        {"scope": {"author": _STOKER}, "emotion": "fear"},
    "scripts.rag_tools.word_contexts":
        {"word": "love", "author_regex": _AUSTEN},
    "scripts.rag_tools.word_contexts_global":
        {"word": "blood"},
    "scripts.rag_tools.word_pos_distribution":
        {"scope": {"author": _AUSTEN}, "word": "love"},
    "scripts.rag_tools.word_freq_timeline":
        {"word": "monster"},
    "scripts.rag_tools.words_disappearing_after":
        {"year": 1920, "top": 5},
    "scripts.rag_tools.words_appearing_after":
        {"year": 1920, "top": 5},
    "scripts.rag_tools.word_etymology":
        {"word": "sword"},
    "scripts.rag_tools.find_words_by_etymology":
        {"scope": {"author": _AUSTEN}, "family": "germanic"},

    # --------------------------------------------------------------
    # rag_tools — author-scope (regexes hit the four golden authors)
    # --------------------------------------------------------------
    "scripts.rag_tools.affinity_by_author":
        {"author_regex": _AUSTEN, "top": 5, "min_corpus_count": 500},
    "scripts.rag_tools.compare_authors":
        {"author1_regex": _AUSTEN, "author2_regex": _WILDE},
    "scripts.rag_tools.corpus_stats_by_author":
        {"author_regex": _STOKER},
    "scripts.rag_tools.author_profile":
        {"author_regex": _SHELLEY},
    "scripts.rag_tools.author_influences":
        {"author_regex": _AUSTEN},
    "scripts.rag_tools.author_attribution":
        {"text": (
            "It is a truth universally acknowledged, that a single man in possession of a good "
            "fortune, must be in want of a wife. However little known the feelings or views of such a "
            "man may be on his first entering a neighbourhood, this truth is so well fixed in the minds "
            "of the surrounding families, that he is considered the rightful property of some one or "
            "other of their daughters. My dear Mr. Bennet, said his lady to him one day, have you heard "
            "that Netherfield Park is let at last? Mr. Bennet replied that he had not. But it is, "
            "returned she; for Mrs. Long has just been here, and she told me all about it. Mr. Bennet "
            "made no answer. Do you not want to know who has taken it? cried his wife impatiently. You "
            "want to tell me, and I have no objection to hearing it. This was invitation enough. Why, my "
            "dear, you must know, Mrs. Long says that Netherfield is taken by a young man of large "
            "fortune from the north of England; that he came down on Monday in a chaise and four to see "
            "the place, and was so much delighted with it that he agreed with Mr. Morris immediately."
        )},
    "scripts.rag_tools.author_metadata":
        {"author_regex": _WILDE},
    "scripts.rag_tools.top_ngrams_by_author":
        {"author_regex": _AUSTEN, "n": 2},
    # R-29 S1 / bug A — book-scoped raw-frequency tool. Single-book scan
    # (fast, NOT a HEAVY_BINDING). Fixture must be re-recorded on prod/SOW
    # (`record_fixtures`) — until then FixtureCoverageGate flags it RED by
    # design (bootstrap state; the gate message documents the record step).
    "scripts.rag_tools.top_ngrams_by_book":
        {"pg_id": "PG345", "n": 1},
    "scripts.rag_tools.lexical_diversity":
        {"scope": {"author": _STOKER}},

    # --------------------------------------------------------------
    # rag_tools — book-scope (golden PG ids)
    # --------------------------------------------------------------
    "scripts.rag_tools.book_readability":
        {"pg_id": "PG1342"},
    "scripts.rag_tools.book_emotion_profile":
        {"pg_id": "PG345"},

    # --------------------------------------------------------------
    # rag_tools — global search / top-N (no scope arg needed)
    # --------------------------------------------------------------
    "scripts.rag_tools.semantic_search":
        {"query": "vampire"},
    "scripts.rag_tools.find_book":
        {"title": "Pride and Prejudice"},
    "scripts.rag_tools.top_authors_by":
        {"metric": "books"},
    "scripts.rag_tools.top_authors_by_country":
        {"country": "GB"},
    "scripts.rag_tools.top_books_by_downloads":
        {"top": 5},
    "scripts.rag_tools.top_books_by_recency":
        {"top": 5},

    # --------------------------------------------------------------
    # learning_tools — covers PG84 + PG174 to round out the four
    # --------------------------------------------------------------
    "scripts.learning_tools.learning_words":
        {"scope": {"book": "PG1342"}, "level": "intermediate"},
    "scripts.learning_tools.enrich_word":
        {"word": "pleasure"},
    "scripts.learning_tools.export_word_list":
        {"words": [{"word": "love"}], "format": "anki_csv"},
    "scripts.learning_tools.affinity_by_book":
        {"pg_id": "PG174"},
    "scripts.learning_tools.book_archaic_words":
        {"pg_id": "PG84", "top": 10},
}


# ---------------------------------------------------------------------------
# Fixture-coverage exemptions (single source of truth — honored by BOTH
# record_fixtures.py and tests/v2/test_v1_contracts.py::FixtureCoverageGate).
#
# enrich_word is LLM-generative (qwen3) — non-deterministic output; a frozen
# golden fixture would false-fail RecordedFixtureReplay on every re-record.
# Shape-only contract TODO (validate declared keys without pinning values).
# The wrapper stays registered in V1_CONTRACTS — the static AST gate, the
# decorator binding (__v1_contract__), and the cache fingerprint all still
# apply; only the recorded-fixture requirement is waived. Keeping its
# LIVE_ARGS entry above means `record_fixtures --only <qualname>` can still
# force a one-off recording (e.g. to eyeball the shape) without the default
# sweep depending on a warm Ollama.
# ---------------------------------------------------------------------------
FIXTURE_EXEMPT: frozenset[str] = frozenset({
    "scripts.learning_tools.enrich_word",
})


# ---------------------------------------------------------------------------
# Heavy bindings — full-corpus scans that dominate a record_fixtures sweep
# (S-B7 F2-DEPLOY-RERECORD). Measured wall-clock from a prod recording
# (scripts/v2/contracts/fixtures/_manifest.json `elapsed_s`):
#
#     word_contexts_global      ~356-403 s   (every-book context grep)
#     word_freq_timeline        ~49 s        (per-year frequency rollup)
#     find_words_by_etymology   ~10-39 s     (etymology-family corpus scan)
#
# The deploy-time re-record gate (`record_fixtures --skip-heavy`, invoked
# from scripts/deploy.sh between probe-gate and prune) skips these so the
# gate cannot hang a deploy on a multi-minute corpus scan. The skip is
# NOT a blind spot: code-level edits to these v1 funcs are still caught on
# every PR by `FixtureFreshnessGate` (AST-fingerprint, depth<=1) in
# tests/v2/test_v1_contracts.py. Only the deploy gate's depth>=2 JSON-diff
# coverage is waived for these three — an explicit, logged trade-off
# (record_fixtures prints which bindings it skipped). A full sweep is still
# available on demand: `record_fixtures` with no flag (or `--only <name>`)
# re-records everything, heavy bindings included.
#
# HEAVY_BINDINGS must be a subset of LIVE_ARGS — enforced by
# tests/v2/test_deploy_b7.py::test_heavy_bindings_are_known_live_args.
# ---------------------------------------------------------------------------
HEAVY_BINDINGS: frozenset[str] = frozenset({
    "scripts.rag_tools.word_contexts_global",
    "scripts.rag_tools.word_freq_timeline",
    "scripts.rag_tools.find_words_by_etymology",
})


def fixture_filename(v1_qualname: str) -> str:
    """Canonical fixture filename for a binding.

    Returns `<dot-replaced qualname>.json`. Keeps the qualname round-
    trippable from filename via `replace("_", ".")` (lossy when v1
    function names themselves contain underscores — which they often
    do — so we keep the v1_qualname as the authoritative key in
    metadata and use the filename only for storage).

    Example: `"scripts.rag_tools.affinity_by_author"` →
    `"scripts.rag_tools.affinity_by_author.json"`.
    """
    return f"{v1_qualname}.json"


__all__ = ["LIVE_ARGS", "FIXTURE_EXEMPT", "HEAVY_BINDINGS", "fixture_filename"]
