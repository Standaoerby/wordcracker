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


class BookSimilarFollowupTrap(unittest.TestCase):
    """Sprint 17 — Stan 2026-05-19 caught a UX trap: the renderer
    suggests «хочу по жанру похожему на X» as a follow-up, but
    classify('хочу по жанру похожему на X') fell to clarify.
    System suggesting a phrasing it can't itself classify is a hard
    fail — fix the gap, lock it in."""

    # Disambiguation policy: book_similar fires only when the phrasing
    # carries an explicit book signal (quoted title, «книг/роман/
    # произведен» noun, «продолжение» trigger, or English «books/novels
    # similar to»). «в стиле X» / «подобное на X» without quotes are
    # ambiguous (could be author or book) and intentionally fall to LLM
    # fallback so the entity-aware reclassifier can resolve.
    PHRASINGS = [
        "хочу по жанру или стилю, похожему на «Преступление и наказание»?",
        "хочу по жанру или стилю, похожие на «Преступление и наказание»",
        "книги похожие на Преступление и наказание",
        "продолжение Преступления и наказания",
        "recommend books similar to Crime and Punishment",
        "find a novel like Dracula",
    ]

    AMBIGUOUS_BUT_NOT_AUTHOR_CLOSEST = [
        # Should NOT classify as book_similar (would steal from author
        # intents) but also NOT crash. Either clarify or another intent
        # — anything except book_similar — is acceptable; LLM fallback
        # will handle disambiguation in prod via classify_and_extract.
        "в стиле Pride and Prejudice",
        "что-то подобное на Дракулу",
    ]

    def test_all_phrasings_classify_to_book_similar(self):
        for q in self.PHRASINGS:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_similar",
                                 msg=f"{q!r} → {m.label!r}, expected book_similar")

    def test_author_closest_not_stolen(self):
        """The author-similarity query «кто похож на Doyle» must stay
        in author_closest — book_similar rules must require explicit
        book context."""
        m = int_mod.classify("кто похож на Doyle")
        self.assertEqual(m.label, "author_closest")

    def test_plan_dispatches_find_book_by_topic(self):
        e = ent_mod.extract("книги похожие на Преступление и наказание")
        p = plan_mod.build("book_similar", e)
        self.assertEqual(p.intent, "book_similar")
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")
        # Topic must include enough signal to find similar books
        topic = p.steps[0].args["topic"]
        self.assertTrue(topic and len(topic) > 5,
                        msg=f"topic too short: {topic!r}")

    def test_no_book_clarifies(self):
        """Without a reference book, book_similar has nothing to compare
        — must clarify."""
        e = ent_mod.extract("книги похожие на что-то")
        p = plan_mod.build("book_similar", e)
        self.assertTrue(p.needs_clarify)

    def test_critic_skipped_for_similar(self):
        """book_similar produces a book-list (same shape as
        topic_book_search) → critic skipped to save 3-5s."""
        from scripts.v2.critic import _INTENT_SKIP_CRITIC
        self.assertIn("book_similar", _INTENT_SKIP_CRITIC)


class ChtoPochitatPosle(unittest.TestCase):
    """Stan 2026-05-19 prod: «что почитать после преступления и наказания»
    classified as book_recommendation, but the plan returned
    top_books_by_downloads (Hemingway / Carroll / Christie — generic
    top, NOT related to the reference book). Two fixes:
      1. Route «что почитать после/подобное/похожее/типа X» to
         book_similar — semantically a similarity-to-reference query.
      2. Add full RU declension for the 5 most-asked Russian-titled
         books (acc/dat/inst beyond just nom/gen/prep) so the entity
         extractor picks up forms like «Преступлению и наказанию» (dat),
         «Войну и мир» (acc), «Анны Карениной» (gen)."""

    def test_stan_verbatim_routes_to_book_similar(self):
        m = int_mod.classify("что почитать после преступления и наказания")
        self.assertEqual(m.label, "book_similar")

    def test_stan_verbatim_resolves_book(self):
        e = ent_mod.extract("что почитать после преступления и наказания")
        self.assertEqual(e.book_id, "PG2554")

    def test_stan_verbatim_plan(self):
        e = ent_mod.extract("что почитать после преступления и наказания")
        p = plan_mod.build("book_similar", e)
        self.assertEqual(p.steps[0].tool, "find_book_by_topic")
        # Sprint 20+ B8: topic enriched with thematic framing — improves
        # semantic search precision for «similar to» queries over bare
        # title (which gave noisy word-cooccurrence results).
        topic = p.steps[0].args["topic"]
        self.assertIn("Crime and Punishment", topic)
        self.assertIn("similar", topic.lower())

    def test_declension_dative(self):
        """«подобное Преступлению и наказанию» — dative case."""
        e = ent_mod.extract("что почитать подобное Преступлению и наказанию")
        self.assertEqual(e.book_id, "PG2554")

    def test_declension_accusative_dracula(self):
        e = ent_mod.extract("что почитать похожее на Дракулу")
        self.assertEqual(e.book_id, "PG345")

    def test_declension_war_and_peace_genitive(self):
        e = ent_mod.extract("что почитать после Войны и мира")
        self.assertEqual(e.book_id, "PG2600")

    def test_declension_anna_karenina_genitive(self):
        e = ent_mod.extract("что почитать типа Анны Карениной")
        self.assertEqual(e.book_id, "PG1399")

    def test_level_recommendation_not_stolen(self):
        """«что почитать на уровне B2» must STAY book_recommendation —
        level queries are not similarity queries."""
        m = int_mod.classify("что почитать на уровне B2")
        self.assertEqual(m.label, "book_recommendation")


class TokenObservability(unittest.TestCase):
    """Sprint 17 — Ollama-side prompt_eval_count + eval_count flow into
    obs_mod.log_request so the admin can answer «do we need more
    num_ctx» from data, not guesses."""

    def test_critic_verdict_carries_token_fields(self):
        """CriticVerdict gained prompt_tokens / eval_tokens slots."""
        from scripts.v2.critic import CriticVerdict
        v = CriticVerdict.trust()
        self.assertTrue(hasattr(v, "prompt_tokens"))
        self.assertTrue(hasattr(v, "eval_tokens"))
        # Trust path leaves them None — they're set only when the
        # critic actually called the LLM.
        self.assertIsNone(v.prompt_tokens)

    def test_critic_review_captures_tokens_from_response(self):
        """When the critic LLM responds, prompt_eval_count and
        eval_count are surfaced on the verdict."""
        import unittest.mock as _mock
        from scripts.v2 import critic
        fake_resp = _mock.MagicMock()
        fake_resp.json.return_value = {
            "message": {"content": '{"verified": true, "unsupported_claims": [], '
                                    '"missing_caveats": [], "summary": "ok"}'},
            "prompt_eval_count": 1234,
            "eval_count": 56,
        }
        fake_resp.raise_for_status.return_value = None
        with _mock.patch.object(critic, "CRITIC_ENABLED", True), \
             _mock.patch("scripts.v2.critic.requests.post",
                          return_value=fake_resp):
            v = critic.review("answer", [{"tool": "x", "ok": True,
                                           "data": {}, "coverage": None,
                                           "warnings": []}],
                              intent="author_metadata")
        self.assertEqual(v.prompt_tokens, 1234)
        self.assertEqual(v.eval_tokens, 56)
        self.assertTrue(v.verified)

    def test_llm_render_returns_tuple_with_meta(self):
        """_llm_render now returns (text, meta) — meta carries
        prompt_tokens / eval_tokens / durations."""
        import unittest.mock as _mock
        from scripts.v2 import rag_v2
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities
        import json as _json
        _body = {
            "message": {"content": "the answer"},
            "prompt_eval_count": 4096,
            "eval_count": 128,
            "total_duration": 3_500_000_000,
            "load_duration": 100_000_000,
        }
        fake_resp = _mock.MagicMock()
        fake_resp.json.return_value = _body
        fake_resp.iter_lines.return_value = [_json.dumps(_body).encode()]
        fake_resp.close.return_value = None
        fake_resp.raise_for_status.return_value = None
        plan = QueryPlan(intent="author_metadata", entities=Entities(),
                          steps=[], explain="test")
        with _mock.patch("scripts.v2.rag_v2.requests.post",
                          return_value=fake_resp):
            text, meta = rag_v2._llm_render(
                "test question", plan, [],
                model="x", ollama_host="http://localhost:0",
            )
        self.assertEqual(text, "the answer")
        self.assertEqual(meta["prompt_tokens"], 4096)
        self.assertEqual(meta["eval_tokens"], 128)
        self.assertEqual(meta["total_duration_ns"], 3_500_000_000)


class Round8SilentContextFallback(unittest.TestCase):
    """Round 8 C3-2 — user-deceptive bug: «теперь у Марло» (Marlowe
    not in aliases at the time) silently restored Shakespeare from
    prior turn and returned Shakespeare's vocabulary as if it were
    Marlowe's. Fix: detect explicit-author-name-after-swap pattern,
    block the backfill, clarify with «I don't know that author»."""

    def test_unknown_author_after_swap_blocks_backfill(self):
        """Simulate the exact Round 8 sequence with a deliberately
        unknown author. Even with Marlowe now in aliases, we need a
        future-proof unknown-author scenario."""
        from scripts.v2.planner import history as hist_mod
        from scripts.v2.planner.entities import Entities, extract
        prior = [
            {"role": "user", "content": "фирменные слова Шекспира"},
            {"role": "assistant", "content": "Shakespeare: doth, thee, thou"},
        ]
        # Use a deliberately invented author name so the fix is verified
        # independently of any alias additions.
        current = extract("теперь у Нонэксистентов")
        merged = hist_mod.merge_with_history(current, prior,
                                              "теперь у Нонэксистентов")
        # The Marlowe→Shakespeare bug would set author_regex='^Shakespeare,'
        # here. Fix means it stays None AND raw_misc carries the name.
        self.assertIsNone(merged.author_regex,
            msg="silent fallback restored prior author — Round 8 C3-2 regression")
        self.assertEqual(
            (merged.raw_misc or {}).get("unresolved_author_named"),
            "Нонэксистентов",
        )

    def test_unknown_author_clarify_message(self):
        from scripts.v2.planner import plan as plan_mod
        from scripts.v2.planner.entities import Entities
        e = Entities(raw_misc={"unresolved_author_named": "Марлоид",
                                "raw_text": "теперь у Марлоид"})
        p = plan_mod.build("author_vocab", e)
        self.assertTrue(p.needs_clarify)
        self.assertIn("Марлоид", p.clarify_question or "")
        self.assertIn("не узнаю", (p.clarify_question or "").lower())

    def test_implicit_followup_still_backfills(self):
        """«ещё примеры» (no explicit new author) — must still backfill
        from prior. The block applies ONLY to explicit-named-author
        cases."""
        from scripts.v2.planner import history as hist_mod
        from scripts.v2.planner.entities import extract
        prior = [
            {"role": "user", "content": "фирменные слова Doyle"},
            {"role": "assistant", "content": "blighter, hullo..."},
        ]
        current = extract("ещё примеры")
        merged = hist_mod.merge_with_history(current, prior, "ещё примеры")
        # No swap trigger + no explicit author → safe to backfill
        self.assertEqual(merged.author_regex, "^Doyle,")


class Round8ElizabethanDramatists(unittest.TestCase):
    """Round 8 surfaced Marlowe gap. Added the cohort — these aliases
    should resolve to PG-style ^Surname, regex."""

    KNOWN_DRAMATISTS = [
        ("Marlowe", "^Marlowe,"),
        ("Кристофер Марло", "^Marlowe,"),
        ("Уэбстер", "^Webster, John"),
        ("Ben Jonson", "^Jonson, Ben"),
        ("Бен Джонсон", "^Jonson, Ben"),
        ("Dekker", "^Dekker,"),
        ("Kyd", "^Kyd,"),
        ("Beaumont", "^Beaumont,"),
        ("Middleton", "^Middleton,"),
    ]

    def test_each_dramatist_resolves(self):
        for label, expected in self.KNOWN_DRAMATISTS:
            with self.subTest(author=label):
                e = ent_mod.extract(f"фирменные слова у {label}")
                self.assertEqual(e.author_regex, expected,
                    msg=f"{label!r} should resolve to {expected!r}")


class Round8AuthorAttribution(unittest.TestCase):
    """Round 8 C7: «угадай автора отрывка X» — phrasing missed by the
    existing «кто автор» rule. Added passage-attribution triggers."""

    POSITIVE = [
        "угадай автора отрывка «the fog came pouring in»",
        "отгадай автора этого отрывка",
        "определи автора текста",
        "чей этот отрывок?",
        "чей отрывок?",
        "identify the author of this passage",
        "whose excerpt is this",
        "who is the author of this text",
    ]

    NEGATIVE = [
        # Bibliographic — not attribution; must NOT classify as
        # author_attribution (any other intent or clarify is fine)
        ("сколько книг у Doyle", "author_attribution"),
    ]

    def test_passage_attribution_classifies(self):
        for q in self.POSITIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "author_attribution",
                    msg=f"{q!r} → {m.label!r}")

    def test_bibliographic_not_stolen(self):
        for q, must_not in self.NEGATIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertNotEqual(m.label, must_not)


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
