"""S-R5 / E9 — bare «примеры <word> у <author>» must route to word_contexts,
not clarify.

ROOT CAUSE (probe E9, deploy 408874e — «примеры heart у Дойла»):
The intent classifier had NO rule for the bare «примеры <english-word>»
form. The word_contexts rules required either a leading «приведи» or the
Russian filler «использования» («примеры использов\\w*»), so the bare
query matched ZERO rules and `classify()` fell through to its
`IntentMatch("clarify", 0.0)` default.

The asymmetry is the bug: the entity extractor already grew
`_BARE_WORD_AFTER_EXAMPLES` (entities.py, Sprint 17) to lift the Latin
token out of exactly this form — but the matching INTENT rule was never
added. So word=heart was extractable, yet the engine still bounced to
clarify because the intent never reached word_contexts.

NEGATIVE TEST (R5): each query below produced intent=clarify on the
pre-fix classifier and now routes to word_contexts. The false-positive
guard tests lock in that Russian genitive fillers («примеры авторов /
слов») do NOT bind the new rule — they correctly stay out of
word_contexts (the form has no studyable word).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import intent as I
from scripts.v2.planner import entities as E


class BareExamplesRoutesToWordContexts(unittest.TestCase):
    """The exact probe query + close variants must reach word_contexts."""

    # The literal probe E9 query is first — this is the regression that
    # FAILED pre-fix (classify -> clarify 0.0).
    POSITIVE = [
        "примеры heart у Дойла",          # <- probe E9 verbatim
        "примеры ajar у Дойла",           # Sprint-17 entities.py comment case
        "примеры fog у Диккенса",
        "examples of heart in Doyle",     # English «examples of <word>»
        "examples gloom in Poe",
    ]

    def test_probe_e9_and_variants_route_to_word_contexts(self):
        for q in self.POSITIVE:
            with self.subTest(q=q):
                self.assertEqual(
                    I.classify(q).label, "word_contexts",
                    f"{q!r} must classify as word_contexts (was clarify pre-fix)",
                )

    def test_word_is_actually_extractable(self):
        """Routing to word_contexts is only useful if the word entity is
        present — _plan_word_contexts clarifies when e.word is None. Lock
        in that the extractor + new intent rule agree."""
        for q in self.POSITIVE:
            with self.subTest(q=q):
                self.assertIsNotNone(
                    E.extract(q).word,
                    f"{q!r} must yield an extractable word entity",
                )


class RussianFillersDoNotBind(unittest.TestCase):
    """False-positive guard — the Latin-word requirement keeps Russian
    genitive fillers out of word_contexts. These have no studyable word
    and must NOT be dragged into the snippet path by the broadened rule."""

    NEGATIVE = [
        "примеры авторов",   # genitive «authors», not a word target
        "примеры слов",      # genitive «words»
        "примеры книг",      # genitive «books»
    ]

    def test_fillers_stay_out_of_word_contexts(self):
        for q in self.NEGATIVE:
            with self.subTest(q=q):
                self.assertNotEqual(
                    I.classify(q).label, "word_contexts",
                    f"{q!r} must NOT bind the bare-examples word_contexts rule",
                )


class PreExistingFormsStillRoute(unittest.TestCase):
    """The broadened rule must not break the original word_contexts forms."""

    def test_legacy_forms(self):
        for q in ("приведи примеры heart у Дойла",
                  "примеры использования heart",
                  "в каком контексте употребляется heart"):
            with self.subTest(q=q):
                self.assertEqual(I.classify(q).label, "word_contexts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
