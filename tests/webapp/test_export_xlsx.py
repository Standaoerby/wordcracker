"""T4 xlsx export. Включая обязательный N1-тест: числовая ячейка
сортируется как число (cell.data_type == 'n'), не как текст."""
from __future__ import annotations

import io

import numpy as np
import pytest

# Self-skip while openpyxl is not yet in requirements.lock (S6 regen).
openpyxl = pytest.importorskip(
    "openpyxl", reason="openpyxl not in requirements.lock yet (S6 regen)")

from api.export import safe_filename, sheet_name, tables_to_xlsx
from scripts.v2.table_extract import extract_tables


def _load(content: bytes):
    return openpyxl.load_workbook(io.BytesIO(content))


class TestXlsxNumericCellIsNumber:
    """plan.md §10.3 — the review-N1 regression test."""

    def test_xlsx_numeric_cell_is_number(self):
        # The full pipeline: numpy-typed tool data → table_extract → xlsx.
        tables = extract_tables("top_ngrams", [
            {"ngram": "of the", "count": np.int64(42), "share": np.float32(3.14)},
        ])
        wb = _load(tables_to_xlsx(tables))
        ws = wb["top_ngrams"]
        count_cell, share_cell = ws.cell(row=2, column=2), ws.cell(row=2, column=3)
        assert isinstance(count_cell.value, int) and count_cell.value == 42
        assert isinstance(share_cell.value, float)
        assert count_cell.data_type == "n" and share_cell.data_type == "n"

    def test_numeric_column_sorts_as_numbers(self):
        # 10 < 100 numerically; as text "10" < "100" < "9" — the bug N1 kills.
        tables = [{"tool": "t", "columns": ["n"], "rows": [[10], [9], [100]]}]
        wb = _load(tables_to_xlsx(tables))
        values = [ws_cell[0].value for ws_cell in wb["t"].iter_rows(min_row=2)]
        assert all(isinstance(v, int) for v in values)
        assert sorted(values) == [9, 10, 100]


class TestSheetNames:
    def test_truncated_to_31(self):
        used: set[str] = set()
        assert len(sheet_name("x" * 64, used)) == 31

    def test_forbidden_chars_replaced(self):
        assert sheet_name("a[b]:c*d?e/f\\g", set()) == "a_b__c_d_e_f_g"

    def test_duplicates_suffixed(self):
        used: set[str] = set()
        assert sheet_name("tool", used) == "tool"
        assert sheet_name("tool", used) == "tool_2"
        assert sheet_name("tool", used) == "tool_3"

    def test_duplicate_suffix_fits_31(self):
        used: set[str] = set()
        long = "y" * 31
        assert sheet_name(long, used) == long
        second = sheet_name(long, used)
        assert len(second) == 31 and second.endswith("_2")

    def test_empty_name(self):
        assert sheet_name("", set()) == "sheet"

    def test_one_sheet_per_table(self):
        tables = [
            {"tool": "a", "columns": ["x"], "rows": [[1]]},
            {"tool": "b", "columns": ["y"], "rows": [[2]]},
        ]
        assert _load(tables_to_xlsx(tables)).sheetnames == ["a", "b"]

    def test_empty_tables_yield_valid_workbook(self):
        assert _load(tables_to_xlsx([])).sheetnames == ["empty"]


class TestFilename:
    def test_default(self):
        assert safe_filename(None) == "wordcracker_export.xlsx"

    def test_extension_appended_and_sanitised(self):
        assert safe_filename("выгрузка/2026").endswith(".xlsx")
        assert "/" not in safe_filename("выгрузка/2026")
