"""Sprint 16 Phase E — meta-query intent tests.

Verifies the new author_lookup / book_extremum / corpus_extremum
intents are classified correctly AND that their plan builders route to
the right tools."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class AuthorLookupIntent(unittest.TestCase):
    """«Какие книги у X» should classify as author_lookup, not
    author_metadata (which is for «когда родился») nor top_authors_books
    (which is for «топ авторов»)."""

    def test_russian_kakie_knigi_u_X(self):
        m = int_mod.classify("Какие книги у Doyle?")
        self.assertEqual(m.label, "author_lookup")

    def test_russian_perechisli_proizvedenija(self):
        m = int_mod.classify("Перечисли произведения у Doyle")
        self.assertEqual(m.label, "author_lookup")

    def test_russian_pokazhi_spisok(self):
        m = int_mod.classify("Покажи список книг у Wodehouse")
        self.assertEqual(m.label, "author_lookup")

    def test_genitive_only_now_routes_author_lookup(self):
        """S-R5 E1b (2026-05-31) — REVERSED. «Перечисли произведения Doyle»
        (genitive surname, no «у») now classifies to author_lookup via the
        bare-genitive rule guarded by the (?-i:[A-ZА-ЯЁ][a-zа-яё]) proper-noun
        check. Previously fell to clarify → v4 LLM-planner flake. The guard
        keeps Q30 «произведения подойдут…» out of author_lookup
        (see test_intent.py Q30 + E1bAuthorLookupPhrasing below)."""
        m = int_mod.classify("Перечисли произведения Doyle")
        self.assertEqual(m.label, "author_lookup")

    def test_english_what_books_does(self):
        m = int_mod.classify("What books does Doyle have")
        self.assertEqual(m.label, "author_lookup")

    def test_doesnt_steal_skolko_knig(self):
        """«сколько у Doyle книг» should still be author_metadata
        (book count), not author_lookup (list)."""
        m = int_mod.classify("сколько у Doyle книг")
        self.assertEqual(m.label, "author_metadata")


class BookExtremumIntent(unittest.TestCase):

    def test_samaya_dlinnaya_kniga(self):
        m = int_mod.classify("Самая длинная книга в корпусе?")
        self.assertEqual(m.label, "book_extremum")

    def test_samaya_populyarnaya_kniga(self):
        m = int_mod.classify("Какая самая популярная книга?")
        self.assertEqual(m.label, "book_extremum")

    def test_english_longest_book(self):
        m = int_mod.classify("What is the longest book in the corpus?")
        self.assertEqual(m.label, "book_extremum")

    def test_english_most_downloaded(self):
        m = int_mod.classify("the most downloaded book")
        self.assertEqual(m.label, "book_extremum")

    def test_top_10_books_still_top_authors_books(self):
        """Plural «топ-10 книг» should NOT trigger book_extremum
        (singular). Goes to top_authors_books, whose BUILDER then
        disambiguates books-vs-authors by raw text — see
        TopAuthorsBooksDisambiguation below."""
        m = int_mod.classify("топ-10 самых популярных книг")
        self.assertEqual(m.label, "top_authors_books")


class CorpusExtremumIntent(unittest.TestCase):

    def test_samyj_plodovityj_avtor(self):
        m = int_mod.classify("Кто самый плодовитый автор в корпусе?")
        self.assertEqual(m.label, "corpus_extremum")

    def test_samyj_populyarnyj_avtor(self):
        m = int_mod.classify("Самый популярный автор?")
        self.assertEqual(m.label, "corpus_extremum")

    def test_english_most_prolific(self):
        m = int_mod.classify("Who is the most prolific author?")
        self.assertEqual(m.label, "corpus_extremum")

    def test_top_5_authors_still_top_authors_books(self):
        """Plural «топ-5 авторов» → top_authors_books, not corpus_extremum."""
        m = int_mod.classify("топ-5 авторов по числу книг")
        self.assertEqual(m.label, "top_authors_books")


class AuthorLookupPlan(unittest.TestCase):

    def test_routes_to_author_metadata_when_author_present(self):
        e = Entities(author_regex="^Doyle,", author_label="Doyle")
        p = plan_mod.build("author_lookup", e)
        self.assertEqual(p.intent, "author_lookup")
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.steps[0].tool, "author_metadata")
        self.assertEqual(p.steps[0].args["author_regex"], "^Doyle,")

    def test_clarifies_when_no_author(self):
        e = Entities()
        p = plan_mod.build("author_lookup", e)
        self.assertTrue(p.needs_clarify)


class TopAuthorsBooksDisambiguation(unittest.TestCase):
    """Books-vs-authors disambiguation inside `_plan_top_authors`
    (2026-06-11, Stan). The top_authors_books label deliberately covers
    both «топ авторов» and plural «самые популярные книги» (so the
    plural doesn't leak into singular book_extremum), but the builder
    used to answer EVERY query with top_authors_by — a table of AUTHORS
    for a question about BOOKS. Now a query that talks about books and
    never mentions an author delegates to _plan_book_recommendation →
    top_books_by_downloads.

    R2 negative tests: the books-route asserts fail on pre-fix code
    (it returned top_authors_by); the authors-route asserts guard
    against the fix over-stealing the legacy paths.
    """

    @staticmethod
    def _plan(q: str):
        m = int_mod.classify(q)
        # All phrasings here must stay on the shared label — the
        # disambiguation is the builder's job, not the classifier's.
        assert m.label == "top_authors_books", (q, m.label)
        return plan_mod.build(m.label, ent_mod.extract(q))

    # ---- books phrasings → top_books_by_downloads (failed pre-fix) ----

    def test_samye_populyarnye_knigi_routes_to_books(self):
        """The bug query: «самые популярные книги» used to return
        top_authors_by(metric=downloads) — top AUTHORS for a question
        about BOOKS."""
        p = self._plan("самые популярные книги")
        self.assertEqual([s.tool for s in p.steps],
                         ["top_books_by_downloads"])

    def test_top_10_knig_routes_to_books_with_top_n(self):
        p = self._plan("топ-10 самых популярных книг")
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")
        self.assertEqual(p.steps[0].args["top"], 10)

    def test_english_most_downloaded_books(self):
        p = self._plan("most downloaded books")
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")

    def test_country_books_routes_to_books_with_disclosure(self):
        """«самые популярные британские книги» → books list; the country
        filter top_books_by_downloads can't honor is DISCLOSED (W-9
        invariant via the _plan_book_recommendation delegate), not
        silently swapped for a table of authors."""
        p = self._plan("самые популярные британские книги")
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")
        self.assertTrue(any("стран" in n.lower()
                            for n in (p.render_notes or [])),
                        p.render_notes)

    # ---- authors phrasings stay on top_authors_by* (negative guards) ----

    def test_top_5_avtorov_po_chislu_knig_stays_authors(self):
        """Mentions both «авторов» and «книг» — author word wins."""
        p = self._plan("топ-5 авторов по числу книг")
        self.assertEqual(p.steps[0].tool, "top_authors_by")
        self.assertEqual(p.steps[0].args["metric"], "books")
        self.assertEqual(p.steps[0].args["top"], 5)

    def test_metric_phrase_knig_stays_authors(self):
        """«топ-10 по числу книг» — «книг» appears ONLY inside the
        metric phrase (rank authors BY book count), so it must NOT
        flip the plan to the books route."""
        p = self._plan("топ-10 по числу книг")
        self.assertEqual(p.steps[0].tool, "top_authors_by")
        self.assertEqual(p.steps[0].args["metric"], "books")

    def test_populyarnye_avtory_stays_authors(self):
        p = self._plan("самые популярные авторы")
        self.assertEqual(p.steps[0].tool, "top_authors_by")
        self.assertEqual(p.steps[0].args["metric"], "downloads")

    def test_country_authors_stays_country_path(self):
        p = self._plan("топ-5 британских авторов по скачиваниям")
        self.assertEqual(p.steps[0].tool, "top_authors_by_country")
        self.assertEqual(p.steps[0].args["country"], "GB")


class CorpusExtremumPlan(unittest.TestCase):

    def test_plodovityj_routes_to_books_metric(self):
        e = Entities(raw_misc={"raw_text": "Кто самый плодовитый автор?"})
        p = plan_mod.build("corpus_extremum", e)
        self.assertEqual(p.steps[0].tool, "top_authors_by")
        self.assertEqual(p.steps[0].args["metric"], "books")
        self.assertEqual(p.steps[0].args["top"], 1)

    def test_populyarnyj_routes_to_downloads_metric(self):
        e = Entities(raw_misc={"raw_text": "Самый популярный автор?"})
        p = plan_mod.build("corpus_extremum", e)
        self.assertEqual(p.steps[0].tool, "top_authors_by")
        self.assertEqual(p.steps[0].args["metric"], "downloads")

    def test_chitaemyj_routes_to_downloads(self):
        e = Entities(raw_misc={"raw_text": "Самый читаемый автор?"})
        p = plan_mod.build("corpus_extremum", e)
        self.assertEqual(p.steps[0].args["metric"], "downloads")


class BookExtremumPlan(unittest.TestCase):

    def test_populyarnaya_routes_to_top_books_by_downloads(self):
        e = Entities(raw_misc={"raw_text": "Самая популярная книга?"})
        p = plan_mod.build("book_extremum", e)
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")
        self.assertEqual(p.steps[0].args["top"], 1)

    def test_dlinnaya_routes_to_clarify(self):
        """No «longest book» tool yet — must clarify with helpful menu."""
        e = Entities(raw_misc={"raw_text": "Самая длинная книга?"})
        p = plan_mod.build("book_extremum", e)
        self.assertTrue(p.needs_clarify)
        # Helpful menu should mention book_readability or book_compare
        self.assertIn("book_compare", (p.clarify_question or "")
                       + (p.clarify_question or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
