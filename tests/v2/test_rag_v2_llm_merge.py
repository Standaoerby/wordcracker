"""Unit tests for the Sprint 14 entity-merge helpers in rag_v2.

These don't hit Ollama or any tools — they verify the merge logic that
takes LLM-suggested entities and fills in gaps left by the regex
extractor."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import Entities
from scripts.v2.rag_v2 import (
    _merge_llm_entities, _needs_entity_help,
    _surname_to_regex, _title_to_book,
)


class SurnameToRegex(unittest.TestCase):
    def test_known_authors(self):
        self.assertEqual(_surname_to_regex("Doyle"), "^Doyle,")
        self.assertEqual(_surname_to_regex("Tolstoy"), "^Tolstoy,")
        self.assertEqual(_surname_to_regex("doyle"), "^Doyle,")  # case-insensitive

    def test_unknown_returns_none(self):
        self.assertIsNone(_surname_to_regex("UnknownAuthor"))
        self.assertIsNone(_surname_to_regex(""))


class TitleToBook(unittest.TestCase):
    def test_known_title(self):
        pg, canon = _title_to_book("Pride and Prejudice")
        self.assertEqual(pg, "PG1342")
        self.assertEqual(canon, "Pride and Prejudice")

    def test_leading_the_fuzzy(self):
        pg, canon = _title_to_book("Old Man and the Sea")
        # Should match KNOWN_BOOKS "the old man and the sea" via fuzzy
        self.assertEqual(canon, "The Old Man and the Sea")

    def test_copyright_book_returns_canonical_no_pg(self):
        pg, canon = _title_to_book("The Lord of the Rings")
        self.assertIsNone(pg)
        self.assertEqual(canon, "The Lord of the Rings")

    def test_unknown_title_passes_through(self):
        pg, canon = _title_to_book("Some Random Title")
        self.assertIsNone(pg)
        self.assertEqual(canon, "Some Random Title")


class NeedsEntityHelp(unittest.TestCase):
    def test_author_vocab_missing_author(self):
        e = Entities()
        self.assertTrue(_needs_entity_help("author_vocab", e))

    def test_author_vocab_with_author(self):
        e = Entities(author_regex="^Doyle,")
        self.assertFalse(_needs_entity_help("author_vocab", e))

    def test_book_archaic_missing_book(self):
        e = Entities()
        self.assertTrue(_needs_entity_help("book_archaic", e))

    def test_book_archaic_with_title(self):
        e = Entities(book_title="Dracula")
        self.assertFalse(_needs_entity_help("book_archaic", e))

    def test_word_etymology_with_etymology_family(self):
        # Either word or etymology_family unblocks
        e = Entities(etymology_family="germanic")
        self.assertFalse(_needs_entity_help("word_etymology", e))

    def test_unknown_intent_no_help(self):
        e = Entities()
        self.assertFalse(_needs_entity_help("introduction", e))
        self.assertFalse(_needs_entity_help("corpus_meta", e))

    def test_lexical_wealth_no_entity_required(self):
        e = Entities()
        self.assertFalse(_needs_entity_help("lexical_wealth", e))


class MergeLLMEntities(unittest.TestCase):
    def test_fills_missing_author(self):
        e = Entities()
        _merge_llm_entities(e, {"author": "Shakespeare"})
        self.assertEqual(e.author_regex, "^Shakespeare,")
        self.assertEqual(e.author_label, "Shakespeare")

    def test_does_not_override_regex_author(self):
        """Regex already found an author — LLM doesn't override."""
        e = Entities(author_regex="^Doyle,", author_label="Conan Doyle")
        _merge_llm_entities(e, {"author": "Twain"})
        self.assertEqual(e.author_regex, "^Doyle,")

    def test_fills_book_from_title(self):
        e = Entities()
        _merge_llm_entities(e, {"book_title": "Dracula"})
        self.assertEqual(e.book_id, "PG345")
        self.assertEqual(e.book_title, "Dracula")

    def test_fills_word(self):
        e = Entities()
        _merge_llm_entities(e, {"word": "Fog"})
        self.assertEqual(e.word, "fog")  # lowercased

    def test_rejects_too_long_word(self):
        e = Entities()
        _merge_llm_entities(e, {"word": "x" * 50})
        self.assertIsNone(e.word)

    def test_fills_year_range(self):
        e = Entities()
        _merge_llm_entities(e, {"year_from": 1837, "year_to": 1901})
        self.assertEqual(e.year_from, 1837)
        self.assertEqual(e.year_to, 1901)

    def test_rejects_out_of_range_year(self):
        e = Entities()
        _merge_llm_entities(e, {"year_from": 100, "year_to": 3000})
        self.assertIsNone(e.year_from)
        self.assertIsNone(e.year_to)

    def test_fills_country(self):
        e = Entities()
        _merge_llm_entities(e, {"country": "gb"})
        self.assertEqual(e.country, "GB")

    def test_unknown_author_no_op(self):
        e = Entities()
        _merge_llm_entities(e, {"author": "NotARealAuthor"})
        self.assertIsNone(e.author_regex)

    def test_empty_llm_dict_no_changes(self):
        e = Entities(author_regex="^Doyle,")
        _merge_llm_entities(e, {})
        self.assertEqual(e.author_regex, "^Doyle,")

    def test_none_input_safe(self):
        e = Entities()
        _merge_llm_entities(e, None)  # type: ignore
        self.assertIsNone(e.author_regex)


if __name__ == "__main__":
    unittest.main(verbosity=2)
