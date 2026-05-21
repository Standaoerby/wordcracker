"""FilterSpec — unified scope/filter contract for v2 tools.

Contract: docs/v2/SPECS.md §2.

A FilterSpec describes "what subcorpus the tool should look at". It collapses
the half-dozen ad-hoc scope shapes scattered across v1 (author_regex, scope={...},
year_from/year_to, country, min_corpus_count) into a single dataclass.

`from_legacy_scope` accepts the old shapes (`"all_corpus"`, `{"book": "PG1342"}`,
`{"author": "^Wilde,", "country": "GB"}`) so v2 tools can be called from the v1
dispatcher while the call sites migrate.

v5 Phase 0 extension ([[architecture_refactor_v5_plan]] §P5):
Added quality-control fields so the same FilterSpec covers the cases that
R14 exposed:
- `exclude_toponyms` — places leaking into author_vocab (B-R14-15: Doyle's
  burger/uitlanders/belmont/colesberg)
- `exclude_translit_names` — Cyrillic→Latin transliterations of character
  names (B-R14-15: Dostoevsky's pyotr/alexandrovna/katerina)
- `exclude_corporate_authors` — CIA/Library of Congress/anonymous
  (B-R14-1: CIA #3 in top_authors_by)
- `level` — CEFR level (was scattered across tool-specific args)
- `validate()` — returns list of issues; plan-builder MUST check before
  passing to tool (Phase 4 will enforce this in plan registry)

Defaults are chosen to be SAFE: new fields default to True/sensible values
so existing tools that don't explicitly pass them get the right behaviour.
Phase 0 keeps the contract; Phase 2+ wires it into tools one by one.
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
    # v5 Phase 0 — fine-grained noise controls. Default True is safe:
    # tools that don't yet apply them ignore the field; tools that do
    # apply them produce cleaner results without extra plan-builder work.
    exclude_toponyms: bool = True
    exclude_translit_names: bool = True
    exclude_corporate_authors: bool = True
    exclude_archaic: bool = False        # alpha2 land, kept here for FilterSpec unity

    # CEFR / learning level (used by learning_words and downstream filters).
    # Acceptable: 'basic', 'intermediate', 'advanced', 'rare', 'A1'..'C2', None.
    level: str | None = None

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
        if self.level:
            bits.append(f"level={self.level}")
        return ", ".join(bits)

    # ---- v5 Phase 0: validation contract ----

    _VALID_LEVELS = {
        "basic", "intermediate", "advanced", "rare",
        "a1", "a2", "b1", "b2", "c1", "c2",
    }
    _VALID_YEAR_BASIS = {"auto", "pub_year", "birth_plus_30"}
    _VALID_POS = {
        "ADJ", "ADV", "NOUN", "PROPN", "VERB", "AUX", "CCONJ",
        "DET", "INTJ", "NUM", "PART", "PRON", "SCONJ", "SYM", "X",
    }

    def validate(self) -> list[str]:
        """Return list of contract violations. Empty list = valid.

        Plan-builder MUST call this before invoking a tool (Phase 4 will
        enforce this in the plan registry). For Phase 0, it's available
        for opt-in use and is exercised by golden tests.
        """
        issues: list[str] = []

        # Scope: at most one primary scope identifier
        scopes_set = sum(1 for s in (self.pg_id, self.user_id, self.author_regex) if s)
        if scopes_set > 1 and not (self.pg_id and self.author_regex):
            # author + pg_id is OK (book-scoped affinity for an author);
            # author + user_id or pg_id + user_id is not.
            if self.user_id and (self.author_regex or self.pg_id):
                issues.append("user_id is mutually exclusive with author_regex/pg_id")

        # Year range sanity
        if self.year_from is not None and self.year_to is not None:
            if self.year_from > self.year_to:
                issues.append(
                    f"year_from={self.year_from} > year_to={self.year_to}"
                )
        if self.year_basis not in self._VALID_YEAR_BASIS:
            issues.append(
                f"year_basis={self.year_basis!r} not in {self._VALID_YEAR_BASIS}"
            )

        # Level sanity
        if self.level is not None and self.level.lower() not in self._VALID_LEVELS:
            issues.append(
                f"level={self.level!r} not in {sorted(self._VALID_LEVELS)}"
            )

        # POS sanity
        if self.pos_filter:
            bad = [p for p in self.pos_filter if p.upper() not in self._VALID_POS]
            if bad:
                issues.append(f"pos_filter has unknown tags: {bad}")

        # Limits sanity
        if self.top_n <= 0:
            issues.append(f"top_n={self.top_n} must be > 0")
        if self.max_books <= 0:
            issues.append(f"max_books={self.max_books} must be > 0")
        if self.min_corpus_count < 0:
            issues.append(f"min_corpus_count={self.min_corpus_count} must be >= 0")

        # Country sanity (loose — just shape check, not full ISO list)
        if self.country is not None and len(self.country) not in (2, 3):
            issues.append(
                f"country={self.country!r} should be ISO 3166 alpha-2/3"
            )

        # Lang sanity
        if self.lang is not None and len(self.lang) not in (2, 3):
            issues.append(f"lang={self.lang!r} should be ISO 639 code")

        return issues

    def is_valid(self) -> bool:
        return not self.validate()
