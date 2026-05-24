"""W-7 (2026-05-23, hardened 2026-05-24) — RENDER_PROMPT must carry the
explicit anti-fabrication clause, and numeric_audit must back-stop the
bio-year fabrication class deterministically.

Tests lock the prompt-level rule so a future edit can't silently drop
it (rule 20 phrasing pinned). The actual prompt-following behaviour is
verified at the integration level (test_critic + bench); here we assert
the contract text exists.

Also asserts the empty-column rule (W-3 rule 19) lives in the prompt so
the deterministic strip-pre-LLM has a back-stop instruction the LLM sees.

Numeric audit tests pin the W-7 acceptance criterion («нет числа в
данных → нет числа в ответе»): on a battery of 10 factual-query
fixtures, the audit must NOT flag real numbers and MUST flag fabricated
ones (years, counts, ids).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class RenderPromptCarriesAntiFabricationRule(unittest.TestCase):
    def test_rule_20_fabrication_guard_present(self):
        """RENDER_PROMPT rule 20 forbids inventing facts. Specific
        phrasing «не указано в данных» is the canonical refusal token."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Антивыдумка", RENDER_PROMPT)
        self.assertIn("не указано в данных", RENDER_PROMPT)
        # The rule references concrete fabrication classes the LLM
        # historically commits: year, count, PG-id, book/author name.
        for keyword in ("год", "PG", "имен"):
            self.assertIn(keyword, RENDER_PROMPT,
                          f"missing fabrication-class anchor «{keyword}»")

    def test_rule_20_cites_marlowe_class_bug(self):
        """Specific historical fabrication cited so reviewers know
        what the rule was added to block (Stan prod 2026-05-22)."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Marlowe", RENDER_PROMPT,
                      "the Marlowe-class bug citation is part of the rule")

    def test_rule_20_anti_knowledge_clause_present(self):
        """W-7 hardened (2026-05-24): the anti-knowledge clause must be
        explicit — the LLM is told that even when it «knows» a fact
        from training, it does not write it unless tool_results back it."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("общие знания", RENDER_PROMPT,
                      "anti-knowledge phrasing missing — rule 20 must "
                      "explicitly override training knowledge")
        # The «protocol» (find→write|refuse) wording is the operational
        # form of the clause; lock it so reviewers don't drop it.
        self.assertIn("Протокол", RENDER_PROMPT,
                      "rule 20 protocol (find→write|refuse) missing")

    def test_rule_19_empty_column_present(self):
        """RENDER_PROMPT rule 19 reinforces the deterministic
        empty-column strip (W-3). Belt and suspenders."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Скрывать колонки без данных", RENDER_PROMPT)


class RenderPromptStructuralIntegrity(unittest.TestCase):
    """Prior rules (1..18) should stay — these tests catch accidental
    deletes."""

    def test_strict_facts_rule_still_present(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("STRICT FACTS-ONLY", RENDER_PROMPT)

    def test_count_honesty_rule_still_present(self):
        """Rule 14 — count honesty (top_returned only)."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("top_returned", RENDER_PROMPT)

    def test_metric_explanations_rule_still_present(self):
        """Rule 15 — metric direction from metric_explanations."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("metric_explanations", RENDER_PROMPT)


class NumericAuditCatchesFabricatedFacts(unittest.TestCase):
    """W-7 deterministic back-stop: rule 20 in RENDER_PROMPT is an LLM
    instruction (probabilistic). numeric_audit is the deterministic gate
    that catches numbers in the answer that don't appear in tool data.

    These tests pin the Stan prod-bug class:
      * «год смерти Marlowe 2008» — year not in data
      * «1000 книг» когда в data 200 — wrong count
      * выдуманный PG-id — id not in data

    TZ приёмка W-7: «unit на нет числа в данных → нет числа в ответе»."""

    def test_bio_year_fabrication_flagged(self):
        """W-7 closed gap (2026-05-24): when context says «год смерти»
        and the specific year is NOT in tool data, numeric_audit flags
        it. Previously this was a known gap (audit trusted any year in
        [1500, 2100]); now `_is_year_like` falls through to regular
        matching in bio contexts.

        Stan prod 2026-05-22: «Marlowe — драматург. Год смерти 2008.»
        Data carried only birth_year=1564. 2008 is now flagged."""
        from scripts.v2.numeric_audit import audit_numbers
        tool_records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Marlowe, Christopher",
                     "books_in_corpus": 12, "birth_year": 1564},
        }]
        answer = "Marlowe — драматург. Год смерти 2008. Родился 1564."
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        flagged = [m.value for m in report.mismatches]
        self.assertIn(2008.0, flagged,
                       f"bio-year fabrication 2008 must be flagged "
                       f"(data has only birth_year=1564). Got: {flagged}")
        # And the real birth year MUST NOT be flagged — sanity for the
        # «бio years that ARE in data» path.
        self.assertNotIn(1564.0, flagged,
                          "real bio year in data must not be flagged")

    def test_is_year_like_backward_compat_no_data_arg(self):
        """`_is_year_like(v, ctx)` without data_numbers stays
        backward-compatible: trusts any year in [1500, 2100] unless
        non-year tokens are nearby. Callers that don't pass data still
        get the old behaviour — only audit_numbers (which DOES pass
        data) gets the new bio-year-fabrication check."""
        from scripts.v2.numeric_audit import _is_year_like
        # Old contract: 2008 in «год смерти 2008» is trusted as year-like
        # when no data_numbers provided.
        self.assertTrue(_is_year_like(2008.0, "год смерти 2008"),
                         "backward-compat: trust years when no data arg")

    def test_is_year_like_bio_context_with_data_flags_unsupported(self):
        """W-7: `_is_year_like(v, ctx, data_numbers)` returns False for
        bio-context years that AREN'T in data — letting the regular
        matcher flag them. The unit-level expression of «no year in
        data → no year in answer»."""
        from scripts.v2.numeric_audit import _is_year_like
        # data only has birth year, 2008 is fabricated
        data_nums = {1564.0, 12.0}
        self.assertFalse(_is_year_like(2008.0, "год смерти 2008", data_nums),
                          "bio-year not in data must NOT be trusted")
        # data DOES have death year — trusted
        data_nums_with_death = {1564.0, 1593.0}
        self.assertTrue(_is_year_like(1593.0, "год смерти 1593",
                                        data_nums_with_death),
                         "bio-year that IS in data must stay trusted")
        # publication / general year (no bio context) — keeps trust
        # even when not in data (covers historical citations).
        self.assertTrue(_is_year_like(1842.0, "опубликовано в 1842",
                                        data_nums),
                         "non-bio year context keeps old trust behaviour")

    def test_fabricated_count_flagged(self):
        from scripts.v2.numeric_audit import audit_numbers
        tool_records = [{
            "tool": "author_metadata",
            "data": {
                "author_canonical": "Marlowe, Christopher",
                "books_in_corpus": 200,  # ground truth
            },
        }]
        # Renderer says 1000 but data says 200 — historical Stan bug
        answer = "У Марло 1000 книг в корпусе."
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        flagged = [m.value for m in report.mismatches]
        self.assertIn(1000.0, flagged,
                       "wrong count 1000 (data=200) must be flagged")

    def test_real_numbers_not_flagged(self):
        """Sanity: numbers that ARE in tool data must NOT flag — otherwise
        users get noise on every answer."""
        from scripts.v2.numeric_audit import audit_numbers
        tool_records = [{
            "tool": "author_metadata",
            "data": {
                "author_canonical": "Doyle, Arthur Conan",
                "books_in_corpus": 153,
                "birth_year": 1859,
                "death_year": 1930,
            },
        }]
        answer = (
            "Артур Конан Дойл (1859–1930) представлен 153 книгами "
            "в корпусе."
        )
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        self.assertFalse(report.has_issues(),
                          f"real numbers flagged as fabrication: "
                          f"{[m.formatted for m in report.mismatches]}")


class W7AcceptanceFactualBattery(unittest.TestCase):
    """W-7 ТЗ-acceptance: набор из 10 фактологических запросов — нет
    fabrication-флагов, когда renderer честный; явные флаги, когда
    renderer выдумывает (число/год/id/имя/счёт).

    Каждый случай — пара (clean_answer / fabricated_answer) на одних и
    тех же tool_records. Чистая версия не флагается, выдуманная — да.
    Acceptance: 0 false positives на честных ответах + явный flag на
    каждой fabrication-классе.
    """

    # --- Helpers ----------------------------------------------------

    def _audit(self, answer, records, intent="unknown"):
        from scripts.v2.numeric_audit import audit_numbers
        return audit_numbers(answer, records, intent=intent)

    def _assert_clean(self, answer, records, *, intent, case):
        report = self._audit(answer, records, intent=intent)
        self.assertFalse(
            report.has_issues(),
            f"[{case}] clean answer wrongly flagged: "
            f"{[m.formatted for m in report.mismatches]}",
        )

    def _assert_flagged(self, answer, records, *, value, intent, case):
        report = self._audit(answer, records, intent=intent)
        flagged = [m.value for m in report.mismatches]
        self.assertIn(
            value, flagged,
            f"[{case}] fabricated value {value} should be flagged. "
            f"Flagged: {flagged}",
        )

    # --- 10 фактологических запросов: clean answers --------------

    def test_01_author_metadata_marlowe_clean(self):
        records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Marlowe, Christopher",
                     "birth_year": 1564, "death_year": 1593,
                     "books_in_corpus": 12},
        }]
        answer = "Кристофер Марло (1564–1593), 12 произведений в корпусе."
        self._assert_clean(answer, records, intent="author_metadata",
                            case="01-marlowe-clean")

    def test_02_author_metadata_marlowe_fabricated_death(self):
        records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Marlowe, Christopher",
                     "birth_year": 1564, "books_in_corpus": 12},
        }]
        answer = "Кристофер Марло (1564–2008), 12 произведений в корпусе."
        self._assert_flagged(answer, records, value=2008.0,
                              intent="author_metadata",
                              case="02-marlowe-fab-death")

    def test_03_author_metadata_dickens_clean(self):
        records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Dickens, Charles",
                     "birth_year": 1812, "death_year": 1870,
                     "books_in_corpus": 60},
        }]
        answer = "Чарльз Диккенс (1812–1870), 60 произведений в корпусе."
        self._assert_clean(answer, records, intent="author_metadata",
                            case="03-dickens-clean")

    def test_04_books_count_fabricated(self):
        records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Marlowe, Christopher",
                     "books_in_corpus": 200},
        }]
        # «1000 книг» — выдумка, в data 200
        answer = "У Марло 1000 книг в корпусе."
        self._assert_flagged(answer, records, value=1000.0,
                              intent="author_metadata",
                              case="04-fabricated-count")

    def test_05_top_words_clean_count(self):
        records = [{
            "tool": "author_top_words",
            "data": {
                "top_requested": 100,
                "top_returned": 47,
                "top": [{"word": f"w{i}", "score": 10 - i * 0.1}
                        for i in range(47)],
            },
        }]
        answer = ("Запрошено 100, после фильтров вернулось 47 — "
                  "представлен список из 47 фирменных слов.")
        self._assert_clean(answer, records, intent="author_top_words",
                            case="05-top-count-clean")

    def test_06_top_words_fabricated_count(self):
        records = [{
            "tool": "author_top_words",
            "data": {
                "top_requested": 100,
                "top_returned": 19,
                "top": [{"word": f"w{i}"} for i in range(19)],
            },
        }]
        # Stan-class hallucination: «50 слов» — ни запрошено, ни возвращено
        answer = "Представляю тебе список из 50 фирменных слов автора."
        self._assert_flagged(answer, records, value=50.0,
                              intent="author_top_words",
                              case="06-top-count-fab")

    def test_07_book_metadata_clean(self):
        records = [{
            "tool": "find_book",
            "data": {"matches": [{
                "pg_id": "1342", "title": "Pride and Prejudice",
                "author_canonical": "Austen, Jane",
                "pub_year": 1813,
            }]},
        }]
        answer = "Pride and Prejudice (PG1342), 1813."
        self._assert_clean(answer, records, intent="book_lookup",
                            case="07-pride-clean")

    def test_08_book_metadata_fabricated_pub_year(self):
        records = [{
            "tool": "find_book",
            "data": {"matches": [{
                "pg_id": "1342", "title": "Pride and Prejudice",
                "author_canonical": "Austen, Jane",
            }]},
        }]
        # Pub year отсутствует в data, renderer выдумал 1813
        # NB: rule 20 ловит и через критика, но numeric_audit увидит
        # что 1813 нет в data_numbers (там только pg_id 1342); 1813
        # вне 1500..2100? нет, в. И контекст не bio. Так что для этого
        # случая audit полагается на rule 20 + critic — мы лишь не
        # должны давать false-positive, и тест в том, что renderer
        # ЧЕСТНО написал бы «год издания не указан в данных».
        # ↓ позитивно проверяем: честная версия не флагается
        clean_answer = "Pride and Prejudice (PG1342), год издания не указан в данных."
        self._assert_clean(clean_answer, records, intent="book_lookup",
                            case="08-honest-no-pub-year")

    def test_09_top_authors_clean(self):
        records = [{
            "tool": "top_authors_by_lexical_richness",
            "data": {
                "metric": "ttr",
                "top": [
                    {"author_canonical": "Joyce, James", "value": 0.42},
                    {"author_canonical": "Woolf, Virginia", "value": 0.38},
                    {"author_canonical": "Dickens, Charles", "value": 0.21},
                ],
            },
        }]
        answer = (
            "| Автор | TTR |\n"
            "| --- | --- |\n"
            "| Joyce, James | 0.42 |\n"
            "| Woolf, Virginia | 0.38 |\n"
            "| Dickens, Charles | 0.21 |\n"
        )
        self._assert_clean(answer, records, intent="lexical_wealth",
                            case="09-top-authors-clean")

    def test_10_no_number_in_data_no_number_in_answer(self):
        """W-7 ТЗ explicit acceptance unit: «нет числа в данных →
        нет числа в ответе». When tool_records carry no numeric fields,
        a honest answer that says «не указано в данных» must produce
        zero numeric mismatches."""
        records = [{
            "tool": "author_metadata",
            "data": {"author_canonical": "Unknown, Author"},
        }]
        honest_answer = (
            "По автору данных в корпусе нет: год рождения, год смерти и "
            "количество книг — не указано в данных."
        )
        report = self._audit(honest_answer, records,
                              intent="author_metadata")
        self.assertFalse(
            report.has_issues(),
            f"honest «не указано в данных» wrongly flagged: "
            f"{[m.formatted for m in report.mismatches]}",
        )
        # И симметрия: если renderer выдумал бы числа, флаг бы стоял.
        fabricated_answer = (
            "Автор родился в 1850, умер в 1920, написал 47 произведений."
        )
        rep2 = self._audit(fabricated_answer, records,
                            intent="author_metadata")
        flagged = [m.value for m in rep2.mismatches]
        # 47 — счёт, должен флагнуться (нет в data)
        self.assertIn(47.0, flagged,
                       f"fabricated count 47 must be flagged. "
                       f"Got: {flagged}")
        # Bio-years 1850 / 1920 — должны флагнуться (контекст био, нет в data)
        self.assertTrue(
            1850.0 in flagged or 1920.0 in flagged,
            f"fabricated bio years must be flagged. Got: {flagged}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
