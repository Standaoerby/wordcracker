"""R-28 заход 1 — B114 «честный учебный контент» (D-R28-1).

Политика: учебный факт без tool-опоры не публикуется.

Тест-ран Q4 + смоук S4 (prod 2.7.x): в учебных word-бандлах LLM выдумывал
факты — этимологию (ajar/garlic «от греческого krokos»), переводы
(phonograph→«фоноскоп», galatz→«стеклянный сосуд»; galatz — ТОПОНИМ,
город Галац из «Дракулы»). Корень: enrich_word = FIXTURE_EXEMPT,
его контент генерится LLM и входит в ответ через ДАННЫЕ инструмента —
evidence-модель WP2b такие фабрикации пропускала.

Четыре слоя фикса:
  1. enrich_word-враппер стирает LLM-этимологию и example_sentence,
     гейтит перевод имён собственных, помечает выживший перевод
     кэвиатом (scripts/v2/tools/learning/enrich.py).
  2. ENRICH_PROMPT больше не просит LLM сочинять этимологию
     (scripts/learning_tools.py).
  3. Детерминированные critic-пассы (suppress, не warn):
     audit_etymology_claims / audit_example_quotes (scripts/v2/critic.py).
  4. Кураторский toponym-слой расширен топонимами «Дракулы»
     (galatz-дыра; scripts/v2/tools/authors/_toponym_filter.py).

R5 — новые регексы (_ETYM_CLAIM_RE / _ETYM_ABSENCE_RE / blockquote)
имеют здесь позитивные И негативные кейсы.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2 import critic as critic_mod


# Канонический честный текст отсутствия этимологии (брифом задан дословно).
_ABSENCE_TEXT = "Этимологии этого слова в данных корпуса нет"


def _v1_enrich_shape(word: str, **overrides) -> dict:
    """Реальная V1EnrichWord-форма (R4 — мок по фактическому return v1)."""
    base = {
        "word": word,
        "translation_ru": "приоткрытый",
        "definition_en": "slightly open",
        "pos": "ADJ",
        "cefr_estimate": "B2",
        "lemma": word,
        "example_sentence": f"The door stood {word} in the night.",
        "etymology": "From Old Norse 'a kjarr', approximately.",
        "proper_noun": False,
        "archaic": False,
        "archaic_note": "",
        "primary_family": "germanic",
        "family_chain": ["middle_english", "old_norse"],
        "ipa": "əˈdʒɑːr",
        "related_forms": [], "cognates": [], "derived_from": [],
        "_cached": False, "_lookup_ms": 5.0,
    }
    base.update(overrides)
    return base


def _call_enrich(word: str, **overrides):
    from scripts.v2.tools.learning.enrich import enrich_word
    with mock.patch("scripts.learning_tools.enrich_word",
                    return_value=_v1_enrich_shape(word, **overrides)):
        return enrich_word(word=word)


# =====================================================================
# Задача 1 — этимология только из word_etymology-данных
# =====================================================================


class EnrichWrapperStripsLlmEtymology(unittest.TestCase):
    """LLM-этимология enrich_word стирается на враппере: и свежая
    генерация, и старые записи дискового кэша word_dictionary.json."""

    def test_etymology_fields_blanked(self):
        r = _call_enrich("ajar")
        self.assertTrue(r.ok)
        self.assertEqual(r.data.get("etymology"), "")
        self.assertEqual(r.data.get("family_chain"), [])
        self.assertEqual(r.data.get("primary_family"), "")

    def test_render_note_points_to_word_etymology(self):
        r = _call_enrich("ajar")
        note = r.data.get("_render_note") or ""
        self.assertIn("word_etymology", note)
        self.assertIn(_ABSENCE_TEXT, note)

    def test_view_etymology_slot_empty(self):
        r = _call_enrich("ajar")
        self.assertIsNotNone(r.view)
        self.assertIsNone((r.view.payload or {}).get("etymology"))


class EnrichPromptNoLongerAsksEtymology(unittest.TestCase):
    """ENRICH_PROMPT не просит LLM сочинять этимологию («ok if
    approximate» порождало krokos-класс фантазий)."""

    def test_prompt_has_no_etymology_field(self):
        from scripts.learning_tools import ENRICH_PROMPT
        self.assertNotIn('"etymology"', ENRICH_PROMPT)

    def test_prompt_keeps_proper_noun_verdict(self):
        # proper_noun-вердикт нужен propn-гейту перевода (задача 3а).
        from scripts.learning_tools import ENRICH_PROMPT
        self.assertIn('"proper_noun"', ENRICH_PROMPT)


class EtymologyClaimGuard(unittest.TestCase):
    """Детерминированный critic-пасс: этимологический клейм без опоры
    в word_etymology-выходе → suppress (механика WP2b)."""

    _FABRICATED = ("Слово ajar — интересный случай. Этимология: происходит "
                   "от греческого krokos, что означает шафран. Перевод — "
                   "«приоткрытый».")

    def _records(self, *, with_etymology: bool):
        recs = [{"tool": "enrich_word", "ok": True,
                 "data": {"word": "ajar", "translation_ru": "приоткрытый"}}]
        if with_etymology:
            recs.append({"tool": "word_etymology", "ok": True,
                         "data": {"word": "ajar",
                                  "family_chain": ["middle_english",
                                                   "old_norse"],
                                  "primary_family": "germanic"}})
        return recs

    def test_fabricated_etymology_is_flagged(self):
        rep = critic_mod.audit_etymology_claims(
            self._FABRICATED, self._records(with_etymology=False))
        self.assertTrue(rep.has_issues())

    def test_suppress_excises_claim_and_appends_honest_text(self):
        rep = critic_mod.audit_etymology_claims(
            self._FABRICATED, self._records(with_etymology=False))
        out = critic_mod.suppress_etymology_claims(self._FABRICATED, rep)
        self.assertNotIn("krokos", out)
        self.assertIn(_ABSENCE_TEXT, out)
        # Не-этимологический контент выжил.
        self.assertIn("приоткрытый", out)

    def test_tool_backed_etymology_survives(self):
        # Негатив на over-suppress: реальная этимология из word_etymology
        # живёт.
        answer = ("**Этимология:** germanic (middle_english → old_norse). "
                  "Перевод — «приоткрытый».")
        rep = critic_mod.audit_etymology_claims(
            answer, self._records(with_etymology=True))
        self.assertFalse(rep.has_issues())
        self.assertEqual(
            critic_mod.suppress_etymology_claims(answer, rep), answer)

    def test_honest_absence_text_is_not_excised(self):
        # Канонический честный текст сам содержит «Этимологии» — он
        # клеймом не считается.
        answer = f"{_ABSENCE_TEXT}. Перевод — «приоткрытый»."
        rep = critic_mod.audit_etymology_claims(
            answer, self._records(with_etymology=False))
        self.assertFalse(rep.has_issues())

    def test_narrative_proiskhodit_ot_is_not_a_claim(self):
        # R5 негатив: повествовательное «происходит от лица рассказчика»
        # — не этимологический клейм.
        answer = "Действие в книге происходит от лица рассказчика."
        rep = critic_mod.audit_etymology_claims(
            answer, self._records(with_etymology=False))
        self.assertFalse(rep.has_issues())

    def test_failed_word_etymology_is_not_evidence(self):
        # word_etymology с ok=False (not_found) — не опора.
        recs = [{"tool": "word_etymology", "ok": False,
                 "data": {"error": "no wiktionary page", "word": "ajar",
                          "family_chain": []}}]
        rep = critic_mod.audit_etymology_claims(self._FABRICATED, recs)
        self.assertTrue(rep.has_issues())

    def test_enrich_word_data_is_not_evidence(self):
        # Старый кэш enrich_word с family_chain — НЕ опора (policy B114):
        # только word_etymology / find_words_by_etymology.
        recs = [{"tool": "enrich_word", "ok": True,
                 "data": {"word": "ajar",
                          "family_chain": ["middle_english"],
                          "primary_family": "germanic"}}]
        rep = critic_mod.audit_etymology_claims(self._FABRICATED, recs)
        self.assertTrue(rep.has_issues())


# =====================================================================
# Задача 2 — примеры только из корпусных hits → suppress
# =====================================================================


class EnrichWrapperStripsExampleSentence(unittest.TestCase):
    def test_example_sentence_blanked(self):
        r = _call_enrich("ajar")
        self.assertEqual(r.data.get("example_sentence"), "")
        note = r.data.get("_render_note") or ""
        self.assertIn("корпусных сниппетов", note)


class ExampleQuoteGuard(unittest.TestCase):
    """Blockquote-примеры без корпусной опоры в tool data → удаляются
    построчно + честный disclosure."""

    _CORPUS_SNIPPET = ("the door of the room stood ajar and a thin "
                       "stream of light fell across the passage")

    def _records(self):
        return [{"tool": "hybrid_search", "ok": True,
                 "data": {"matches": [
                     {"snippet": self._CORPUS_SNIPPET,
                      "title": "Bleak House", "author": "Dickens"}]}}]

    def test_supported_quote_survives(self):
        answer = ("**Примеры из корпуса:**\n"
                  "> the door of the room stood ajar and a thin stream "
                  "of light — Dickens, *Bleak House*")
        rep = critic_mod.audit_example_quotes(answer, self._records())
        self.assertFalse(rep.has_issues())
        self.assertEqual(
            critic_mod.suppress_unsupported_quotes(answer, rep), answer)

    def test_fabricated_quote_is_removed(self):
        answer = ("**Примеры из корпуса:**\n"
                  "> She left the garden gate ajar hoping the cat would "
                  "return before midnight\n"
                  "Перевод — «приоткрытый».")
        rep = critic_mod.audit_example_quotes(answer, self._records())
        self.assertTrue(rep.has_issues())
        out = critic_mod.suppress_unsupported_quotes(answer, rep)
        self.assertNotIn("garden gate ajar", out)
        self.assertIn("Honesty guard", out)
        self.assertIn("приоткрытый", out)

    def test_short_blockquote_is_skipped(self):
        # <4 латинских токенов — не проверяется (русские пояснения,
        # шаблонные подсказки).
        answer = "> переведи ardour, fog\nОстальной ответ."
        rep = critic_mod.audit_example_quotes(answer, self._records())
        self.assertFalse(rep.has_issues())

    def test_truncated_supported_quote_survives(self):
        # Рендерер усёк сниппет «…» — токены всё равно из data.
        answer = "> the door of the room stood ajar and a thin…"
        rep = critic_mod.audit_example_quotes(answer, self._records())
        self.assertFalse(rep.has_issues())


# =====================================================================
# Задача 3 — переводы: кэвиат + propn-гейт
# =====================================================================


class TranslationCaveat(unittest.TestCase):
    def test_surviving_translation_carries_model_caveat_note(self):
        r = _call_enrich("ajar")
        note = r.data.get("_render_note") or ""
        self.assertIn("не словарём", note)
        # Перевод при этом не тронут.
        self.assertEqual(r.data.get("translation_ru"), "приоткрытый")

    def test_view_caveat_present(self):
        r = _call_enrich("ajar")
        caveats = " ".join(str(c) for c in (r.view.caveats or []))
        self.assertIn("не словарём", caveats)


class ProperNounTranslationGate(unittest.TestCase):
    """Имя собственное не получает перевода — вместо него
    «имя собственное (вероятно топоним/персонаж)»."""

    def test_galatz_toponym_is_gated(self):
        # S4: galatz (город Галац, «Дракула») получал «стеклянный сосуд».
        r = _call_enrich("galatz", translation_ru="стеклянный сосуд",
                         proper_noun=False)  # LLM-вердикт тоже промахнулся
        self.assertEqual(r.data.get("translation_ru"), "")
        self.assertTrue(r.data.get("propn_gate"))
        note = r.data.get("_render_note") or ""
        self.assertIn("имя собственное (вероятно топоним/персонаж)", note)

    def test_gazetteer_given_name_is_gated(self):
        r = _call_enrich("natasha", translation_ru="наташа")
        self.assertEqual(r.data.get("translation_ru"), "")
        self.assertTrue(r.data.get("propn_gate"))

    def test_llm_proper_noun_verdict_gates(self):
        # Слово вне кураторских списков, но LLM сам сказал proper_noun.
        r = _call_enrich("quincey", proper_noun=True,
                         translation_ru="куинси")
        self.assertEqual(r.data.get("translation_ru"), "")
        self.assertEqual(r.data.get("propn_gate"),
                         "LLM-вердикт proper_noun")

    def test_ordinary_word_is_not_gated(self):
        # Негатив на over-suppress: обычное слово переводится (с кэвиатом).
        r = _call_enrich("ardour", translation_ru="пыл")
        self.assertEqual(r.data.get("translation_ru"), "пыл")
        self.assertNotIn("propn_gate", r.data)


# =====================================================================
# Задача 4 — galatz-дыра: кураторское покрытие топонимов «Дракулы»
# =====================================================================


class DraculaToponymsExtension(unittest.TestCase):
    def test_dracula_toponyms_are_known(self):
        from scripts.v2.tools.authors._toponym_filter import is_toponym
        for t in ("galatz", "varna", "bistritz", "borgo", "bukovina"):
            self.assertTrue(is_toponym(t), msg=f"{t} must be a toponym")

    def test_ordinary_learning_words_are_not_toponyms(self):
        # Негатив на over-suppress: учебная лексика не режется.
        from scripts.v2.tools.authors._toponym_filter import is_toponym
        for w in ("garlic", "ardour", "phonograph", "ajar"):
            self.assertFalse(is_toponym(w), msg=f"{w} must NOT be a toponym")

    def test_filter_toponyms_drops_galatz_row(self):
        # Путь learning_words(book:PG345): строка с galatz выпадает на
        # toponym-слое (WP3-цепочка подключена к book-scope — Фаза 0c).
        from scripts.v2.tools.authors._toponym_filter import filter_toponyms
        rows = [{"word": "galatz"}, {"word": "garlic"}]
        kept, dropped = filter_toponyms(rows, ner_csv_paths=[])
        self.assertEqual(dropped, 1)
        self.assertEqual([r["word"] for r in kept], ["garlic"])


# =====================================================================
# Render notes — точный честный текст в живом LLM-рендер-пути
# =====================================================================


class RenderNotesCarryHonestInstructions(unittest.TestCase):
    def _notes(self, build_fn, query: str) -> str:
        from scripts.v2.planner.entities import extract
        plan = build_fn(extract(query))
        return " ".join(plan.render_notes or [])

    def test_word_contexts_bundle_notes(self):
        from scripts.v2.planner.builders.word import _plan_word_contexts
        notes = self._notes(_plan_word_contexts, "что значит ajar")
        self.assertIn(_ABSENCE_TEXT, notes)
        self.assertIn("не словарём", notes)
        self.assertIn("имя собственное (вероятно топоним/персонаж)", notes)

    def test_word_etymology_bundle_notes(self):
        from scripts.v2.planner.builders.word import _plan_word_etymology
        notes = self._notes(_plan_word_etymology, "этимология слова engine")
        self.assertIn(_ABSENCE_TEXT, notes)
        self.assertIn("не словарём", notes)


if __name__ == "__main__":
    unittest.main()
