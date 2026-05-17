"""v2 enrich_word + export_word_list."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


@tool(
    name="enrich_word",
    category="learning",
    description=(
        "LLM-обогащение одного слова: translation, definition, POS, CEFR, etymology, "
        "proper_noun verdict. Кешируется на диске в word_dictionary.json."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "word":         {"type": "string"},
            "contexts":     {"type": "array", "items": {"type": "string"},
                             "description": "примеры использования для anti-hallucination"},
            "lemma_hint":   {"type": "string"},
            "pos_hint":     {"type": "string"},
            "target_lang":  {"type": "string", "description": "default 'ru'"},
        },
        "required": ["word"],
    },
    requires=["word"],
    cost="medium",   # LLM round-trip
    cacheable=False,  # has its own on-disk word_dictionary cache
)
def enrich_word(word: str, contexts=None, lemma_hint: str = "",
                pos_hint: str = "", target_lang: str = "ru") -> ToolResult:
    try:
        from scripts.learning_tools import enrich_word as _v1
    except ImportError as e:
        return ToolResult.fail(tool="enrich_word", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(word=word, contexts=contexts or [],
              lemma_hint=lemma_hint, pos_hint=pos_hint,
              target_lang=target_lang)
    query = {"word": word, "lemma_hint": lemma_hint, "pos_hint": pos_hint,
             "target_lang": target_lang}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="enrich_word", err_type="internal",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(tool="enrich_word", data=raw, query=query)


@tool(
    name="export_word_list",
    category="learning",
    description=(
        "Экспорт списка слов в anki_csv / anki_apkg / markdown / json. "
        "anki_apkg создаёт one-shot deck готовый для импорта в Anki без mapping dialog."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "words":        {"type": "array", "description":
                             "list of strings or dicts (from learning_words.results)"},
            "format":       {"type": "string",
                             "enum": ["anki_csv", "anki_apkg", "markdown", "json"]},
            "out_path":     {"type": "string"},
            "target_lang":  {"type": "string", "description": "default 'ru'"},
            "deck_name":    {"type": "string", "description": "Anki deck name"},
        },
        "required": ["words"],
    },
    requires=[],
    cost="cheap",
    cacheable=False,  # writes to disk
)
def export_word_list(words, format: str = "anki_csv",
                     out_path: str | None = None,
                     target_lang: str = "ru",
                     deck_name: str = "wordcracker") -> ToolResult:
    try:
        from scripts.learning_tools import export_word_list as _v1
    except ImportError as e:
        return ToolResult.fail(tool="export_word_list", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(words=words, format=format, out_path=out_path,
              target_lang=target_lang, deck_name=deck_name)
    query = {"format": format, "n_words": len(words), "target_lang": target_lang}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="export_word_list", err_type="invalid_args",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(tool="export_word_list", data=raw, query=query)
