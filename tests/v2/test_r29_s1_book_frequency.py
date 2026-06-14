"""R-29 S1 / bug A — book-scoped raw-frequency routing + tool.

Pure planner / wrapper unit tests — NO live model, NO ChromaDB, NO SPGC
corpus. The planner is deterministic; the single wrapper test mocks its v1
callee. Mirrors the characterization pins in
`test_audit_s1_s5_characterization.py` (now green) and extends coverage to
the new `top_ngrams_by_book` tool + the bigram bump on the book route.

Fix under test:
  * `_plan_author_top_words` (builders/author.py) routes a named book to
    `top_ngrams_by_book` (book-scoped RAW frequency), never to
    `top_ngrams_by_author` (author aggregate) and never to `affinity_by_book`
    (corpus-relative affinity).
  * `top_ngrams_by_book` v2 wrapper (tools/books/top_ngrams_book.py) emits
    the raw-frequency rows with an honest «частотные слова в [книга]» label.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from scripts.v2.planner.plan import build
from scripts.v2.planner.entities import Entities


def _tools(plan):
    return [s.tool for s in plan.steps]


def _args_blob(plan):
    return json.dumps([s.args for s in plan.steps], ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. A named book (book_id) routes to the book tool, never the author aggregate
#    — even when an author is ALSO present (book is the more specific scope).
# ---------------------------------------------------------------------------
def test_book_frequency_with_book_id_routes_to_book_tool():
    e = Entities(author_regex="^Stoker, Bram", book_id="PG345",
                 book_title="Dracula", top_n=20,
                 raw_misc={"raw_text": "самые частотные слова в Dracula"})
    plan = build("author_top_words", e)
    tools = _tools(plan)
    assert "top_ngrams_by_book" in tools, f"steps={tools}"
    assert "top_ngrams_by_author" not in tools, (
        f"book scope collapsed into an author aggregate: {tools}")
    # honest metric: raw frequency, NOT affinity
    assert "affinity_by_book" not in tools, (
        f"raw-frequency request must not route to affinity: {tools}")
    assert "PG345" in _args_blob(plan)


# ---------------------------------------------------------------------------
# 2. bug A repro — Eugene Onegin (Pushkin, PG23997) + Frankenstein
#    (Shelley, PG84). Book scope, no author aggregate, no foreign author leak.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pg_id,title", [
    ("PG23997", "Eugene Onegin"),
    ("PG84", "Frankenstein"),
])
def test_bug_a_book_scope_no_author_aggregate(pg_id, title):
    e = Entities(book_id=pg_id, book_title=title, top_n=20,
                 raw_misc={"raw_text": f"частотные слова в {title}"})
    plan = build("author_top_words", e)
    tools = _tools(plan)
    assert "top_ngrams_by_author" not in tools, f"steps={tools}"
    assert "top_ngrams_by_book" in tools, f"steps={tools}"
    blob = _args_blob(plan)
    assert pg_id in blob, f"resolved book {pg_id} dropped: {blob}"
    # no author_regex injected anywhere → no author aggregate, no foreign author
    assert "author_regex" not in blob, (
        f"a foreign-author aggregate leaked into the book-scope plan: {blob}")


# ---------------------------------------------------------------------------
# 3. Author-only query (no book named) → the previous author path is intact.
# ---------------------------------------------------------------------------
def test_author_only_top_words_path_unbroken():
    e = Entities(author_regex="^Doyle", top_n=20,
                 raw_misc={"raw_text": "самые частотные слова у Дойла"})
    plan = build("author_top_words", e)
    tools = _tools(plan)
    assert "top_ngrams_by_author" in tools, f"steps={tools}"
    assert "top_ngrams_by_book" not in tools, f"steps={tools}"
    assert "^Doyle" in _args_blob(plan)


# ---------------------------------------------------------------------------
# 4. The bigram/trigram bump survives the book route (shared n-detection).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_n", [
    ("топ биграмм в Дракуле", 2),
    ("частотные триграммы в Dracula", 3),
    ("частотные слова в Dracula", 1),
])
def test_book_route_bumps_ngram_n(text, expected_n):
    e = Entities(book_id="PG345", book_title="Dracula", top_n=15,
                 raw_misc={"raw_text": text})
    plan = build("author_top_words", e)
    step = next(s for s in plan.steps if s.tool == "top_ngrams_by_book")
    assert step.args.get("n") == expected_n, f"args={step.args}"


# ---------------------------------------------------------------------------
# 5. The v2 wrapper turns a v1-shaped dict into a ToolResult carrying the raw
#    counts + an honest book-frequency label (mock v1 — no corpus).
# ---------------------------------------------------------------------------
def test_wrapper_emits_honest_book_frequency():
    from scripts.v2.tools.books.top_ngrams_book import top_ngrams_by_book
    fake_v1 = {
        "pg_id": "PG345", "title": "Dracula", "n": 1, "pos_filter": None,
        "book_tokens": 1000, "total_ngrams": 900,
        "top": [{"ngram": "blood", "count": 42},
                {"ngram": "night", "count": 30}],
    }
    with mock.patch("scripts.rag_tools.top_ngrams_by_book",
                    return_value=fake_v1):
        res = top_ngrams_by_book(pg_id="PG345", n=1, top=20)
    assert res.ok
    assert res.data["top"][0]["ngram"] == "blood"
    assert res.data["top"][0]["count"] == 42
    # Honest-label signal: marks RAW FREQUENCY in the book and explicitly
    # contrasts it with affinity (affinity-label-чек guard).
    note = (res.data.get("_render_note") or "").lower()
    assert "raw frequency" in note
    assert "affinity" in note


def test_wrapper_rejects_empty_pg_id():
    from scripts.v2.tools.books.top_ngrams_book import top_ngrams_by_book
    res = top_ngrams_by_book(pg_id="  ", n=1, top=20)
    assert not res.ok
    assert res.error.type == "invalid_args"
