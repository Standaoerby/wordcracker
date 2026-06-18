"""WP-1 PR-2 — affinity book-vs-author scope (route-affinity-book-pride).

R2 negative test: FAILS on the pre-fix `_plan_author_vocab`
(`affinity_by_author`), PASSES on the post-fix builder (`affinity_by_book`).

Mock/fixture-only (R-MOCK-FROM-FIXTURE): exercises the deterministic rules
pipeline (`intent → entities → plan`) with NO live model / Ollama / Chroma.

Root cause this pins (offline-reproduced)
==========================================
`route-affinity-book-pride` («фирменные слова в книге Pride and Prejudice»)
classifies as `author_vocab` with a resolved BOOK and no author. On the live
path `rag_v2._needs_entity_help("author_vocab", …)` returns True (it only
checks `author_regex`, ignoring that a book WAS resolved), so the LLM
entity-help backfills the book's own author (Pride and Prejudice → Austen).
The OLD `_plan_author_vocab` guard (`if not e.author_regex and book`) then
lost to that inferred author and widened book→author → `affinity_by_author`.

The frequency twin (`_plan_author_top_words` → `top_ngrams_by_book`) never had
this bug because it checks the book FIRST, unconditionally. The fix gives the
affinity builder the same unconditional book-first check, honouring the S1
invariant «book НИКОГДА молча не → author».

NB: the v4 LLM planner (which reads `@tool(description=…)`) is NOT on this
query's path — the rules plan is non-clarify, so v4 never fires. A
description-only edit would be a no-op here (R8). This is the real lever.
"""
import pytest

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.plan import build


def _tools(intent: str, e: Entities) -> list[str]:
    return [s.tool for s in build(intent, e).steps]


# --- the core regression: a named book must NOT widen to its author ---

def test_named_book_wins_over_inferred_author_in_affinity():
    """Both book AND author present (the post-entity-help shape). The named
    book is the more specific scope and must win → affinity_by_book."""
    e = Entities(author_regex="^Austen,", author_label="Austen",
                 book_id="PG1342", book_title="Pride and Prejudice")
    tools = _tools("author_vocab", e)
    assert "affinity_by_book" in tools, tools
    assert "affinity_by_author" not in tools, tools


def test_named_book_by_title_only_wins_over_inferred_author():
    """Same, but the book is unresolved (title only, no PG id) — still a
    book scope: find_book → affinity_by_book, never affinity_by_author."""
    e = Entities(author_regex="^Austen,", book_title="Pride and Prejudice")
    tools = _tools("author_vocab", e)
    assert "affinity_by_book" in tools, tools
    assert "affinity_by_author" not in tools, tools


def test_book_pos_filter_query_stays_book_scoped():
    """«характерные ПРИЛАГАТЕЛЬНЫЕ в книге Dracula» (+inferred Stoker) — POS
    affinity of a named book stays book-scoped."""
    e = Entities(author_regex="^Stoker,", book_id="PG345",
                 book_title="Dracula", pos_filter=["ADJ"])
    tools = _tools("author_vocab", e)
    assert tools == ["affinity_by_book"], tools


# --- no over-correction: author-only affinity is unchanged ---

def test_author_only_affinity_unchanged():
    """No book named → author signature words still route to the author
    tool. The fix must not steal author-scoped affinity."""
    e = Entities(author_regex="^Doyle,")
    tools = _tools("author_vocab", e)
    assert tools == ["affinity_by_author"], tools


def test_multi_author_fanout_unchanged():
    """«сравни фирменные прилагательные Уайльда и Шоу» — no book, multi
    author fan-out over affinity_by_author is preserved."""
    e = Entities(author_regex="^Wilde,", multi_author_regex=["^Shaw,"],
                 pos_filter=["ADJ"])
    tools = _tools("author_vocab", e)
    assert tools and all(t == "affinity_by_author" for t in tools), tools
    assert "affinity_by_book" not in tools, tools


# --- parity with the frequency twin (the symmetry that was missing) ---

@pytest.mark.parametrize("intent,book_tool", [
    ("author_vocab", "affinity_by_book"),       # affinity twin (this fix)
    ("author_top_words", "top_ngrams_by_book"),  # frequency twin (already ok)
])
def test_named_book_first_parity(intent, book_tool):
    """Both the affinity and frequency author builders must prefer a named
    book over a co-present author — the asymmetry PR-2 closes."""
    e = Entities(author_regex="^Austen,", book_id="PG1342",
                 book_title="Pride and Prejudice")
    assert book_tool in _tools(intent, e)
