"""Sprint 19 — admin library helpers + user-upload source labeling.

Two surfaces:
  1) scripts/admin_library.py — list / inspect / delete / reprocess
     helpers. Tested via tempdir fixtures (no production filesystem
     touched).
  2) rag_v2._detect_user_uploads — scans tool results for U-prefix
     book ids and flags them so the renderer can disclose.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


class UidNormalization(unittest.TestCase):
    def test_canonical_forms(self):
        from admin_library import normalize_uid
        self.assertEqual(normalize_uid("U7"), "U7")
        self.assertEqual(normalize_uid("u7"), "U7")
        self.assertEqual(normalize_uid("u042"), "U42")
        self.assertEqual(normalize_uid("  U99  "), "U99")

    def test_invalid_returns_none(self):
        from admin_library import normalize_uid
        self.assertIsNone(normalize_uid(""))
        self.assertIsNone(normalize_uid("PG1342"))
        self.assertIsNone(normalize_uid("XYZ"))
        self.assertIsNone(normalize_uid("Ufoo"))


class LibraryWithTempDir(unittest.TestCase):
    """Patch admin_library paths to a tempdir so we exercise the real
    file walking without touching /workspace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = root / "raw_text"
        self.tok = root / "user_tokens"
        self.cnt = root / "user_counts"
        self.meta = root / "user_uploads_metadata.csv"
        for p in [self.raw, self.tok, self.cnt]:
            p.mkdir(parents=True)
        # Three test books:
        #   U1: complete + healthy
        #   U2: raw exists but tokens missing
        #   U3: in metadata but no raw on disk
        (self.raw / "u1.txt").write_text(
            "Lorem ipsum " * 5000,  # ~60KB → healthy size
            encoding="utf-8",
        )
        (self.tok / "U1_tokens.txt").write_text(
            "\n".join(["lorem", "ipsum"] * 100), encoding="utf-8",
        )
        (self.cnt / "U1_counts.txt").write_text(
            "lorem\t250\nipsum\t250\n", encoding="utf-8",
        )
        (self.raw / "u2.txt").write_text("X" * 50_000, encoding="utf-8")
        # U2 has no tokens/counts files
        # Write metadata
        with open(self.meta, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "id", "title", "author", "authoryearofbirth",
                "authoryearofdeath", "language", "downloads",
                "subjects", "type", "source_filename", "uploaded_ts",
            ])
            w.writeheader()
            for row in [
                {"id": "U1", "title": "Healthy Book", "author": "Test, A.",
                 "language": "en", "uploaded_ts": "2026-05-19T01:00:00Z",
                 "source_filename": "u1.txt"},
                {"id": "U2", "title": "Broken Book", "author": "Test, B.",
                 "language": "en", "uploaded_ts": "2026-05-19T02:00:00Z",
                 "source_filename": "u2.txt"},
                {"id": "U3", "title": "Missing Book", "author": "Test, C.",
                 "language": "en", "uploaded_ts": "2026-05-19T03:00:00Z",
                 "source_filename": "u3.txt"},
            ]:
                w.writerow(row)

        # Patch all four paths
        import admin_library
        self.patches = [
            mock.patch.object(admin_library, "RAW_DIR",    self.raw),
            mock.patch.object(admin_library, "USER_TOKENS", self.tok),
            mock.patch.object(admin_library, "USER_COUNTS", self.cnt),
            mock.patch.object(admin_library, "USER_META",   self.meta),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_list_finds_all_three(self):
        from admin_library import list_user_books
        books = list_user_books()
        ids = {b["id"] for b in books}
        self.assertEqual(ids, {"U1", "U2", "U3"})

    def test_health_classification(self):
        from admin_library import list_user_books
        books = {b["id"]: b for b in list_user_books()}
        self.assertEqual(books["U1"]["health"], "ok")
        self.assertEqual(books["U2"]["health"], "no_tokens")
        self.assertEqual(books["U3"]["health"], "no_raw")

    def test_audit_summary(self):
        from admin_library import audit_library
        summary = audit_library()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_health"]["ok"], 1)
        self.assertGreater(summary["total_bytes"], 0)

    def test_stats_healthy_book(self):
        from admin_library import book_stats
        s = book_stats("U1")
        self.assertEqual(s["id"], "U1")
        self.assertGreater(s["raw_bytes"], 50_000)
        self.assertEqual(s["total_tokens"], 500)
        self.assertEqual(s["vocab_size"], 2)
        self.assertEqual(len(s["top_words"]), 2)

    def test_stats_invalid_id(self):
        from admin_library import book_stats
        s = book_stats("ZZZ")
        self.assertIn("error", s)

    def test_stats_raw_missing(self):
        from admin_library import book_stats
        s = book_stats("U3")
        self.assertEqual(s.get("error"), "raw_not_found")

    def test_raw_path_returns_existing_file(self):
        from admin_library import book_raw_path
        p = book_raw_path("U1")
        self.assertIsNotNone(p)
        self.assertTrue(p.exists())

    def test_raw_path_missing(self):
        from admin_library import book_raw_path
        self.assertIsNone(book_raw_path("U3"))

    def test_delete_removes_files(self):
        from admin_library import delete_book, list_user_books
        result = delete_book("U1")
        self.assertEqual(result["id"], "U1")
        self.assertGreaterEqual(len(result["removed"]), 1)
        # After delete, U1 is gone from listing
        ids = {b["id"] for b in list_user_books()}
        self.assertNotIn("U1", ids)


class DetectUserUploadsInResults(unittest.TestCase):
    """Sprint 19 — rag_v2._detect_user_uploads walks tool result data
    and flags U-prefix book references. Drives renderer disclosure."""

    def _ok(self, tool: str, data: dict):
        from scripts.v2._types import Coverage, ToolResult
        return ToolResult.success(
            tool=tool, data=data,
            coverage=Coverage(books_matched=1, books_total=-1),
        )

    def test_no_user_uploads_in_results(self):
        from scripts.v2.rag_v2 import _detect_user_uploads
        r = self._ok("find_book", {"matches": [
            {"pg_id": "PG1342", "title": "Pride and Prejudice"}]})
        has, count, sample = _detect_user_uploads([r])
        self.assertFalse(has)
        self.assertEqual(count, 0)

    def test_single_user_upload(self):
        from scripts.v2.rag_v2 import _detect_user_uploads
        r = self._ok("affinity_by_book", {
            "pg_id": "U7", "top": [{"word": "x", "affinity": 100}]})
        has, count, sample = _detect_user_uploads([r])
        self.assertTrue(has)
        self.assertEqual(count, 1)
        self.assertIn("U7", sample)

    def test_multiple_user_uploads_deduplicated(self):
        from scripts.v2.rag_v2 import _detect_user_uploads
        r1 = self._ok("hybrid_search", {"matches": [
            {"pg_id": "U7"}, {"pg_id": "U12"}, {"pg_id": "U7"}]})
        r2 = self._ok("find_book", {"matches": [{"pg_id": "U99"}]})
        has, count, sample = _detect_user_uploads([r1, r2])
        self.assertTrue(has)
        self.assertEqual(count, 3)  # U7 deduplicated

    def test_mixed_pg_and_user(self):
        from scripts.v2.rag_v2 import _detect_user_uploads
        r = self._ok("find_book_by_topic", {"matches": [
            {"pg_id": "PG1342"}, {"pg_id": "U5"}, {"pg_id": "PG345"}]})
        has, count, _ = _detect_user_uploads([r])
        self.assertTrue(has)
        self.assertEqual(count, 1)

    def test_skips_failed_results(self):
        from scripts.v2._types import ToolResult
        from scripts.v2.rag_v2 import _detect_user_uploads
        failed = ToolResult.fail(tool="x", err_type="internal", message="boom")
        has, count, _ = _detect_user_uploads([failed])
        self.assertFalse(has)


if __name__ == "__main__":
    unittest.main(verbosity=2)
