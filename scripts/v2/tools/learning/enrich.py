"""v2 enrich_word + export_word_list."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1EnrichWord, V1ExportWordList


@tool(
    name="enrich_word",
    category="learning",
    description=(
        "LLM-обогащение одного слова: translation, definition, POS, CEFR, "
        "proper_noun verdict. Кешируется на диске в word_dictionary.json. "
        "Этимологию НЕ поставляет (R-28 B114) — её даёт word_etymology."
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
    # R-28 B114 (2026-06-11) — «честный учебный контент»: учебный факт
    # без tool-опоры не публикуется (D-R28-1). Враппер стирает
    # LLM-сгенерированные etymology/family_chain/primary_family (Q4:
    # ajar/garlic «от греческого krokos») и example_sentence (примеры —
    # только корпусные hits), гейтит перевод имён собственных (S4:
    # galatz→«стеклянный сосуд» — топоним из «Дракулы») и помечает
    # выживший перевод кэвиатом «сгенерирован моделью, не словарём».
    wrapper_version="v3-r28-honest-learning",
)
@v1_contract(v1_fn="scripts.learning_tools.enrich_word",
             schema=V1EnrichWord)
def enrich_word(word: str, contexts=None, lemma_hint: str = "",
                pos_hint: str = "", target_lang: str = "ru") -> ToolResult:
    from scripts.learning_tools import enrich_word as _v1
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
        for related_key in ("related_forms", "cognates", "derived_from"):
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
    # R-28 B114 — «честный учебный контент»: учебный факт без tool-опоры
    # не публикуется (D-R28-1). Стирание полей сохраняет V1EnrichWord-
    # форму (пустые значения из __defaults__), контракт не нарушается.
    honesty_notes: list[str] = []
    propn_gate_reason: str | None = None
    if isinstance(raw, dict):
        # 1. Этимология enrich_word — LLM-фантазия (Q4: ajar/garlic «от
        #    греческого krokos»). Канонический источник — word_etymology
        #    (Wiktionary-backed, параллельный шаг бандла W-10). Старые
        #    записи дискового кэша word_dictionary.json ещё несут поле —
        #    стираем на враппере, не полагаясь на чистку кэша.
        if (raw.get("etymology") or raw.get("family_chain")
                or raw.get("primary_family")):
            raw["etymology"] = ""
            raw["family_chain"] = []
            raw["primary_family"] = ""
            honesty_notes.append(
                "этимология из enrich_word УДАЛЕНА (LLM-генерация без "
                "tool-опоры) — этимологию бери ТОЛЬКО из word_etymology; "
                "если word_etymology пуст, пиши ровно: «Этимологии этого "
                "слова в данных корпуса нет»")
        # 2. Примеры — только корпусные hits (hybrid_search /
        #    word_contexts). LLM-сочинённый example_sentence не публикуется.
        if raw.get("example_sentence"):
            raw["example_sentence"] = ""
            honesty_notes.append(
                "example_sentence УДАЛЁН (LLM-сочинённый) — примеры "
                "употребления бери только из корпусных сниппетов "
                "(hybrid_search / word_contexts)")
        # 3. propn-гейт перевода (S4: galatz — город Галац из «Дракулы» —
        #    получил перевод «стеклянный сосуд»). Детектор WP3 (gazetteer/
        #    patronymic/surname) + кураторские топонимы + собственный
        #    LLM-вердикт proper_noun. Имя собственное не переводится.
        from scripts.v2.tools.authors._propn_gazetteer import (
            is_proper_name_token,
        )
        from scripts.v2.tools.authors._toponym_filter import is_toponym
        if is_proper_name_token(word):
            propn_gate_reason = "детектор имён (gazetteer/patronymic/surname)"
        elif is_toponym(word):
            propn_gate_reason = "кураторский топоним"
        elif raw.get("proper_noun") is True:
            propn_gate_reason = "LLM-вердикт proper_noun"
        if propn_gate_reason:
            for k in ("translation_ru", "translation", "translation_en"):
                if raw.get(k):
                    raw[k] = ""
            raw["propn_gate"] = propn_gate_reason
            honesty_notes.append(
                f"«{word}» — имя собственное ({propn_gate_reason}): "
                f"перевод НЕ публикуется; вместо перевода пиши ровно: "
                f"«имя собственное (вероятно топоним/персонаж)»")
        elif raw.get("translation_ru") or raw.get("translation"):
            # 3a. Словаря EN→RU в стеке нет (Phase 0b R-28) — перевод
            # остаётся LLM-генерацией и обязан носить видимый кэвиат.
            honesty_notes.append(
                "перевод сгенерирован моделью, не словарём — покажи этот "
                "кэвиат видимой пометкой рядом с переводом")
        if honesty_notes:
            prev = raw.get("_render_note", "")
            joined = " | ".join(honesty_notes)
            raw["_render_note"] = (prev + " | " + joined if prev else joined)
    result = ToolResult.success(tool="enrich_word", data=raw, query=query)

    # v5 Phase 2.5 — ETYMOLOGY_BUNDLE view for the enriched word.
    # Closes B-R14-2 partial-bundle: slots_available makes missing
    # facets explicit instead of silently skipping.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        # R-28 B114 — enrich_word этимологию больше НЕ поставляет: его
        # family_chain/primary_family были LLM-генерацией и стёрты выше.
        # Слот etymology в бандле заполняет ТОЛЬКО word_etymology
        # (его собственный ETYMOLOGY_BUNDLE; Wiktionary-backed).
        # V1EnrichWord canonical: translation_ru / translation_<lang>, ipa,
        # pos, definition_en.
        view_caveats: list[str] = []
        if propn_gate_reason:
            view_caveats.append(
                f"«{word}» — имя собственное (вероятно топоним/персонаж); "
                f"перевод не публикуется")
        elif raw.get("translation_ru") or raw.get("translation"):
            view_caveats.append(
                "перевод сгенерирован моделью, не словарём")
        view = vb.build_etymology_bundle(
            word=word,
            translation_ru=(raw.get("translation_ru")
                            if target_lang == "ru"
                            else raw.get("translation")),
            ipa=raw.get("ipa"),
            pos=raw.get("pos"),
            definition_en=raw.get("definition_en"),
            etymology=None,
            caveats=view_caveats,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.learning.enrich").exception(
            "enrich_word view emission failed"
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
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.learning_tools.export_word_list",
             schema=V1ExportWordList)
def export_word_list(words, format: str = "anki_csv",
                     out_path: str | None = None,
                     target_lang: str = "ru",
                     deck_name: str = "wordcracker") -> ToolResult:
    from scripts.learning_tools import export_word_list as _v1
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
        # V1ExportWordList canonical: out_path, format, entries, content.
        view = vb.build_export_artifact(
            format=fmt_normalized,
            content=str(raw.get("content") or "")[:8000],
            filename_suggestion=(raw.get("out_path")
                                 or f"wordcracker_export.{fmt_normalized.split('_')[-1]}"),
            item_count=len(words) if isinstance(words, list) else 0,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.learning.enrich").exception(
            "export_word_list view emission failed"
        )
    return result
