"""FilterSpec — unified scope/filter contract for v2 tools.

Contract: docs/v2/SPECS.md §2.

A FilterSpec describes "what subcorpus the tool should look at". It collapses
the half-dozen ad-hoc scope shapes scattered across v1 (author_regex, scope={...},
year_from/year_to, country, min_corpus_count) into a single dataclass.

`from_legacy_scope` accepts the old shapes (`"all_corpus"`, `{"book": "PG1342"}`,
`{"author": "^Wilde,", "country": "GB"}`) so v2 tools can be called from the v1
dispatcher while the call sites migrate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal


@dataclass
class FilterSpec:
    # scope
    author_regex: str | None = None
    pg_id: str | None = None
    user_id: str | None = None
    lang: str = "en"
    country: str | None = None

    # period (writing prime proxy unless explicit basis given)
    year_from: int | None = None
    year_to: int | None = None
    year_basis: Literal["auto", "pub_year", "birth_plus_30"] = "auto"

    # quality
    min_corpus_count: int = 0
    min_author_count: int = 5
    exclude_proper_nouns: bool = True
    exclude_metalinguistic: bool = True
    pos_filter: list[str] | None = None

    # limits
    max_books: int = 10_000
    top_n: int = 50

    # ----- adapters -----

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_legacy_scope(cls, scope) -> "FilterSpec":
        """Adapt v1 `scope` shapes to a FilterSpec.

        v1 callers used three shapes:
          * "all_corpus"
          * {"book": "PG1342"}
          * {"author": "^Wilde,", "country": "GB", "year_from": 1880, "year_to": 1900}
        Other keys come through `raw_extra` for tools that want them.
        """
        if scope == "all_corpus" or scope is None:
            return cls()
        if isinstance(scope, str):
            if scope.startswith("PG") or scope.startswith("U"):
                return cls(pg_id=scope)
            # bare regex string
            return cls(author_regex=scope)
        if isinstance(scope, dict):
            if "book" in scope or "pg_id" in scope:
                pg = scope.get("book") or scope.get("pg_id")
                return cls(pg_id=pg)
            if "user_id" in scope:
                return cls(user_id=scope["user_id"])
            return cls(
                author_regex=scope.get("author") or scope.get("author_regex"),
                country=scope.get("country"),
                year_from=scope.get("year_from"),
                year_to=scope.get("year_to"),
                lang=scope.get("lang", "en"),
                pos_filter=scope.get("pos_filter"),
                min_corpus_count=scope.get("min_corpus_count", 0),
                exclude_proper_nouns=scope.get("exclude_proper_nouns", True),
            )
        raise ValueError(f"unsupported scope shape: {scope!r}")

    def explain(self) -> str:
        """Human-readable description for the renderer."""
        bits = []
        if self.pg_id:
            bits.append(f"книга {self.pg_id}")
        elif self.user_id:
            bits.append(f"user-загрузка {self.user_id}")
        elif self.author_regex:
            bits.append(f"автор `{self.author_regex}`")
        else:
            bits.append("весь корпус")
        if self.country:
            bits.append(f"страна={self.country}")
        if self.year_from or self.year_to:
            bits.append(f"период {self.year_from or '…'}–{self.year_to or '…'}")
        if self.pos_filter:
            bits.append(f"POS={','.join(self.pos_filter)}")
        if self.min_corpus_count:
            bits.append(f"min_corpus={self.min_corpus_count}")
        return ", ".join(bits)
