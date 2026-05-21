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
    # Sprint 20+ B7 — filter ISO-639 language codes leaking into
    # related_forms / etymology output. Stan Round 11 Q28: «связанные
    # формы: enm-wmi, ang, gmw-pro, gem-pro, ine-pro, swerd, sweard» —
    # first 5 are language codes, only swerd/sweard are real attested
    # forms. Wrapper-level filter, not v1 change.
    if isinstance(raw, dict):
        from scripts.v2.tools._result_filters import (
            looks_like_iso_code,
        )
        for related_key in ("related_forms", "etymology_chain",
                              "cognates", "derived_from"):
            forms = raw.get(related_key)
            if not isinstance(forms, list):
                continue
            kept = []
            dropped = []
            for item in forms:
                # item can be str «ang» or dict {"word": "ang", "lang": "OE"}
                token = item if isinstance(item, str) else (
                    item.get("word") if isinstance(item, dict) else None
                )
                if isinstance(token, str) and looks_like_iso_code(token):
                    dropped.append(token)
                else:
                    kept.append(item)
            if dropped:
                raw[related_key] = kept
                note = (f"v2 filter dropped ISO-639 language codes from "
                         f"{related_key}: {', '.join(dropped[:5])}"
                         f"{'…' if len(dropped) > 5 else ''}")
                prev = raw.get("_render_note", "")
                raw["_render_note"] = (prev + " | " + note if prev else note)
    result = ToolResult.success(tool="enrich_word", data=raw, query=query)

    # v5 Phase 2.5 — ETYMOLOGY_BUNDLE view for the enriched word.
    # Closes B-R14-2 partial-bundle: slots_available makes missing
    # facets explicit instead of silently skipping.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        etym = None
        if raw.get("primary_family") or raw.get("family_chain"):
            etym = {
                "primary_family": raw.get("primary_family"),
                "family_chain": raw.get("family_chain") or raw.get("etymology_chain") or [],
            }
        view = vb.build_etymology_bundle(
            word=word,
            translation_ru=(raw.get("translation_ru") or raw.get("translation")
                             if target_lang == "ru" else raw.get("translation")),
            ipa=raw.get("ipa"),
            pos=raw.get("pos") or raw.get("pos_tag"),
            definition_en=raw.get("definition") or raw.get("definition_en"),
            etymology=etym,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.learning.enrich").warning(
            "enrich_word view emission failed: %s", e,
        )
    return result


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
    result = ToolResult.success(tool="export_word_list", data=raw, query=query)

    # v5 Phase 2.5 — EXPORT_ARTIFACT view.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        fmt_map = {"anki_csv": "anki_csv", "anki_apkg": "anki_csv",
                    "csv": "csv", "tsv": "tsv",
                    "markdown": "markdown", "json": "json"}
        fmt_normalized = fmt_map.get(format, "csv")
        view = vb.build_export_artifact(
            format=fmt_normalized,
            content=str(raw.get("content") or raw.get("text") or "")[:8000],
            filename_suggestion=raw.get("filename") or raw.get("out_path")
                                  or f"wordcracker_export.{fmt_normalized.split('_')[-1]}",
            item_count=len(words) if isinstance(words, list) else 0,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.learning.enrich").warning(
            "export_word_list view emission failed: %s", e,
        )
    return result
