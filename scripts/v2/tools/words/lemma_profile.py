"""v2 lemma_profile — quick rarity/POS/difficulty lookup for a single lemma.

Powered by scripts/v2/profiles/lemma.py. First call on a new lemma builds
+ persists; subsequent calls return cached.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.profiles import lemma as lemma_mod
from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


@tool(
    name="lemma_profile",
    category="words",
    description=(
        "Снимок леммы: global_count, rarity (0-1, выше = реже), "
        "difficulty (basic/intermediate/advanced/rare/ultra_rare). "
        "Для «насколько частое слово», «что за уровень слова X»."
    ),
    input_schema={
        "type": "object",
        "properties": {"lemma": {"type": "string"}},
        "required": ["lemma"],
    },
    requires=["word"],
    cost="cheap",
    cacheable=True,
)
def lemma_profile(lemma: str) -> ToolResult:
    p = lemma_mod.get_or_build(lemma)
    if p is None:
        return ToolResult(
            ok=False, tool="lemma_profile", query={"lemma": lemma},
            data={"lemma": lemma}, error=None,
            warnings=[ToolWarning(
                "not_in_corpus",
                f"'{lemma}' has 0 occurrences in corpus_counts — possibly a typo, "
                f"proper noun, or post-2018 coinage.",
            )],
            coverage=Coverage(),
        )
    result = ToolResult.success(
        tool="lemma_profile", data=p,
        coverage=Coverage(
            books_matched=p.get("book_count") or -1,
            books_total=-1,
        ),
        query={"lemma": lemma},
    )

    # v5 Phase 2.5 — TOP_N_TABLE view (lemma stats as key-value rows).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        rows = [
            {"metric": "global_count",
             "value": p.get("global_count") or "—"},
            {"metric": "rarity (0-1)",
             "value": (f"{p['rarity']:.3f}"
                        if isinstance(p.get("rarity"), (int, float))
                        else (p.get("rarity") or "—"))},
            {"metric": "difficulty", "value": p.get("difficulty") or "—"},
            {"metric": "books with lemma",
             "value": p.get("book_count") or "—"},
        ]
        view = vb.build_top_n_table(
            rows=rows,
            columns=["metric", "value"],
            headline=f"Профиль леммы — {lemma}",
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.lemma_profile").warning(
            "lemma_profile view emission failed: %s", e,
        )
    return result
