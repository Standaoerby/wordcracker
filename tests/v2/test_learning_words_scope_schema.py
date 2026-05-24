"""learning_words.input_schema must accurately describe the v1 scope contract.

Prod incident 2026-05-24 (bad_answers id 6d864f676266): user query
«я учу английский, с чего начать?» routed through learning_words with
`scope: "corpus"`. The wrapper rejected via v1's

    "bad scope; use {'book': PGid} or {'author': regex} or 'all_corpus'"

Root cause (per R3): the v2 wrapper's input_schema declared

    "scope": {"type": "object", "description": "{'book': PGid} | {'author': regex}"}

— which is wrong on two counts:
  (a) v1 also accepts the literal STRING 'all_corpus', so the declared
      `type: object` lies about what the wrapper truly accepts;
  (b) the description doesn't mention 'all_corpus', so when the LLM
      wanted a whole-corpus call it improvised `"corpus"`.

This test locks the corrected contract: schema must declare
`oneOf` over the three v1 branches, and must validate / reject input
according to that contract. Negative half (R2):
`scope="corpus"` is rejected against the new schema; would have been
accepted (wrongly) by the loose pre-fix schema.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tool_registry import REGISTRY


class LearningWordsScopeSchemaShape(unittest.TestCase):
    """Pure structural assertions on the schema — no jsonschema lib
    required. Locks in the oneOf+enum shape so future edits can't
    quietly regress to the loose `{type: object}` form."""

    def setUp(self):
        self.spec = REGISTRY["learning_words"]
        self.scope_schema = self.spec.input_schema["properties"]["scope"]

    def test_scope_declares_oneOf(self):
        """The fix: scope is a polymorphic shape, expressed via oneOf —
        NOT a flat `type: object` that hides the 'all_corpus' string
        branch from the LLM's view."""
        self.assertIn("oneOf", self.scope_schema,
                      "scope schema must declare oneOf — see prod incident "
                      "6d864f676266 (bad_answers 2026-05-24)")
        self.assertEqual(len(self.scope_schema["oneOf"]), 3,
                         "expected 3 branches: book / author / all_corpus")

    def test_scope_oneOf_has_book_branch(self):
        branches = self.scope_schema["oneOf"]
        book_branches = [b for b in branches
                         if b.get("type") == "object"
                         and "book" in b.get("properties", {})]
        self.assertEqual(len(book_branches), 1,
                         "exactly one {book: PGid} branch expected")
        self.assertEqual(book_branches[0]["required"], ["book"])

    def test_scope_oneOf_has_author_branch(self):
        branches = self.scope_schema["oneOf"]
        author_branches = [b for b in branches
                           if b.get("type") == "object"
                           and "author" in b.get("properties", {})]
        self.assertEqual(len(author_branches), 1)
        self.assertEqual(author_branches[0]["required"], ["author"])

    def test_scope_oneOf_has_all_corpus_literal(self):
        """The branch the LLM didn't know about, pre-fix."""
        branches = self.scope_schema["oneOf"]
        str_branches = [b for b in branches if b.get("type") == "string"]
        self.assertEqual(len(str_branches), 1)
        self.assertEqual(str_branches[0].get("enum"), ["all_corpus"],
                         "string branch must enum to ['all_corpus'] — "
                         "any other literal (e.g. 'corpus', 'all') must "
                         "fail validation")

    def test_description_mentions_all_corpus(self):
        """Even readers who don't validate schemas (some LLMs lean on
        description text alone) must see 'all_corpus' in the description."""
        desc = self.scope_schema.get("description", "")
        self.assertIn("all_corpus", desc,
                      "scope description must spell out 'all_corpus' so "
                      "the LLM doesn't improvise (it improvised "
                      "'corpus' on 2026-05-24)")


class LearningWordsScopeSchemaValidation(unittest.TestCase):
    """If jsonschema is available (it is on prod — pulled transitively
    by chromadb), validate the actual incident input against the
    declared schema. Pre-fix code's loose `type: object` would have
    accepted scope=anything; post-fix oneOf rejects the bad value."""

    @classmethod
    def setUpClass(cls):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("jsonschema not installed")

    def setUp(self):
        from scripts.v2.tool_registry import REGISTRY
        self.schema = REGISTRY["learning_words"].input_schema

    def _validate(self, args):
        import jsonschema
        jsonschema.validate(args, self.schema)

    def test_corpus_literal_rejected(self):
        """THE incident: LLM emitted scope='corpus'. Must be rejected
        by the schema — there's no enum value for it."""
        import jsonschema
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"scope": "corpus", "level": "basic", "top": 50})

    def test_all_corpus_literal_accepted(self):
        """The valid string scope."""
        self._validate({"scope": "all_corpus", "level": "intermediate"})

    def test_book_scope_accepted(self):
        self._validate({"scope": {"book": "PG1342"}, "level": "basic"})

    def test_author_scope_accepted(self):
        self._validate({"scope": {"author": "^Doyle,"}, "level": "advanced"})

    def test_arbitrary_object_rejected(self):
        """Pre-fix `type: object` would have accepted any dict. Post-fix
        oneOf demands book or author key specifically."""
        import jsonschema
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"scope": {"random_key": "foo"}})

    def test_scope_required(self):
        """scope is required — request without it must fail validation."""
        import jsonschema
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"level": "basic"})


if __name__ == "__main__":
    unittest.main()
