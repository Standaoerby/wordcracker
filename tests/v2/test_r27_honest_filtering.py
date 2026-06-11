"""R-27 WP3 «honest filtering» + B115 + B116 (2026-06-11).

Live repro (prod 2.7.3, тест-ран 2026-06-10): Q7 «лексика Льва Толстого»
/ Q8 «убери русские имена и фамилии» — ответ заявил «после фильтрации
имён собственных … осталось 24», при этом в таблице остались hélène,
sergius, petrovna, hippolyte, nicholas. Формула бага: «правда, что
фильтровали — ложь, что отфильтровали».

R2-негативы этого файла падают на до-фиксовом коде:
  * лик-лист 1c — старая цепочка фильтров пропускала lowercase /
    accented имена (нет gazetteer, нет accent-fold, нет cap-ratio);
  * clean-семантика — флага `clean` в data не существовало;
  * critic «заявлено vs показано» — функций не существовало;
  * B115 — санитайзера не существовало;
  * B116 — «~159k» при «159 075» в таблице срезался WP2b-repair'ом.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Регрессионный лик-лист 1c — все должны отсекаться обоими тулами.
LEAK_LIST = [
    "hélène", "sergius", "petrovna", "hippolyte", "nicholas",
    "anna", "betsy", "theodore", "berg", "missy",
    "pávlovna", "iván", "márya", "nikolay", "taras",
]

# Негатив на over-filtering: легитимные слова не отсекаются (включая
# словоформы вокруг «berg»).
LEGIT_WORDS = [
    "iceberg", "bergamot", "bergs", "blighter", "indubitably",
    "ancient", "candle", "stitch", "whisky", "casanova", "retina",
]


def _affinity_row(word: str) -> dict:
    # Shape per V1AffinityByAuthor.__row_keys__ (rag_tools.py:735).
    # affinity < 80 / corpus_count > 200 so the propn-dominance heuristic
    # does NOT fire — the new layers must do the catching themselves.
    return {"word": word, "author_count": 50, "corpus_count": 5000,
            "affinity": 12.0}


def _learning_row(word: str) -> dict:
    # Shape per V1LearningWords.__row_keys__ (learning_tools.py:530/536).
    return {"word": word, "lemma": word, "pos": "NOUN", "scope_count": 5,
            "corpus_count": 500, "affinity": 3.0, "score": 0.5}


def _affinity_v1(rows: list[dict]) -> dict:
    return {
        "author_regex": "^Tolstoy, Leo", "slug": "tolstoy_leo",
        "pos_filter": None, "effective_min_corpus_count": 0,
        "total_unique_words": 9000, "top": rows, "cached": True,
        "proper_noun_filter": "corpus-diff heuristic dropped 0, "
                              "spaCy PROPN dropped 0",
    }


def _learning_v1(rows: list[dict]) -> dict:
    return {
        "scope": "author:^Tolstoy,", "level": "intermediate",
        "band_min": 100, "band_max": 10_000, "top_n": 30,
        "candidates": len(rows), "results": rows,
    }


# =====================================================================
# Задача 1 — детектор (чистая функция, общая для фильтра и критика)
# =====================================================================

class ProperNameDetector(unittest.TestCase):

    def test_leak_list_all_detected(self):
        from scripts.v2.tools.authors._propn_gazetteer import (
            is_proper_name_token,
        )
        for w in LEAK_LIST:
            with self.subTest(word=w):
                self.assertTrue(is_proper_name_token(w),
                                f"лик-лист 1c: {w!r} должен отсекаться")

    def test_legit_words_not_detected(self):
        from scripts.v2.tools.authors._propn_gazetteer import (
            is_proper_name_token,
        )
        for w in LEGIT_WORDS:
            with self.subTest(word=w):
                self.assertFalse(is_proper_name_token(w),
                                 f"over-filtering: {w!r} должен жить")

    def test_accent_folding(self):
        from scripts.v2.tools.authors._propn_gazetteer import fold_accents
        self.assertEqual(fold_accents("hélène"), "helene")
        self.assertEqual(fold_accents("pávlovna"), "pavlovna")
        self.assertEqual(fold_accents("iván"), "ivan")
        self.assertEqual(fold_accents("márya"), "marya")

    def test_patronymic_suffixes_generic(self):
        from scripts.v2.tools.authors._propn_gazetteer import (
            is_proper_name_token,
        )
        # Generic patronymics — NOT curated one-by-one.
        for w in ("ivanovich", "andreevna", "nikolaevich", "bolkonskaya"):
            self.assertTrue(is_proper_name_token(w), w)
        # Suffix collisions deliberately not matched.
        for w in ("whisky", "casanova", "nova"):
            self.assertFalse(is_proper_name_token(w), w)

    def test_capitalization_ratio_layer(self):
        from scripts.v2.tools.authors._propn_gazetteer import (
            filter_by_cap_ratio,
        )
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "PG1_tokens.txt"
            # «Fakename» — всегда капитализирован (имя, не из gazetteer);
            # «blighter» — капитализирован 1 раз из 12 (начало предложения).
            tok.write_text(
                "\n".join(["Fakename"] * 12
                          + ["blighter"] * 11 + ["Blighter"]),
                encoding="utf-8")
            rows = [_affinity_row("fakename"), _affinity_row("blighter")]
            kept, dropped, verified = filter_by_cap_ratio(rows, [tok])
            self.assertTrue(verified)
            self.assertEqual(dropped, 1)
            self.assertEqual([r["word"] for r in kept], ["blighter"])

    def test_cap_ratio_degrades_to_unverified(self):
        from scripts.v2.tools.authors._propn_gazetteer import (
            filter_by_cap_ratio,
        )
        rows = [_affinity_row("blighter")]
        kept, dropped, verified = filter_by_cap_ratio(
            rows, [Path("/no/such/tokens.txt")])
        self.assertFalse(verified)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept, rows)


# =====================================================================
# Задача 1 — wiring в affinity_by_author / learning_words (лик-лист 1c
# параметризованно по обоим тулам + over-filtering негатив)
# =====================================================================

class AffinityLeakList(unittest.TestCase):

    def _run(self, rows):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                        return_value=_affinity_v1(rows)):
            return affinity_by_author("^Tolstoy, Leo", top=30)

    def test_leak_list_filtered(self):
        r = self._run([_affinity_row(w) for w in LEAK_LIST]
                      + [_affinity_row(w) for w in LEGIT_WORDS])
        self.assertTrue(r.ok)
        words = [t["word"] for t in r.data["top"]]
        for w in LEAK_LIST:
            with self.subTest(word=w):
                self.assertNotIn(w, words)
        for w in LEGIT_WORDS:
            with self.subTest(word=w):
                self.assertIn(w, words, f"over-filtering: {w!r} отсечён")
        self.assertIn("name gazetteer", r.data["proper_noun_filter"])

    def test_requested_vs_returned_contract_kept(self):
        # 1e — контракт count-honesty: top_requested/top_returned
        # по-прежнему стампятся, renderer называет фактическое N.
        r = self._run([_affinity_row(w) for w in LEAK_LIST]
                      + [_affinity_row("blighter")])
        self.assertEqual(r.data["top_requested"], 30)
        self.assertEqual(r.data["top_returned"], 1)
        self.assertIn("ACTUAL COUNT", r.data["_render_note"])


class LearningWordsLeakList(unittest.TestCase):

    def _run(self, rows, scope=None):
        from scripts.v2.tools.learning.learning_words import learning_words
        with mock.patch("scripts.learning_tools.learning_words",
                        return_value=_learning_v1(rows)):
            return learning_words(scope or {"author": "^Tolstoy,"}, top=30)

    def test_leak_list_filtered(self):
        r = self._run([_learning_row(w) for w in LEAK_LIST]
                      + [_learning_row(w) for w in LEGIT_WORDS])
        self.assertTrue(r.ok)
        words = [t["word"] for t in r.data["results"]]
        for w in LEAK_LIST:
            with self.subTest(word=w):
                self.assertNotIn(w, words)
        for w in LEGIT_WORDS:
            with self.subTest(word=w):
                self.assertIn(w, words, f"over-filtering: {w!r} отсечён")
        self.assertIn("name gazetteer", r.data["_render_note"])

    def test_requested_vs_returned_contract_kept(self):
        r = self._run([_learning_row(w) for w in LEAK_LIST]
                      + [_learning_row("blighter")])
        self.assertEqual(r.data["top_requested"], 30)
        self.assertEqual(r.data["top_returned"], 1)
        self.assertIn("ACTUAL COUNT", r.data["_render_note"])


# =====================================================================
# Задача 1d — clean-семантика
# =====================================================================

class CleanSemantics(unittest.TestCase):

    def test_affinity_clean_false_without_corpus(self):
        # Локально/в CI tokens-файлов нет → cap-ratio не верифицирован →
        # фильтр имён частичный → clean=False.
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                        return_value=_affinity_v1(
                            [_affinity_row("blighter")])):
            r = affinity_by_author("^Tolstoy, Leo", top=30)
        self.assertIs(r.data["clean"], False)

    def test_affinity_clean_true_with_verified_corpus(self):
        import scripts.v2.tools.authors.affinity as aff_mod
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "PG1_tokens.txt"
            tok.write_text("\n".join(["blighter"] * 15), encoding="utf-8")
            with mock.patch("scripts.rag_tools.affinity_by_author",
                            return_value=_affinity_v1(
                                [_affinity_row("blighter")])), \
                 mock.patch.object(aff_mod, "_author_token_files",
                                   return_value=[tok]):
                r = aff_mod.affinity_by_author("^Tolstoy, Leo", top=30)
        self.assertIs(r.data["clean"], True)

    def test_affinity_clean_false_on_detector_leftover(self):
        # Post-filter детектор — независимый back-stop: если строка с
        # именем добралась до итогового списка (обход фильтра, баг,
        # старый кэш) — clean=False + _propn_leftover.
        import scripts.v2.tools.authors.affinity as aff_mod
        with mock.patch("scripts.rag_tools.affinity_by_author",
                        return_value=_affinity_v1(
                            [_affinity_row("hélène"),
                             _affinity_row("blighter")])), \
             mock.patch.object(aff_mod, "filter_proper_names",
                               side_effect=lambda rows, **kw: (rows, 0)):
            r = aff_mod.affinity_by_author("^Tolstoy, Leo", top=30)
        self.assertIs(r.data["clean"], False)
        self.assertIn("hélène", r.data["_propn_leftover"])

    def test_learning_words_all_corpus_clean_false(self):
        # all_corpus: cap-ratio скан всего корпуса не делается →
        # верификации нет → clean=False (честно: имена могли остаться).
        from scripts.v2.tools.learning.learning_words import learning_words
        with mock.patch("scripts.learning_tools.learning_words",
                        return_value=_learning_v1(
                            [_learning_row("blighter")])):
            r = learning_words("all_corpus", top=30)
        self.assertIs(r.data["clean"], False)

    def test_empty_result_is_clean(self):
        from scripts.v2.tools.learning.learning_words import learning_words
        with mock.patch("scripts.learning_tools.learning_words",
                        return_value=_learning_v1([])):
            r = learning_words({"book": "PG1342"}, top=30)
        self.assertIs(r.data["clean"], True)


# =====================================================================
# Задача 3 — renderer wording при clean=False
# =====================================================================

class RendererCleanWording(unittest.TestCase):

    def test_partial_filter_note_when_not_clean(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                        return_value=_affinity_v1(
                            [_affinity_row("blighter")])):
            r = affinity_by_author("^Tolstoy, Leo", top=30)
        note = r.data["_render_note"]
        self.assertIn("ЧАСТИЧНЫЙ фильтр", note)
        self.assertIn("могли остаться", note)
        self.assertNotIn("после фильтра имён собственных и редких токенов "
                         "осталось", note)

    def test_assertive_phrasing_only_when_clean(self):
        import scripts.v2.tools.authors.affinity as aff_mod
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "PG1_tokens.txt"
            tok.write_text("\n".join(["blighter"] * 15), encoding="utf-8")
            with mock.patch("scripts.rag_tools.affinity_by_author",
                            return_value=_affinity_v1(
                                [_affinity_row("blighter")])), \
                 mock.patch.object(aff_mod, "_author_token_files",
                                   return_value=[tok]):
                r = aff_mod.affinity_by_author("^Tolstoy, Leo", top=30)
        self.assertIn("после фильтра имён собственных",
                      r.data["_render_note"])

    def test_learning_words_partial_note(self):
        from scripts.v2.tools.learning.learning_words import learning_words
        with mock.patch("scripts.learning_tools.learning_words",
                        return_value=_learning_v1(
                            [_learning_row("blighter")])):
            r = learning_words({"author": "^Tolstoy,"}, top=30)
        self.assertIn("частичный фильтр", r.data["_render_note"])

    def test_rule21_addendum_in_render_prompt(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Частичный фильтр ≠ полный", RENDER_PROMPT)
        self.assertIn("clean: false", RENDER_PROMPT)


# =====================================================================
# Задача 2 — critic «заявлено vs показано» (чистая функция, без LLM)
# =====================================================================

_CLAIM_ANSWER_DIRTY = (
    "Вот лексика Толстого. Запрошено 30 слов, но после фильтрации имён "
    "собственных и редких токенов осталось 24.\n\n"
    "| rank | word | affinity |\n"
    "|---|---|---|\n"
    "| 1 | hélène | 42.0 |\n"
    "| 2 | sergius | 38.1 |\n"
    "| 3 | blighter | 12.0 |\n"
)

_CLAIM_ANSWER_CLEAN = (
    "Вот лексика Толстого. Запрошено 30 слов, но после фильтрации имён "
    "собственных и редких токенов осталось 24.\n\n"
    "| rank | word | affinity |\n"
    "|---|---|---|\n"
    "| 1 | blighter | 12.0 |\n"
    "| 2 | ardour | 9.3 |\n"
)


class CriticClaimedVsShown(unittest.TestCase):

    def test_positive_names_in_rows_suppress_claim(self):
        from scripts.v2 import critic as critic_mod
        rep = critic_mod.audit_claimed_vs_shown(_CLAIM_ANSWER_DIRTY)
        self.assertTrue(rep.has_issues())
        self.assertIn("hélène", rep.shown_names)
        out = critic_mod.suppress_partial_filter_claims(
            _CLAIM_ANSWER_DIRTY, rep)
        self.assertNotIn("после фильтрации имён собственных", out)
        self.assertIn("частичный", out.lower())
        self.assertIn("могли", out)
        # Таблица с данными остаётся нетронутой.
        self.assertIn("| 1 | hélène | 42.0 |", out)

    def test_negative_clean_table_keeps_claim(self):
        from scripts.v2 import critic as critic_mod
        rep = critic_mod.audit_claimed_vs_shown(_CLAIM_ANSWER_CLEAN)
        self.assertFalse(rep.has_issues())
        out = critic_mod.suppress_partial_filter_claims(
            _CLAIM_ANSWER_CLEAN, rep)
        self.assertEqual(out, _CLAIM_ANSWER_CLEAN)

    def test_partial_disclosure_claim_not_suppressed(self):
        # Уже честная формулировка не вырезается даже при именах в строках.
        from scripts.v2 import critic as critic_mod
        answer = _CLAIM_ANSWER_DIRTY.replace(
            "после фильтрации имён собственных и редких токенов осталось 24",
            "применён частичный фильтр имён — в списке могли остаться имена",
        )
        rep = critic_mod.audit_claimed_vs_shown(answer)
        self.assertFalse(rep.has_issues())

    def test_no_claim_no_report(self):
        from scripts.v2 import critic as critic_mod
        answer = ("Топ слов:\n\n| rank | word |\n|---|---|\n"
                  "| 1 | hélène |\n")
        rep = critic_mod.audit_claimed_vs_shown(answer)
        self.assertFalse(rep.has_issues())


# =====================================================================
# B115 — служебные render-инструкции не доходят до пользователя
# =====================================================================

class B115ServiceLeak(unittest.TestCase):

    def test_leaked_warning_line_stripped(self):
        # Дословный канал Q7: текст under_filled warning'а отзеркален LLM.
        from scripts.v2.render_sanitizer import strip_service_lines
        answer = (
            "Вот список слов.\n"
            "requested top=30, returned 27 after filtering — renderer "
            "must say 27\n"
            "| rank | word |\n|---|---|\n| 1 | blighter |\n"
        )
        out = strip_service_lines(answer)
        self.assertNotIn("renderer must say", out)
        self.assertNotIn("requested top=", out)
        self.assertIn("Вот список слов.", out)
        self.assertIn("| 1 | blighter |", out)

    def test_actual_count_note_stripped(self):
        from scripts.v2.render_sanitizer import strip_service_lines
        answer = ("Слова уровня B2.\n"
                  "ACTUAL COUNT: tool returned 24 words — use 24.\n"
                  "Всего 24 слова.")
        out = strip_service_lines(answer)
        self.assertNotIn("ACTUAL COUNT", out)
        self.assertIn("Всего 24 слова.", out)

    def test_tagged_note_line_stripped_but_markdown_link_kept(self):
        from scripts.v2.render_sanitizer import strip_service_lines
        answer = ("[affinity_by_author] cosine_similarity ниже 0.05 — "
                  "структурное свойство\n"
                  "Подробнее: [книга](https://example.org/x)\n")
        out = strip_service_lines(answer)
        self.assertNotIn("[affinity_by_author]", out)
        self.assertIn("[книга](https://example.org/x)", out)

    def test_clean_text_untouched(self):
        from scripts.v2.render_sanitizer import strip_service_lines
        answer = "Обычный ответ.\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(strip_service_lines(answer), answer)

    def test_stream_scrubber_drops_service_line_in_flight(self):
        from scripts.v2.render_sanitizer import StreamLineScrubber
        emitted: list[str] = []
        s = StreamLineScrubber(emitted.append)
        # Дельты рвут строки в произвольных местах — как реальный стрим.
        for piece in ("Вот сло", "ва.\nrequested top=30 … renderer ",
                      "must say 27\nИто", "го: 27 слов."):
            s.feed(piece)
        s.flush()
        joined = "".join(emitted)
        self.assertNotIn("renderer must say", joined)
        self.assertIn("Вот слова.\n", joined)
        self.assertIn("Итого: 27 слов.", joined)


# =====================================================================
# B116 — numeric-repair: нормализация и trust видимых таблиц
# =====================================================================

class B116NumericRepair(unittest.TestCase):

    _RECORDS = [{"tool": "book_readability", "ok": True,
                 "data": {"pg_id": "PG2701"}, "query": {"book": "PG2701"}}]

    def _audit(self, answer):
        from scripts.v2 import numeric_audit as na
        rep = na.audit_numbers(answer, self._RECORDS,
                               intent="book_readability")
        return na.repair_with_audit(answer, rep), rep

    def test_159k_with_table_value_not_cut(self):
        # Q16: «~159k слов» при 159 075 в таблице — НЕ режется.
        answer = ("Всего в книге ~159k слов.\n\n"
                  "| Книга | Слов |\n|---|---|\n| Moby Dick | 159 075 |\n")
        fixed, rep = self._audit(answer)
        self.assertIn("~159k", fixed)
        self.assertEqual([m.value for m in rep.mismatches], [])

    def test_159_tys_variant_not_cut(self):
        answer = ("Объём — около 159 тыс. слов.\n\n"
                  "| Книга | Слов |\n|---|---|\n| Moby Dick | 159,075 |\n")
        fixed, rep = self._audit(answer)
        self.assertIn("159 тыс.", fixed)
        self.assertEqual(rep.mismatches, [])

    def test_fabricated_number_still_cut(self):
        # Регрессия: выдуманное «331» режется по-прежнему.
        answer = ("Всего в книге ~159k слов. В корпусе 331 уникальный "
                  "диалект.\n\n"
                  "| Книга | Слов |\n|---|---|\n| Moby Dick | 159 075 |\n")
        fixed, rep = self._audit(answer)
        self.assertIn("~159k", fixed)
        self.assertEqual([m.formatted for m in rep.mismatches], ["331"])
        self.assertNotIn("331 уникальный", fixed)

    def test_table_number_normalization_separators(self):
        from scripts.v2.numeric_audit import collect_answer_table_numbers
        answer = ("| a | b |\n|---|---|\n"
                  "| x | 159 075 |\n| y | 1,234 |\n| z | 55K |\n")
        nums = collect_answer_table_numbers(answer)
        self.assertIn(159075.0, nums)
        self.assertIn(1234.0, nums)
        self.assertIn(55000.0, nums)

    def test_prose_only_fabrication_with_no_table_still_flagged(self):
        answer = "У автора 47 книг в корпусе."
        fixed, rep = self._audit(answer)
        self.assertEqual([m.formatted for m in rep.mismatches], ["47"])


if __name__ == "__main__":
    unittest.main()
