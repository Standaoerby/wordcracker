"""Characterizing xfail pins for the 2026-06-14 systemic pipeline audit.

These tests PIN the structural roots S1, S2, S4, S5 from
`docs/audit/S1-S5_findings.md` (cross-referenced to
AUDIT_2026-06-14_pipeline-systemic). Each asserts the INVARIANT a fix
must establish, so it currently FAILS and is marked
`@pytest.mark.xfail(strict=True)` — the same forcing-function pattern
this repo already uses in `test_cache_ast_fingerprint.py` (test 14):

    The test asserts the *fixed* behaviour. Today it fails → xfail keeps
    CI green. When the fix lands the assertion passes; strict mode turns
    the XPASS into a red, forcing whoever fixed it to delete the xfail
    and (where noted) update the audit doc. Without strict=True the
    forcing function would be silent.

Scope notes (safe-max, 2026-06-14 night run):
  * These are PURE ADDITIONS — no golden fixture / manifest / corpus is
    touched, no re-record is required to run them.
  * All five are deterministic: planner / critic / router / a fixture
    file read. None needs a live model, ChromaDB, or the SPGC corpus.
  * S3 (render «compose-first-then-check») is CONFIRMED in the findings
    doc but has NO deterministic unit seam — the only seam is the inline
    render-payload assembler in `_llm_render`, and exposing it is itself
    part of the S3 fix (render-from-evidence). Its characterization
    ships WITH that track, not before it. See the doc, §S3.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.v2.planner.plan import build
from scripts.v2.planner.entities import Entities
from scripts.v2.planner.router import _inject
from scripts.v2._types import ToolResult
from scripts.v2.critic import filter_claims_with_data_evidence


# ===========================================================================
# S1 — Scope is not first-class (bug A: book-frequency → author-aggregate)
# ===========================================================================
#
# `_plan_author_top_words` (scripts/v2/planner/builders/author.py:162-189)
# ignores `e.book_id`: it routes to `top_ngrams_by_author` on the author
# (collapsing a book into an author aggregate over ~10 books), or — with no
# author — calls `_need_author` (author.py:171-172) and DISCARDS the book.
# Its sibling `_plan_author_vocab` (author.py:200-201) already HAS the
# book-fallback that this builder lacks. Entities carries no `scope` slot
# (entities.py:565-613) — scope is an implicit consequence of which intent
# fired, never a resolved field.


# R-29 S1 FIX LANDED — xfail removed. `_plan_author_top_words` now routes a
# named book to `top_ngrams_by_book` (book-scoped raw frequency), so this
# invariant holds and the test passes as a normal green pin. See
# docs/audit/S1-S5_findings.md §S1 + builders/author.py / builders/book.py.
def test_s1_book_frequency_does_not_collapse_to_author_aggregate():
    # «самые частотные слова из "Dracula"» — a BOOK-scoped frequency query.
    # Stoker / Dracula (PG345) are public domain — no copyright-refusal path.
    e = Entities(author_regex="^Stoker, Bram", book_id="PG345",
                 book_title="Dracula", top_n=20)
    plan = build("author_top_words", e)
    tools = [s.tool for s in plan.steps]
    assert "top_ngrams_by_author" not in tools, (
        f"book-scope collapsed into an author aggregate: steps={tools}. "
        f"A named book must not be silently rendered as an author top-words list."
    )


# R-29 S1 FIX LANDED — xfail removed. A resolved book with no author is now
# honored (routed to top_ngrams_by_book on the book's pg_id) instead of being
# discarded by _need_author. See docs/audit/S1-S5_findings.md §S1.
def test_s1_book_frequency_without_author_keeps_the_book():
    e = Entities(book_id="PG345", book_title="Dracula", top_n=20)
    plan = build("author_top_words", e)
    args_blob = json.dumps([s.args for s in plan.steps], ensure_ascii=False)
    assert "PG345" in args_blob, (
        f"resolved book PG345 was dropped (needs_clarify={plan.needs_clarify}, "
        f"steps={[s.tool for s in plan.steps]}); the planner asks for an "
        f"author instead of scoping to the book."
    )


# ===========================================================================
# S2 — Critic grounds ad-hoc, surface-not-lemma (bug B: «said» false-flag)
# ===========================================================================
#
# filter_claims_with_data_evidence (scripts/v2/critic.py:219-249) grounds a
# claim by lowercased SUBSTRING match against a blob of the turn's tool
# data/query, with NO lemma normalization (critic.py imports no lemmatizer).
# An inflected surface form ("said") whose LEMMA ("say") is present in tool
# data is not recognized as grounded — the deterministic rescue leaves it
# for the LLM critic to false-flag (cry-wolf).


@pytest.mark.xfail(strict=True, reason=(
    "S2 / bug B: filter_claims_with_data_evidence (critic.py:219-249) grounds "
    "by lowercased substring with no lemma normalization, so an inflected "
    "surface form ('said') whose lemma ('say') IS in tool data is left "
    "un-rescued and false-flagged. Fix target: lemma-aware grounding "
    "(surface<->lemma). Remove this xfail when grounding is lemma-normalized "
    "(see docs/audit/S1-S5_findings.md §S2)."
))
def test_s2_inflected_surface_grounded_by_lemma_is_suppressed():
    claim = "слово «said» отсутствует в данных"
    # The tool emitted the lemma 'say' (lemma_profile / affinity rows are
    # lemma-keyed); the answer echoed the surface form 'said'.
    tool_records = [{
        "tool": "lemma_profile",
        "data": {"lemma": "say", "global_count": 99999},
        "query": {"lemma": "say"},
    }]
    kept, suppressed = filter_claims_with_data_evidence([claim], tool_records)
    assert claim in suppressed, (
        "surface 'said' was not matched to grounded lemma 'say' — the false "
        f"hallucination flag survives (kept={kept!r}, suppressed={suppressed!r})."
    )


# ===========================================================================
# S4 — Inter-step contract is string-keyed, no schema (B120 class)
# ===========================================================================
#
# router._inject (scripts/v2/planner/router.py:92-114) parses the "key@rank"
# string and reads the producer row value with a hardcoded row.get("id"),
# returning None on any miss. A producer row that is PRESENT but lacks the
# contracted key is therefore SILENTLY indistinguishable from a genuinely
# empty / short source — both yield None → the step is skipped. Nothing
# reconciles producer-output key <-> consumer-input key.


@pytest.mark.xfail(strict=True, reason=(
    "S4 / B120 class: router._inject (router.py:92-114) reads a hardcoded "
    "row.get('id') and returns None on any miss, so a row that is present but "
    "carries the wrong key is silently identical to an empty source. No "
    "schema reconciles producer-output <-> consumer-input keys. Fix target: a "
    "key-contract mismatch is detectable (distinct from a legitimate "
    "shortfall). Remove this xfail when an inter-step key contract makes the "
    "two cases distinguishable (see docs/audit/S1-S5_findings.md §S4)."
))
def test_s4_wrongkey_injection_distinguishable_from_empty_source():
    empty_src = ToolResult.success(
        tool="top_books_by_downloads", data={"top": []})
    # A row IS present but carries 'pg_id' (the B120 phantom key) instead of
    # the contracted 'id' — a producer<->consumer key mismatch.
    wrongkey_src = ToolResult.success(
        tool="top_books_by_downloads",
        data={"top": [{"pg_id": 345, "title": "Dracula"}]})
    out_empty = _inject({}, [empty_src], [0], "pg_id@0")
    out_wrongkey = _inject({}, [wrongkey_src], [0], "pg_id@0")
    assert out_wrongkey != out_empty, (
        "B120 class: a producer row missing the contracted key ('id') is "
        "silently indistinguishable from an empty source — _inject returns "
        f"None for both (out_empty={out_empty!r}, out_wrongkey={out_wrongkey!r}). "
        "No schema check names the producer<->consumer key mismatch."
    )


# ===========================================================================
# S5 — Drift = noise: nondeterministic timestamp baked into a golden fixture
# ===========================================================================
#
# The committed export_word_list fixture embeds a wall-clock epoch in
# out_path, sourced from scripts/learning_tools.py:881 int(time.time()). The
# recorder's _VOLATILE_BODY_KEYS (scripts/v2/contracts/record_fixtures.py:85)
# strips only "_elapsed_s", not this path — so every re-record churns the
# value, producing perpetual advisory drift (noise, not a contract change).

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "v2" / "contracts" / "fixtures"
    / "scripts.learning_tools.export_word_list.json"
)


@pytest.mark.xfail(strict=True, reason=(
    "S5 / nondeterministic fixture: export_word_list.json embeds a wall-clock "
    "epoch in out_path (from learning_tools.py:881 int(time.time())); the "
    "recorder strips only _elapsed_s (record_fixtures.py:85), so every "
    "re-record churns the value -> perpetual advisory drift. Fix target: "
    "normalize the timestamp at record time + re-record. NOTE: the green path "
    "needs a recorder change AND a fixture re-record — both OUT OF SCOPE for a "
    "safe-max session (must not touch fixtures/restamp/corpus). Remove this "
    "xfail once the recorder normalizes the timestamp and the fixture is "
    "re-recorded clean (see docs/audit/S1-S5_findings.md §S5)."
))
def test_s5_export_word_list_fixture_has_no_nondeterministic_timestamp():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    out_path = str(data.get("out_path", ""))
    assert not re.search(r"export_\d{6,}\.", out_path), (
        f"fixture embeds a nondeterministic epoch in out_path={out_path!r} — "
        "drift-noise that churns the git diff on every re-record."
    )
