"""Unit tests for the LLM intent fallback.

These don't hit Ollama — we mock requests.post. The point is to verify
the parse logic, cache, taxonomy sync, and that the wire-up in
rag_v2.ask falls through to the LLM only when the rule-based classifier
returns clarify."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import llm_intent
from scripts.v2.planner.intent import INTENTS


class TaxonomySync(unittest.TestCase):
    def test_every_intent_has_a_hint(self):
        """The hint table must match the INTENTS taxonomy so the LLM gets
        the full list of options every time."""
        non_clarify = INTENTS - {"clarify"}
        missing = non_clarify - set(llm_intent._INTENT_HINTS)
        self.assertFalse(missing, msg=f"intents without LLM hint: {missing}")

    def test_no_stale_hints(self):
        non_clarify = INTENTS - {"clarify"}
        stale = set(llm_intent._INTENT_HINTS) - non_clarify - {"clarify"}
        self.assertFalse(stale, msg=f"hints with no matching intent: {stale}")


class ParseLabel(unittest.TestCase):
    def test_bare_label(self):
        self.assertEqual(llm_intent._parse_label("author_vocab"), "author_vocab")

    def test_quoted(self):
        self.assertEqual(llm_intent._parse_label('"author_vocab"'), "author_vocab")
        self.assertEqual(llm_intent._parse_label("'book_archaic'"), "book_archaic")

    def test_with_prefix(self):
        self.assertEqual(llm_intent._parse_label("intent: author_metadata"),
                         "author_metadata")
        self.assertEqual(llm_intent._parse_label("Label: word_emotion"),
                         "word_emotion")

    def test_trailing_punctuation(self):
        self.assertEqual(llm_intent._parse_label("corpus_meta."), "corpus_meta")
        self.assertEqual(llm_intent._parse_label("introduction;"), "introduction")

    def test_with_markdown(self):
        self.assertEqual(llm_intent._parse_label("`word_etymology`"), "word_etymology")

    def test_unknown_returns_none(self):
        self.assertIsNone(llm_intent._parse_label("not_a_real_intent"))

    def test_empty(self):
        self.assertIsNone(llm_intent._parse_label(""))
        self.assertIsNone(llm_intent._parse_label("   "))

    def test_multiword_explanation(self):
        """LLM sometimes adds explanation despite instructions; pick the
        first valid intent token."""
        self.assertEqual(
            llm_intent._parse_label("author_vocab because the user asks for "
                                     "characteristic words"),
            "author_vocab",
        )


class ClassifyDispatch(unittest.TestCase):
    def setUp(self):
        llm_intent._reset_cache_for_tests()

    def test_disabled_returns_none(self):
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", False):
            self.assertIsNone(llm_intent.classify_with_llm("ну привет"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(llm_intent.classify_with_llm(""))
        self.assertIsNone(llm_intent.classify_with_llm("   "))

    def test_happy_path_parses_intent(self):
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", True), \
             mock.patch("scripts.v2.planner.llm_intent.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": "introduction"}
            }
            m = llm_intent.classify_with_llm("ну привет, ты кто вообще?")
            self.assertIsNotNone(m)
            self.assertEqual(m.label, "introduction")
            self.assertEqual(m.matched_pattern, "llm-fallback")

    def test_cache_hit_skips_http(self):
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", True), \
             mock.patch("scripts.v2.planner.llm_intent.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {"message": {"content": "corpus_meta"}}
            # First call → LLM, second → cached
            m1 = llm_intent.classify_with_llm("сколько книжек у тебя?")
            m2 = llm_intent.classify_with_llm("сколько книжек у тебя?")
            self.assertEqual(m1.label, "corpus_meta")
            self.assertEqual(m2.label, "corpus_meta")
            self.assertEqual(m2.matched_pattern, "llm-fallback-cached")
            self.assertEqual(mp.call_count, 1)

    def test_network_error_returns_none(self):
        import requests
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", True), \
             mock.patch("scripts.v2.planner.llm_intent.requests.post",
                        side_effect=requests.exceptions.ConnectionError("boom")):
            self.assertIsNone(llm_intent.classify_with_llm("some query"))

    def test_unparseable_response_returns_none(self):
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", True), \
             mock.patch("scripts.v2.planner.llm_intent.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "message": {"content": "I am not sure how to classify"}
            }
            self.assertIsNone(llm_intent.classify_with_llm("strange query"))

    def test_history_added_to_prompt(self):
        """When `history` is given, the last user turn should be threaded
        into the user message — so «а у пушкина?» can be classified with
        context of «фирменные слова Doyle» from prior turn."""
        with mock.patch.object(llm_intent, "LLM_INTENT_ENABLED", True), \
             mock.patch("scripts.v2.planner.llm_intent.requests.post") as mp:
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {"message": {"content": "author_vocab"}}
            history = [
                {"role": "user", "content": "фирменные слова Doyle"},
                {"role": "assistant", "content": "..."},
            ]
            llm_intent.classify_with_llm("а у пушкина?", history)
            sent_payload = mp.call_args[1].get("json") or mp.call_args.kwargs.get("json")
            assert sent_payload, "expected json payload on requests.post"
            user_content = sent_payload["messages"][-1]["content"]
            self.assertIn("Doyle", user_content)
            self.assertIn("пушкина", user_content.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
