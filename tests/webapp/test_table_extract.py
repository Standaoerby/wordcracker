"""N1 — table cells are native scalars; extraction shapes (plan.md §2.4)."""
from __future__ import annotations

import json

import numpy as np

from scripts.v2.table_extract import _scalar, extract_tables


class TestScalar:
    def test_numpy_int_stays_int(self):
        v = _scalar(np.int64(42))
        assert v == 42 and type(v) is int

    def test_numpy_float_stays_float(self):
        v = _scalar(np.float32(3.5))
        assert v == 3.5 and type(v) is float

    def test_numpy_bool_stays_bool(self):
        v = _scalar(np.bool_(True))
        assert v is True

    def test_native_passthrough(self):
        for x in (None, True, 7, 3.14, "слово"):
            assert _scalar(x) is x or _scalar(x) == x

    def test_nested_dict_becomes_json_string(self):
        v = _scalar({"a": 1, "b": [2, 3]})
        assert isinstance(v, str)
        assert json.loads(v) == {"a": 1, "b": [2, 3]}

    def test_unserialisable_falls_back_to_str(self):
        class Weird:
            def __str__(self):
                return "weird"
        assert _scalar(Weird()) == "weird"


class TestExtract:
    def test_record_list(self):
        tables = extract_tables("top_ngrams", [
            {"ngram": "of the", "count": np.int64(100)},
            {"ngram": "in a", "count": np.int64(50)},
        ])
        assert len(tables) == 1
        t = tables[0]
        assert t["columns"] == ["ngram", "count"]
        assert t["rows"] == [["of the", 100], ["in a", 50]]
        assert type(t["rows"][0][1]) is int  # N1

    def test_dict_with_record_lists(self):
        tables = extract_tables("compare", {
            "books": [{"pg_id": "PG1342", "title": "Pride and Prejudice"}],
            "note": "x",
        })
        assert len(tables) == 1
        assert tables[0]["tool"] == "compare"

    def test_dict_with_two_record_lists_gets_suffixes(self):
        tables = extract_tables("compare", {
            "a": [{"x": 1}],
            "b": [{"y": 2}],
        })
        assert {t["tool"] for t in tables} == {"compare.a", "compare.b"}

    def test_scalar_dict_single_row(self):
        tables = extract_tables("corpus_stats", {"books": 1828, "tokens": 9.5})
        assert len(tables) == 1
        assert tables[0]["rows"] == [[1828, 9.5]]

    def test_private_keys_dropped(self):
        tables = extract_tables("t", [{"word": "ardour", "_render_note": "x"}])
        assert tables[0]["columns"] == ["word"]

    def test_col_meta_book(self):
        tables = extract_tables("find_book", [
            {"title": "Emma", "pg_id": "PG158", "downloads": 5}])
        meta = tables[0]["col_meta"]
        assert meta["title"] == {"kind": "book", "id_col": "pg_id"}

    def test_col_meta_word(self):
        tables = extract_tables("learning_words", [{"word": "ardour", "freq": 3}])
        assert tables[0]["col_meta"]["word"] == {"kind": "word"}

    def test_no_meta_without_id_cols(self):
        tables = extract_tables("stats", [{"metric": "x", "value": 1}])
        assert "col_meta" not in tables[0]

    def test_never_raises(self):
        class Boom:
            def __getattr__(self, _):
                raise RuntimeError("boom")
        assert extract_tables("t", Boom()) == []

    def test_empty_and_scalar_data(self):
        assert extract_tables("t", []) == []
        assert extract_tables("t", "просто текст") == []
