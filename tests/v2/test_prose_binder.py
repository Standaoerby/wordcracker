"""ProseBinder — Phase 3 step B tests.

Architectural invariant: prose audit MUST reject any number or proper
noun in prose that isn't in skeleton+payload. This is the only thing
that lets us add a narrow LLM step on top of the deterministic skeleton
without re-introducing fabrication.

Coverage:
  - verify_prose number audit (positive + negative)
  - verify_prose entity audit (positive + negative)
  - bind_prose with mocked LLM (success / fail / verification drop)
  - language detection
  - JSON parse tolerance (markdown fence stripping)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import prose_binder as pb
from scripts.v2 import view_builders as vb
from scripts.v2.view_types import EmptyReason, ViewType


# =====================================================================
# Number / entity audit
# =====================================================================


class NumberAudit(unittest.TestCase):
    """All numbers in prose must appear in skeleton or payload."""

    def test_prose_number_in_skeleton_passes(self):
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2", word_count=119000,
        )
        skeleton = "Flesch 58.8 | FK 10.9 | CEFR B2 | 119000 words"
        prose = "Pride and Prejudice has Flesch 58.8 and 119000 words."
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        self.assertEqual(failures, [], f"expected pass, got {failures}")

    def test_prose_fabricated_number_fails(self):
        """Prose claims 1820 (publication year) but it's nowhere in
        skeleton/payload — must fail audit. This closes a class of
        R14 fabrications where renderer adds plausible dates."""
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2", word_count=119000,
        )
        skeleton = "Flesch 58.8 | FK 10.9 | CEFR B2"
        prose = "Pride and Prejudice was published in 1813 with Flesch 58.8."
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        self.assertTrue(any("1813" in f for f in failures),
                        f"1813 should be flagged; got {failures}")

    def test_small_number_tolerance(self):
        """Numbers ≤20 (rank labels, «top-3», «2 примера») tolerated
        even if not literally in payload — they're natural-language
        tallies, not factual claims."""
        view = vb.build_top_n_table(
            rows=[{"rank": 1, "w": "x"}], columns=["rank", "w"], requested_n=1,
        )
        skeleton = "| rank | w |\n| 1 | x |"
        prose = "Вот top-1 результат — слово, которое встретилось 3 раза."
        # Tolerates 3, 1 (both ≤20)
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        self.assertEqual(failures, [])


class EntityAudit(unittest.TestCase):
    """Capitalized words in prose must appear in skeleton or payload."""

    def test_prose_entity_in_payload_passes(self):
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        skeleton = "Pride and Prejudice (PG1342)\nFlesch 58.8"
        prose = "Pride and Prejudice is at B2 difficulty."
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        self.assertEqual(failures, [])

    def test_prose_fabricated_author_fails(self):
        """Prose mentions Bronte but only Austen is in payload."""
        view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2",
        )
        # payload contains "Pride and Prejudice" but no author
        skeleton = "Pride and Prejudice (PG1342)\nFlesch 58.8 | B2"
        prose = "Similar to Bronte's Jane Eyre at this level."
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        self.assertTrue(any("Bronte" in f for f in failures),
                        f"Bronte should be flagged; got {failures}")

    def test_common_starters_ignored(self):
        view = vb.build_top_n_table(
            rows=[{"x": 1}], columns=["x"], requested_n=1,
        )
        skeleton = "| x |\n| 1 |"
        prose = "Вот результат. Можно посмотреть на другой scope."
        failures = pb.verify_prose(prose=prose, view=view, skeleton=skeleton)
        # «Вот», «Можно» — in ignore set
        self.assertEqual(failures, [])


# =====================================================================
# JSON parsing tolerance
# =====================================================================


class JsonParse(unittest.TestCase):
    def test_plain_json(self):
        d = pb._parse_json_strict('{"intro":"hi","next_steps":[]}')
        self.assertEqual(d, {"intro": "hi", "next_steps": []})

    def test_markdown_fence_json(self):
        d = pb._parse_json_strict('```json\n{"intro":"hi"}\n```')
        self.assertEqual(d, {"intro": "hi"})

    def test_markdown_fence_no_lang(self):
        d = pb._parse_json_strict('```\n{"intro":"hi"}\n```')
        self.assertEqual(d, {"intro": "hi"})

    def test_garbage_returns_empty(self):
        d = pb._parse_json_strict("not json at all")
        self.assertEqual(d, {})

    def test_embedded_json(self):
        """LLM sometimes adds text around the JSON — tolerate it."""
        d = pb._parse_json_strict('Here is the answer: {"intro":"x"}')
        self.assertEqual(d, {"intro": "x"})


# =====================================================================
# Language detection
# =====================================================================


class LanguageDetect(unittest.TestCase):
    def test_russian_detected(self):
        self.assertEqual(pb._detect_language("какие книги у Доyle"), "ru")

    def test_english_detected(self):
        self.assertEqual(pb._detect_language("what books by Doyle"), "en")

    def test_empty_default_ru(self):
        self.assertEqual(pb._detect_language(""), "ru")


# =====================================================================
# bind_prose end-to-end with mocked LLM
# =====================================================================


def _make_llm(return_value: str):
    """Mock LLM callable returning a fixed string."""
    def _fn(system_prompt: str, user_prompt: str, /,
             *, timeout_s: float = 8.0) -> str:
        return return_value
    return _fn


class BindProse(unittest.TestCase):
    def setUp(self):
        self.view = vb.build_readability_summary(
            book_title="Pride and Prejudice", pg_id="PG1342",
            flesch=58.8, flesch_kincaid=10.9, cefr="B2", word_count=119000,
        )
        from scripts.v2 import template_executor as te
        self.skeleton = te.render_view(self.view)

    def test_passes_when_prose_clean(self):
        """LLM returns prose that only references payload values."""
        llm = _make_llm(json.dumps({
            "intro": "Pride and Prejudice on B2 уровне.",
            "next_steps": ["Сравни с другой книгой Austen?"],
        }, ensure_ascii=False))
        # Note: Austen not in payload — entity audit would fail. Use payload-only words.
        llm = _make_llm(json.dumps({
            "intro": "Pride and Prejudice on B2 level — Flesch 58.8.",
            "next_steps": ["Compare with another book?"],
        }))
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="how hard is Pride and Prejudice?",
                          llm_call=llm)
        self.assertTrue(r.used_llm)
        self.assertTrue(r.verification_passed,
                        f"audit failed: {r.verification_failures}")
        self.assertIsNotNone(r.intro)

    def test_drops_prose_when_fabricated_author(self):
        """LLM hallucinates «Brontë» — audit must reject."""
        llm = _make_llm(json.dumps({
            "intro": "Pride and Prejudice similar to Bronte's work.",
            "next_steps": [],
        }))
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="how hard is Pride and Prejudice?",
                          llm_call=llm)
        self.assertTrue(r.used_llm)
        self.assertFalse(r.verification_passed)
        self.assertIsNone(r.intro)
        self.assertTrue(any("Bronte" in f for f in r.verification_failures))

    def test_drops_prose_when_fabricated_number(self):
        llm = _make_llm(json.dumps({
            "intro": "Pride and Prejudice was published in 1813.",
            "next_steps": [],
        }))
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="how hard is Pride and Prejudice?",
                          llm_call=llm)
        self.assertFalse(r.verification_passed)
        self.assertTrue(any("1813" in f for f in r.verification_failures))

    def test_empty_llm_response_drops_silently(self):
        llm = _make_llm("")
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="x", llm_call=llm)
        self.assertTrue(r.used_llm)
        self.assertIsNone(r.intro)
        self.assertIn("llm_returned_empty", r.verification_failures)

    def test_disabled_returns_empty(self):
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="x", enable_llm=False)
        self.assertFalse(r.used_llm)
        self.assertIsNone(r.intro)

    def test_llm_exception_drops_gracefully(self):
        def _raising(system_prompt, user_prompt, /, *, timeout_s=8.0):
            raise RuntimeError("Ollama died")
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="x", llm_call=_raising)
        self.assertFalse(r.used_llm)    # exception means call never returned
        self.assertTrue(any("llm_exception" in f
                            for f in r.verification_failures))

    def test_intro_length_capped(self):
        """Intro >350 chars gets truncated with ellipsis."""
        long = "ab " * 200    # 600 chars of 'ab '
        llm = _make_llm(json.dumps({"intro": long, "next_steps": []}))
        r = pb.bind_prose(view=self.view, skeleton=self.skeleton,
                          question="x", llm_call=llm)
        if r.verification_passed and r.intro:
            self.assertLessEqual(len(r.intro), 351)
            self.assertTrue(r.intro.endswith("…"))


class ProseToMarkdown(unittest.TestCase):
    def test_combines_intro_and_steps(self):
        r = pb.ProseResult(
            intro="Intro line.",
            next_steps=["Question 1?", "Question 2?"],
        )
        md = r.to_markdown()
        self.assertIn("Intro line.", md)
        self.assertIn("Question 1?", md)
        self.assertIn("Что ещё", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
