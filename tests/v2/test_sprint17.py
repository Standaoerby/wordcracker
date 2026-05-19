"""Sprint 17 — Round 7 closures + performance tests.

Three closeable Round 7 findings:
  1) Multi-author word_contexts (Q8: «ajar у Остин/Диккенса/Дойла»)
  2) Bare-word extraction after «примеры/examples»
  3) Intent classifier short-circuit (perf, no behavior change)"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class MultiAuthorWordContexts(unittest.TestCase):
    """Round 7 Q8: «ajar у Остин/Диккенса/Дойла» — only Austen
    got processed in v3.0. plan now emits N steps."""

    def test_slash_separated_dispatches_all_three(self):
        e = ent_mod.extract('слова "ajar" у Остин/Диккенса/Дойла')
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 3)
        regexes = [s.args["author_regex"] for s in p.steps]
        self.assertEqual(regexes, ["^Austen,", "^Dickens,", "^Doyle,"])

    def test_comma_and_and_separated(self):
        e = ent_mod.extract('словом fog у Мелвилла, Стивенсона и Конрада')
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 3)
        ids = [s.args["author_regex"] for s in p.steps]
        self.assertIn("^Melville,", ids)
        self.assertIn("^Stevenson,", ids)
        self.assertIn("^Conrad,", ids)

    def test_caps_at_4_total(self):
        """Bound time — even with 6 authors mentioned, no more than 4 steps."""
        e = Entities(word="fog", author_regex="^A,",
                     multi_author_regex=["^B,", "^C,", "^D,", "^E,", "^F,"])
        p = plan_mod.build("word_contexts", e)
        self.assertLessEqual(len(p.steps), 4)

    def test_optional_for_secondaries(self):
        """Failure on one author doesn't abort the whole chain."""
        e = Entities(word="fog", author_regex="^A,",
                     multi_author_regex=["^B,"])
        p = plan_mod.build("word_contexts", e)
        self.assertFalse(p.steps[0].optional)   # primary is required
        self.assertTrue(p.steps[1].optional)    # secondary is best-effort

    def test_single_author_unchanged(self):
        """Single-author phrasing still produces one step (no regression)."""
        e = Entities(word="ajar", author_regex="^Austen,")
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 1)


class BareWordAfterExamples(unittest.TestCase):
    """Sprint 17 — Round 7 Q8 phrasing «примеры ajar у X» where the
    target word isn't quoted and «слова» keyword isn't present."""

    def test_russian_primery_extracts_word(self):
        e = ent_mod.extract("примеры ajar у Доyла")
        self.assertEqual(e.word, "ajar")

    def test_english_examples_of(self):
        e = ent_mod.extract("examples of fog in Stevenson")
        self.assertEqual(e.word, "fog")

    def test_primery_ispolzovaniya(self):
        e = ent_mod.extract("примеры использования sword у Толкина")
        self.assertEqual(e.word, "sword")

    def test_doesnt_grab_russian_genitive_noise(self):
        """«примеры авторов» / «примеры слов» should NOT extract a word —
        Latin-only capture filters Russian common-noun fillers."""
        e = ent_mod.extract("примеры авторов которые писали о море")
        self.assertIsNone(e.word)

    def test_doesnt_grab_word_substring(self):
        """«примеры слова X» — when «слова» trigger is present, the bare
        rule shouldn't conflict with _WORD_AFTER_KEY."""
        e = ent_mod.extract("примеры слова fog у Мелвилла")
        self.assertEqual(e.word, "fog")


class CriticSkipListExtension(unittest.TestCase):
    """Sprint 17 — Phase E/F/G intents are pure table echo. Numeric audit
    now catches the high-value fabrication class. The LLM critic adds
    3-5s of latency for no benefit on these intents."""

    EXPECTED_SKIPS = {
        "learning", "top_authors_books", "vocab_passport",
        "author_lookup", "corpus_extremum", "book_extremum",
        "topic_book_search", "book_pub_year", "book_lookup",
    }

    def test_all_phase_e_f_g_intents_skip(self):
        from scripts.v2.critic import _INTENT_SKIP_CRITIC
        for intent in self.EXPECTED_SKIPS:
            with self.subTest(intent=intent):
                self.assertIn(intent, _INTENT_SKIP_CRITIC)

    def test_skip_returns_trust_with_reason(self):
        """review() must short-circuit cleanly for skipped intents — no
        LLM call, returns a verdict with `(critic skipped for…)` summary."""
        from scripts.v2.critic import review
        v = review("any answer body", [{"tool": "find_book", "data": {}}],
                    intent="book_lookup")
        self.assertTrue(v.verified)
        self.assertIn("critic skipped", v.summary)
        self.assertIn("book_lookup", v.summary)

    def test_non_skipped_intent_still_attempts_critic(self):
        """author_compare should still go through the LLM critic — not in
        the skip list, behavior unchanged."""
        from scripts.v2.critic import _INTENT_SKIP_CRITIC
        self.assertNotIn("author_compare", _INTENT_SKIP_CRITIC)
        self.assertNotIn("author_metadata", _INTENT_SKIP_CRITIC)
        self.assertNotIn("word_contexts", _INTENT_SKIP_CRITIC)


class IntentClassifierCorrectness(unittest.TestCase):
    """After Sprint 17 short-circuit, classifier results must be
    bit-identical to the pre-optimization output. Spot-check the most
    common intent paths to lock the contract before short-circuit
    lands."""

    SAMPLES = [
        ("Что ты умеешь?",                          "introduction"),
        ("сколько книг в базе",                     "corpus_meta"),
        ("когда родился Doyle",                     "author_metadata"),
        ("фирменные слова Wodehouse",               "author_vocab"),
        ("на кого по стилю похож Doyle",            "author_closest"),
        ("сравни Wodehouse и Twain",                "author_compare"),
        ("уровень сложности Pride and Prejudice",   "book_readability"),
        ("найди упоминания fog у Диккенса",         "word_contexts"),
        ("найди книгу про викторианский Лондон",    "topic_book_search"),
        ("когда была опубликована Война и мир",     "book_pub_year"),
        ("какие книги у Doyle",                     "author_lookup"),
        ("самый плодовитый автор",                  "corpus_extremum"),
        ("самая популярная книга",                  "book_extremum"),
        ("топ-10 авторов по числу книг",            "top_authors_books"),
    ]

    def test_canonical_classifications_stable(self):
        for q, expected in self.SAMPLES:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, expected,
                                  msg=f"classify({q!r}) → {m.label!r}, expected {expected!r}")


class ReadabilityCompareClarifyDrop(unittest.TestCase):
    """Stan's 2026-05-19 prod test:
    «что сложнее читать, преступление и наказание или Сон в летнюю ночь?»
    Fell to clarify with 0 calls. Two bugs:
      (1) No intent rule for readability-compare phrasing
      (2) Only first book in KNOWN_BOOKS extracted; «Сон в летнюю ночь»
          was missing AND there was no multi-book collection."""

    EXACT_QUERY = "что сложнее читать, преступление и наказание или Сон в летнюю ночь?"

    def test_intent_classifies_correctly(self):
        m = int_mod.classify(self.EXACT_QUERY)
        self.assertEqual(m.label, "book_readability_compare")

    def test_both_books_extracted(self):
        e = ent_mod.extract(self.EXACT_QUERY)
        # Primary
        self.assertEqual(e.book_id, "PG2554")  # Crime and Punishment
        # Secondary
        self.assertIn("PG1514", e.multi_book_ids)  # Midsummer Night's Dream

    def test_end_to_end_plan(self):
        e = ent_mod.extract(self.EXACT_QUERY)
        p = plan_mod.build("book_readability_compare", e)
        self.assertFalse(p.needs_clarify)
        self.assertEqual(len(p.steps), 2)
        ids = [s.args.get("pg_id") for s in p.steps]
        self.assertIn("PG2554", ids)
        self.assertIn("PG1514", ids)

    def test_english_variants_classify(self):
        for q in [
            "which is harder to read, Dracula or Hamlet?",
            "is Dracula easier to read or Hamlet?",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_readability_compare")

    def test_single_book_falls_through(self):
        """«сложнее читать X» (no «или Y») → single-book readability."""
        e = ent_mod.extract("сложнее читать Преступление и наказание")
        # No second book → fall through to single-book readability plan
        p = plan_mod.build("book_readability_compare", e)
        self.assertEqual(p.intent, "book_readability")
        self.assertEqual(len(p.steps), 1)

    def test_no_book_clarifies(self):
        e = ent_mod.extract("что сложнее читать или попроще")  # no titles
        p = plan_mod.build("book_readability_compare", e)
        self.assertTrue(p.needs_clarify)

    def test_caps_at_3_books(self):
        """If user mentions 4 books, we cap to bound wall-clock."""
        e = ent_mod.extract(
            "что легче читать, Pride and Prejudice или Dracula "
            "или Hamlet или Frankenstein?"
        )
        p = plan_mod.build("book_readability_compare", e)
        # No more than 3 book_readability calls (find_book chains
        # might add a wrapper step but we cap at 3 readability calls)
        readability_steps = [s for s in p.steps if s.tool == "book_readability"]
        self.assertLessEqual(len(readability_steps), 3)


class AskStreamObservability(unittest.TestCase):
    """Sprint 17 — ask_stream() (used by /api/chat/stream and therefore
    every chat-UI query) had NO obs_mod.log_request() calls, so /admin/
    failed was permanently empty for streamed queries. Stan's 2026-05-19
    test caught this. Mirror ask()'s three logging blocks (clarify,
    out_of_scope, success-tail) into ask_stream()."""

    def _consume(self, gen):
        """Exhaust the SSE generator and return the captured events."""
        return list(gen)

    def test_stream_clarify_logs_failure(self):
        import unittest.mock as _mock
        from scripts.v2 import rag_v2
        with _mock.patch.object(rag_v2.obs_mod, "log_request") as mp:
            events = self._consume(rag_v2.ask_stream("asdfqwerty xyz random gibberish"))
        # At least one log_request call
        self.assertGreater(mp.call_count, 0,
                           msg="ask_stream clarify path never logged")
        # The call should mark is_failure=True with failure_kind=clarify
        rec = mp.call_args_list[0][0][0]
        self.assertTrue(rec.get("is_failure"))
        self.assertEqual(rec.get("failure_kind"), "clarify")
        self.assertTrue(rec.get("via_stream"))

    def test_stream_out_of_scope_logs_failure(self):
        import unittest.mock as _mock
        from scripts.v2 import rag_v2
        with _mock.patch.object(rag_v2.obs_mod, "log_request") as mp:
            events = self._consume(rag_v2.ask_stream(
                "напиши мне короткий рассказ в стиле Wodehouse"))
        self.assertGreater(mp.call_count, 0)
        kinds = [c[0][0].get("failure_kind") for c in mp.call_args_list]
        self.assertIn("out_of_scope", kinds)

    def test_stream_marks_via_stream(self):
        """Records from ask_stream must carry via_stream=True so admin
        can distinguish stream vs non-stream sources later if needed."""
        import unittest.mock as _mock
        from scripts.v2 import rag_v2
        with _mock.patch.object(rag_v2.obs_mod, "log_request") as mp:
            self._consume(rag_v2.ask_stream("xyzzy nonsense"))
        for call in mp.call_args_list:
            rec = call[0][0]
            self.assertTrue(rec.get("via_stream"),
                            msg=f"record missing via_stream: {rec}")


class MidsummerKnownBook(unittest.TestCase):
    def test_nominative_resolves(self):
        e = ent_mod.extract("какие архаизмы в Сне в летнюю ночь")  # prep case
        self.assertEqual(e.book_id, "PG1514")

    def test_english_resolves(self):
        e = ent_mod.extract("affinity for A Midsummer Night's Dream")
        self.assertEqual(e.book_id, "PG1514")

    def test_hamlet_resolves(self):
        e = ent_mod.extract("уровень сложности Гамлета")
        self.assertEqual(e.book_id, "PG1524")


if __name__ == "__main__":
    unittest.main(verbosity=2)
