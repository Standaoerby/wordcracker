"""template_executor — Phase 3 deterministic renderer.

Part of v5 architectural refactor ([[architecture_refactor_v5_plan]] §P2).

Single most-important file in the v5 effort: this is what turns a
typed `RenderableView` into user-visible markdown — with NO LLM call
and NO possibility of fabrication.

Architecture:

    RenderableView (typed, validated)
            │
            ▼  TemplateExecutor.render(view)
            │
            │  (pure Python — one function per view_type)
            │
            ▼
    markdown skeleton (str)
            │
            ▼  (later — Phase 3 step B)
    [optional] ProseBinder writes intro/next-steps prose,
                verified against view.payload before output
            │
            ▼
    final answer string

Why this matters:

The R14 fabrication class (B-R14-3, B-R14-2, B-R14-5, B-R14-14) all stem
from the renderer treating `RENDER_PROMPT` rules as soft guidance. Same
prompt, same tool output — sometimes correct, sometimes fabricated. LLMs
follow rules probabilistically; that's incompatible with hard
invariants like "do not invent facts".

This module makes fabrication structurally impossible for the parts of
the answer it controls: every cell in every table comes from
`view.payload`, no exceptions. The renderer's freedom is limited to
"intro paragraph + next-step suggestions" (Phase 3 step B), and those
get audited against the payload before output.

Coverage in this first cut: 16 view_types as listed in view_types.py.
Each render fn is a pure function: same input → same bytes out.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal

from scripts.v2.view_types import (
    EmptyReason,
    EmptyState,
    RenderableView,
    ViewType,
)

log = logging.getLogger("wordcracker.v2.template_executor")


# =====================================================================
# Formatting helpers (one source of truth for numbers, years, scores)
# =====================================================================

def format_int(n: int | float | None) -> str:
    """Number formatting — ASCII space thousand separator (U+0020)."""
    if n is None:
        return "—"
    try:
        i = int(n)
        return f"{i:,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def format_float(n: float | None, *, digits: int = 2) -> str:
    if n is None:
        return "—"
    try:
        return f"{float(n):.{digits}f}"
    except (TypeError, ValueError):
        return str(n)


def format_year_range(year_from: int | None, year_to: int | None) -> str:
    if year_from is None and year_to is None:
        return ""
    if year_from is not None and year_to is not None:
        return f"{year_from}–{year_to}"
    return f"{year_from or '…'}–{year_to or '…'}"


def format_score(score: float | None, *, digits: int = 4) -> str:
    """Burrows Delta / Flesch / similar metric values."""
    return format_float(score, digits=digits)


def format_share(share: float | None) -> str:
    """Share / percent — 0..1 expected."""
    if share is None:
        return "—"
    try:
        return f"{float(share) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(share)


# =====================================================================
# Markdown table helper — used by ~half the view types
# =====================================================================

def md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Produce a markdown pipe-table. All cells are converted to str via
    plain str() — caller is responsible for formatting numbers."""
    if not rows:
        return ""
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for r in rows:
        cells = [_escape_md_cell(str(c) if c is not None else "—") for c in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _escape_md_cell(s: str) -> str:
    """Escape pipes inside a markdown table cell. Newlines → spaces."""
    return s.replace("|", "\\|").replace("\n", " ")


# =====================================================================
# Empty-state renderer — used by many view types when payload is empty
# =====================================================================

def render_empty_state(es: EmptyState, *, language: str = "ru") -> str:
    """Render an EmptyState block. Always shows the human-readable
    reason — closes B-R14-3: renderer never has to invent why."""
    msg = es.message_ru if language == "ru" else es.message_en
    parts = [msg.strip()]
    if es.filters_applied:
        # Human-readable filter dump
        f_lines = []
        for k, v in es.filters_applied.items():
            if v is None or v == "" or v == []:
                continue
            f_lines.append(f"- `{k}` = `{v}`")
        if f_lines:
            parts.append("\n**Применённые фильтры:**\n" + "\n".join(f_lines))
    if es.suggestion:
        parts.append(f"\n_💡 {es.suggestion}_")
    return "\n\n".join(parts)


# =====================================================================
# Caveats renderer
# =====================================================================

def render_caveats(caveats: list[str]) -> str:
    if not caveats:
        return ""
    return "\n\n" + "\n".join(f"_ℹ️ {c}_" for c in caveats)


# =====================================================================
# Per-ViewType renderers
# =====================================================================

def _render_top_n_table(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    payload = view.payload
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    count_returned = payload.get("count_returned", len(rows))
    count_requested = payload.get("count_requested")

    table_rows = [[r.get(col, "—") for col in columns] for r in rows]
    body = md_table(columns, table_rows)

    headline = view.headline
    head = f"### {headline}\n\n" if headline else ""

    counts = ""
    if count_requested is not None and count_requested != count_returned:
        counts = (f"_Запрошено: {format_int(count_requested)}, "
                  f"возвращено: {format_int(count_returned)}_\n\n")

    return head + counts + body + render_caveats(view.caveats)


def _render_comparison_panel(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    payload = view.payload
    entities = payload.get("entities") or []
    metrics = payload.get("metrics") or []
    shared = payload.get("shared_signatures") or []

    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")

    # Metric panel — name + direction + scale
    if metrics:
        metric_rows = [
            [
                m.get("name", "—"),
                m.get("direction", "—"),
                m.get("scale", "—"),
                m.get("interpret", "—"),
            ]
            for m in metrics
        ]
        parts.append("**Метрики:**\n")
        parts.append(md_table(
            ["Метрика", "Направление", "Шкала", "Интерпретация"],
            metric_rows,
        ))

    # Entity comparison table
    entity_metric_names = []
    for m in metrics:
        n = m.get("name")
        if n:
            entity_metric_names.append(n)
    if entity_metric_names and entities:
        headers = ["Автор"] + entity_metric_names
        rows = []
        for e in entities:
            row = [e.get("name", "—")]
            em = e.get("metrics") or {}
            for mn in entity_metric_names:
                row.append(format_score(em.get(mn)))
            rows.append(row)
        parts.append("\n**Сравнение:**\n")
        parts.append(md_table(headers, rows))

    # Signature words per entity
    for e in entities:
        sig = e.get("signature_words") or []
        if sig:
            name = e.get("name", "—")
            parts.append(f"\n**Фирменные слова — {name}:**\n")
            parts.append(", ".join(f"`{w}`" for w in sig[:30]))

    if shared:
        parts.append(f"\n**Общие фирменные слова:** "
                     + ", ".join(f"`{w}`" for w in shared[:30]))

    return "\n".join(parts) + render_caveats(view.caveats)


def _render_readability_summary(view: RenderableView) -> str:
    payload = view.payload
    title = payload.get("book_title", "—")
    pg_id = payload.get("pg_id", "—")
    flesch = payload.get("flesch")
    fk = payload.get("flesch_kincaid")
    cefr = payload.get("cefr")
    wc = payload.get("word_count")

    parts = [f"### {title} ({pg_id})\n"]
    rows = [
        ["Flesch Reading Ease", format_float(flesch, digits=1),
         "выше = легче читать"],
        ["Flesch–Kincaid Grade", format_float(fk, digits=1),
         "школьный класс"],
        ["CEFR", cefr or "—", "уровень владения"],
    ]
    if wc is not None:
        rows.append(["Слов", format_int(wc), ""])
    parts.append(md_table(["Метрика", "Значение", "Интерпретация"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_etymology_bundle(view: RenderableView) -> str:
    p = view.payload
    word = p.get("word", "—")
    parts = [f"### {word}\n"]

    head_bits = []
    if p.get("translation_ru"):
        head_bits.append(f"**перевод**: {p['translation_ru']}")
    if p.get("ipa"):
        head_bits.append(f"**IPA**: /{p['ipa']}/")
    if p.get("pos"):
        head_bits.append(f"**POS**: {p['pos']}")
    if head_bits:
        parts.append(" · ".join(head_bits))

    if p.get("definition_en"):
        parts.append(f"\n_{p['definition_en']}_")

    snippets = p.get("snippets") or []
    if snippets:
        parts.append("\n**Примеры из корпуса:**\n")
        for s in snippets[:3]:
            snip = s.get("snippet", "").strip()
            title = s.get("title", "")
            author = s.get("author", "")
            who = f" — {author}, *{title}*" if title else ""
            parts.append(f"> {snip}{who}")

    etym = p.get("etymology")
    if etym:
        primary = etym.get("primary_family")
        chain = etym.get("family_chain") or []
        bits: list[str] = []
        if primary:
            bits.append(str(primary))
        if chain:
            chain_str = " → ".join(str(x) for x in chain if x)
            if primary and chain_str:
                bits.append(f"({chain_str})")
            elif chain_str:
                bits.append(chain_str)
        etym_str = " ".join(bits) if bits else "—"
        parts.append(f"\n**Этимология:** {etym_str}")
    else:
        # Slot explicitly None — say so once, don't hang.
        slots = p.get("slots_available") or {}
        if slots.get("etymology") is False:
            parts.append("\n_Этимология не извлеклась._")

    return "\n".join(parts) + render_caveats(view.caveats)


def _render_recommendation_list(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    items = view.payload.get("items") or []
    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")
    rows = []
    for it in items:
        title = it.get("title", "—")
        pg_id = it.get("pg_id", "")
        author = it.get("author", "—")
        reasons = it.get("reasons", "")
        if isinstance(reasons, list):
            reasons = "; ".join(reasons)
        title_cell = f"{title} ({pg_id})" if pg_id else title
        rows.append([title_cell, author, reasons])
    parts.append(md_table(["Книга", "Автор", "Почему"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_attribution_result(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    cands = p.get("candidates") or []
    metric_name = p.get("primary_metric", "score")
    me = p.get("primary_metric_explanation") or {}
    direction = me.get("direction", "")
    rows = []
    for c in cands[:10]:
        rows.append([
            c.get("author", "—"),
            format_score(c.get("score")),
            format_int(c.get("books_matched")),
        ])
    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")
    if direction:
        parts.append(f"_{metric_name}: {direction}_\n")
    parts.append(md_table([metric_name.capitalize(), "Значение", "Книг"],
                          rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_author_metadata(view: RenderableView) -> str:
    p = view.payload
    name = p.get("author_canonical", "—")
    by = p.get("birth_year")
    dy = p.get("death_year")
    nat = p.get("nationality") or "—"
    books = p.get("books_in_corpus", 0)
    src = p.get("bio_source")

    parts = [f"### {name}\n"]
    rows = [
        ["Годы жизни", f"{by or '?'}–{dy or '?'}"],
        ["Национальность", nat],
        ["Книг в корпусе", format_int(books)],
    ]
    if src:
        rows.append(["Источник", src])
    parts.append(md_table(["", ""], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_book_lookup(view: RenderableView) -> str:
    p = view.payload
    book = p.get("book") or {}
    title = book.get("title", "—")
    pg_id = book.get("pg_id", "")
    author = book.get("author", "")
    pub = book.get("pub_year")
    dl = book.get("downloads")

    head = f"### {title}" + (f" ({pg_id})" if pg_id else "")
    parts = [head, ""]
    rows = [
        ["Автор", author or "—"],
        ["Год публикации", str(pub) if pub else "не указан"],
        ["Загрузок", format_int(dl) if dl is not None else "—"],
    ]
    parts.append(md_table(["", ""], rows))

    cands = p.get("candidates") or []
    if len(cands) > 1:
        parts.append("\n_Также найдено:_")
        for c in cands[1:6]:
            ct = c.get("title", "")
            ca = c.get("author", "")
            cp = c.get("pg_id", "")
            parts.append(f"- {ct} ({cp}), {ca}")

    return "\n".join(parts) + render_caveats(view.caveats)


def _render_author_lookup(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    author = p.get("author_canonical", "—")
    books = p.get("books") or []
    parts = [f"### {author} — книги в корпусе\n"]
    rows = []
    for b in books[:50]:
        rows.append([
            b.get("title", "—"),
            b.get("pg_id", ""),
            str(b.get("pub_year") or ""),
            format_int(b.get("downloads")) if b.get("downloads") is not None else "—",
        ])
    parts.append(md_table(["Название", "PG id", "Год", "Загрузок"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_emotion_profile(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    title = p.get("book_title", "—")
    pg_id = p.get("pg_id", "")
    emos = p.get("emotions") or []
    dom = p.get("dominant") or []

    parts = [f"### Эмоциональный профиль — {title} ({pg_id})\n"]
    if dom:
        parts.append(f"**Доминирующие:** {', '.join(dom)}\n")
    rows = []
    for e in emos:
        rows.append([
            e.get("emotion", "—"),
            format_share(e.get("share")),
            format_int(e.get("count")),
        ])
    parts.append(md_table(["Эмоция", "Доля", "Вхождений"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_learning_words(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    words = p.get("words") or []
    level = p.get("requested_level", "")
    scope = p.get("scope_label", "")

    headline = view.headline or f"Слова уровня {level} — {scope}"
    parts = [f"### {headline}\n"]
    rows = []
    for w in words:
        rows.append([
            w.get("lemma", "—"),
            w.get("translation_ru") or w.get("translation") or "—",
            (w.get("example") or "")[:80],
            w.get("level", level),
        ])
    parts.append(md_table(["Слово", "Перевод", "Пример", "Уровень"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_collocates(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    word = p.get("word", "—")
    cols = p.get("collocates") or []
    scope = p.get("scope_label", "")
    window = p.get("window")

    parts = [f"### Коллокаты — «{word}» ({scope}, окно ±{window})\n"]
    rows = []
    for c in cols[:30]:
        rows.append([
            c.get("token", "—"),
            format_float(c.get("npmi"), digits=3),
            format_int(c.get("count")),
        ])
    parts.append(md_table(["Слово", "NPMI", "Вхождений"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_word_contexts(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    word = p.get("word", "—")
    contexts = p.get("contexts") or []
    scope = p.get("scope_label", "")

    parts = [f"### Контексты — «{word}» ({scope})\n"]
    for ctx in contexts[:5]:
        snip = ctx.get("snippet", "").strip()
        title = ctx.get("title", "")
        author = ctx.get("author", "")
        who = f" — {author}, *{title}*" if title else ""
        parts.append(f"> {snip}{who}")
        parts.append("")
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_timeline_chart(view: RenderableView) -> str:
    if view.empty_state:
        return render_empty_state(view.empty_state, language=view.language)
    p = view.payload
    word = p.get("word", "—")
    series = p.get("series") or []
    basis = p.get("basis", "")

    parts = [f"### Частота слова «{word}» по эпохам (basis={basis})\n"]
    rows = []
    for s in series:
        rows.append([
            f"{s.get('bucket_start', '?')}–{s.get('bucket_end', '?')}",
            format_float(s.get("freq_per_million"), digits=2),
            format_int(s.get("count")),
        ])
    parts.append(md_table(["Период", "Частота (на 1M слов)", "Вхождений"], rows))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_corpus_meta_snapshot(view: RenderableView) -> str:
    p = view.payload
    rows = [
        ["Книг", format_int(p.get("n_books"))],
        ["Авторов", format_int(p.get("n_authors"))],
        ["Токенов", format_int(p.get("n_tokens"))],
        ["SPGC baseline", p.get("spgc_baseline") or "—"],
    ]
    if p.get("chroma_chunks") is not None:
        rows.append(["ChromaDB chunks", format_int(p["chroma_chunks"])])
    if p.get("user_uploads"):
        rows.append(["User-загрузки", format_int(p["user_uploads"])])
    headline = view.headline or "Обзор корпуса"
    return f"### {headline}\n\n" + md_table(["", ""], rows) + render_caveats(view.caveats)


def _render_export_artifact(view: RenderableView) -> str:
    p = view.payload
    fmt = p.get("format", "—")
    fn = p.get("filename_suggestion", "export")
    n = p.get("item_count", 0)
    content = p.get("content", "")
    fence_lang = {
        "anki_csv": "csv", "csv": "csv", "tsv": "tsv",
        "markdown": "markdown", "json": "json",
    }.get(fmt, "")
    parts = [
        f"### Экспорт ({fmt}) — {format_int(n)} элементов",
        f"_Имя файла: `{fn}`_\n",
        f"```{fence_lang}",
        content,
        "```",
    ]
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_introduction(view: RenderableView) -> str:
    p = view.payload
    name = p.get("name", "Словоёб")
    caps = p.get("capabilities") or []
    examples = p.get("examples") or []
    n_books = p.get("corpus_size_books", 0)

    parts = [
        f"### Привет, я {name}.",
        f"\nЯ литературный аналитик корпуса Project Gutenberg "
        f"({format_int(n_books)} книг).",
    ]
    if caps:
        parts.append("\n**Что я умею:**")
        for c in caps:
            parts.append(f"- {c}")
    if examples:
        parts.append("\n**Примеры запросов:**")
        for e in examples:
            parts.append(f"- _{e}_")
    return "\n".join(parts)


def _render_out_of_scope(view: RenderableView) -> str:
    p = view.payload
    why = p.get("why_ru", "")
    what = p.get("what_ru") or []
    which = p.get("which_alternatives") or []

    parts = [why]
    if what:
        parts.append("\n**Что доступно:**")
        for w in what:
            parts.append(f"- {w}")
    if which:
        parts.append("\n**Что можно посмотреть вместо:**")
        for it in which:
            t = it.get("title", "")
            pg = it.get("pg_id", "")
            note = it.get("note", "")
            line = f"- **{t}**"
            if pg:
                line += f" ({pg})"
            if note:
                line += f" — {note}"
            parts.append(line)
    return "\n".join(parts)


def _render_error_friendly(view: RenderableView) -> str:
    p = view.payload
    msg = p.get("message_ru", "")
    hint = p.get("retry_hint_ru")
    partials = p.get("partial_results") or []

    parts = [f"⚠️ {msg}"]
    if hint:
        parts.append(f"\n_{hint}_")
    if partials:
        parts.append("\n**Частичные данные, которые получилось собрать:**")
        for r in partials:
            t = r.get("tool", "")
            summary = r.get("summary", "")
            parts.append(f"- `{t}`: {summary}")
    return "\n".join(parts)


def _render_clarify(view: RenderableView) -> str:
    p = view.payload
    q = p.get("question", "")
    alts = p.get("alternatives") or []
    why = p.get("why")

    parts = []
    if why:
        parts.append(f"_{why}_\n")
    parts.append(f"**{q}**")
    if alts:
        parts.append("")
        for a in alts:
            parts.append(f"- {a}")
    return "\n".join(parts)


def _render_not_found(view: RenderableView) -> str:
    p = view.payload
    etype = p.get("entity_type", "сущность")
    q = p.get("query", "")
    msg = p.get("message_ru", "")
    cands = p.get("candidates") or []

    type_ru = {"author": "автора", "book": "книгу", "word": "слово"}.get(etype, etype)
    parts = [f"Не нашёл {type_ru} по запросу «{q}». {msg}".strip()]
    if cands:
        parts.append("\n**Может быть, ты имел в виду:**")
        for c in cands[:5]:
            disp = c.get("display") or c.get("name") or c.get("title") or "—"
            parts.append(f"- {disp}")
    return "\n".join(parts)


# =====================================================================
# Dispatch table
# =====================================================================

def _render_author_profile(view: RenderableView) -> str:
    """AUTHOR_PROFILE — composite view (metadata + signature + diversity
    + influences). Phase 3 stub: render headline + each section if
    present in payload. Phase 3.5 will refine cross-section layout."""
    p = view.payload
    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")
    name = p.get("author_canonical", "—")
    parts.append(f"**Автор:** {name}\n")
    md = p.get("metadata") or {}
    if md.get("birth_year") or md.get("death_year"):
        parts.append(f"_Годы жизни:_ {md.get('birth_year') or '?'}–"
                     f"{md.get('death_year') or '?'}")
    sig = p.get("signature_words") or []
    if sig:
        parts.append("\n**Фирменные слова:** " + ", ".join(f"`{w}`" for w in sig[:30]))
    diversity = p.get("lexical_diversity")
    if diversity is not None:
        parts.append(f"\n**Лексическое разнообразие (TTR):** {format_float(diversity, digits=3)}")
    infl = p.get("influences") or []
    if infl:
        parts.append("\n**Возможные стилистические родственники:** "
                     + ", ".join(str(x) for x in infl[:10]))
    return "\n".join(parts) + render_caveats(view.caveats)


def _render_vocab_passport(view: RenderableView) -> str:
    """VOCAB_PASSPORT — composite per-book lexical fingerprint. Stub
    render; expanded in Phase 3.5."""
    p = view.payload
    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")
    scope = p.get("scope_label", "—")
    parts.append(f"**Источник:** {scope}\n")
    if p.get("total_words"):
        parts.append(f"_Слов всего:_ {format_int(p['total_words'])}")
    if p.get("unique_lemmas"):
        parts.append(f"_Уникальных лемм:_ {format_int(p['unique_lemmas'])}")
    if p.get("ttr") is not None:
        parts.append(f"_TTR:_ {format_float(p['ttr'], digits=3)}")
    sections = p.get("sections") or []
    for sec in sections:
        title = sec.get("title", "")
        rows = sec.get("rows") or []
        if rows and title:
            parts.append(f"\n**{title}:**")
            cols = sec.get("columns") or list(rows[0].keys())
            parts.append(md_table(cols, [[r.get(c, "—") for c in cols] for r in rows]))
    return "\n".join(parts) + render_caveats(view.caveats)


VIEW_RENDERERS: dict[ViewType, Callable[[RenderableView], str]] = {
    ViewType.TOP_N_TABLE:           _render_top_n_table,
    ViewType.COMPARISON_PANEL:      _render_comparison_panel,
    ViewType.READABILITY_SUMMARY:   _render_readability_summary,
    ViewType.ETYMOLOGY_BUNDLE:      _render_etymology_bundle,
    ViewType.RECOMMENDATION_LIST:   _render_recommendation_list,
    ViewType.ATTRIBUTION_RESULT:    _render_attribution_result,
    ViewType.AUTHOR_METADATA:       _render_author_metadata,
    ViewType.BOOK_LOOKUP:           _render_book_lookup,
    ViewType.AUTHOR_LOOKUP:         _render_author_lookup,
    ViewType.EMOTION_PROFILE:       _render_emotion_profile,
    ViewType.LEARNING_WORDS:        _render_learning_words,
    ViewType.COLLOCATES:            _render_collocates,
    ViewType.WORD_CONTEXTS:         _render_word_contexts,
    ViewType.TIMELINE_CHART:        _render_timeline_chart,
    ViewType.CORPUS_META_SNAPSHOT:  _render_corpus_meta_snapshot,
    ViewType.EXPORT_ARTIFACT:       _render_export_artifact,
    ViewType.AUTHOR_PROFILE:        _render_author_profile,
    ViewType.VOCAB_PASSPORT:        _render_vocab_passport,
    ViewType.INTRODUCTION:          _render_introduction,
    ViewType.OUT_OF_SCOPE:          _render_out_of_scope,
    ViewType.ERROR_FRIENDLY:        _render_error_friendly,
    ViewType.CLARIFY:               _render_clarify,
    ViewType.NOT_FOUND:             _render_not_found,
}


# =====================================================================
# Public API
# =====================================================================

def render_view(view: RenderableView) -> str:
    """Deterministically render a view to markdown.

    Same input → same output, byte-for-byte. No LLM. No fabrication
    possible — every cell comes from view.payload.

    Phase 3 step B (LLM ProseBinder) wraps this: takes rendered
    skeleton + question, generates short intro/next-step prose, then
    audits prose against view.payload before output. If any number or
    name in prose isn't in payload → strip prose, keep skeleton.
    """
    issues = view.validate()
    if issues:
        log.error("render_view: invalid view %s: %s", view.view_type, issues)
        return f"⚠️ Internal: invalid view ({'; '.join(issues)})"

    if view.view_type == ViewType.BUNDLE:
        # Composite — payload has {"sub_views": [...]} of RenderableView dicts.
        # We don't reconstruct dataclasses here in Phase 0; bundle rendering
        # is added in Phase 3.5 once we know which composites need it.
        return _render_bundle_stub(view)

    fn = VIEW_RENDERERS.get(view.view_type)
    if fn is None:
        log.warning("render_view: no renderer for %s", view.view_type)
        return f"_(view_type={view.view_type.value} not yet supported)_"
    return fn(view)


def _render_bundle_stub(view: RenderableView) -> str:
    """Bundle composite — Phase 3 partial. For now, render
    headline + caveats; sub-views need to be RenderableView instances
    (Phase 3.5 will support nested rendering)."""
    parts = []
    if view.headline:
        parts.append(f"### {view.headline}\n")
    sub_views = view.payload.get("sub_views") or []
    for sv in sub_views:
        if isinstance(sv, RenderableView):
            parts.append(render_view(sv))
            parts.append("")
        else:
            parts.append("_(bundle sub-view: dict-form, render in Phase 3.5)_")
    return "\n".join(parts) + render_caveats(view.caveats)


# Module-level marker
V5_TEMPLATE_EXECUTOR_VERSION = "0.1"


__all__ = [
    "V5_TEMPLATE_EXECUTOR_VERSION",
    "VIEW_RENDERERS",
    "render_view",
    "format_int", "format_float", "format_year_range",
    "format_score", "format_share",
    "md_table",
    "render_empty_state", "render_caveats",
]
