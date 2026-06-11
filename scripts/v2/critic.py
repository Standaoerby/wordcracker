"""Critic pass — second-LLM verification of the rendered answer.

After the renderer emits its prose answer, we run a short low-temperature
critic call that sees:
  * the renderer's final text
  * the structured tool_results it should have been quoting from
  * the plan intent

Critic returns a JSON verdict:
  {
    "verified": bool,
    "unsupported_claims": [str, ...],   # short paraphrase of suspect claims
    "missing_caveats":   [str, ...],    # coverage / corpus gaps the answer
                                        # glossed over
    "summary":           str            # 1-sentence overall assessment
  }

The rag_v2 pipeline appends a "⚠️ Проверь" block to the answer when
unsupported_claims is non-empty. Numeric and named-entity claims are the
highest-value targets — those are what produced the v1.0e hallucination
bug ("PG1327 = Crime and Punishment").

Cost: ~3-5 s extra per query, gated on env WC_CRITIC=on (default on for v2).
Failures (network / JSON parse) fall through silently — the un-critiqued
answer still ships.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import requests

log = logging.getLogger("wordcracker.v2.critic")

CRITIC_MODEL = os.environ.get("WC_CRITIC_MODEL", os.environ.get(
    "WC_LLM_MODEL", "qwen3:14b"))
CRITIC_ENABLED = os.environ.get("WC_CRITIC", "on").lower() in ("on", "1", "true")
CRITIC_TIMEOUT_S = int(os.environ.get("WC_CRITIC_TIMEOUT", "30"))


_CRITIC_PROMPT = """Ты — критик-верификатор. Получи ответ + tool_results на котором он построен. Твоя задача — найти ТОЛЬКО ОТКРОВЕННО ВЫДУМАННЫЕ утверждения. ОЧЕНЬ ВАЖНО: ложные срабатывания дороже пропусков. Если не уверен — НЕ флагай.

ДЕФОЛТ: verified=true. Флагай ТОЛЬКО при чёткой фальсификации.

ПРИЗНАКИ ФАЛЬСИФИКАЦИИ (когда обязательно флагать):
- Число которого ВООБЩЕ нет ни в одном поле tool_results.data (типа «у Толстого 250 книг» когда tool_results показывают 47).
- PG id типа «PG12345» которого нет ни в одном tool_result.
- Книга названа канонически (e.g. "Anna Karenina") но её НЕТ в matches / data — а ответ говорит что есть.
- Цитата в кавычках которая не присутствует в samples / contexts / snippet любого tool.
- Ответ заявляет ОПЕРАЦИЮ (фильтрацию, исключение, сортировку), которой нет ни в tool args (query), ни в data (proper_noun_filter / _render_note). Пример: «после фильтрации имён собственных и русских фамилий», когда в query только pos_filter и data не сообщает о дропах имён. Заявленная-но-не-выполненная операция = фабрикация.

НЕ ФЛАГАЙ (data echo — это нормально):
- Любую таблицу из tool_data — даже если ответ перечисляет 20 строк, считай таблицу одним agreement с data.
- Числа из data в перифразе («Holmes встречается 4045 раз» → ответ «4 тысячи раз» — это compression, не fabrication).
- Стилистические комментарии («это типично для викторианцев», «голос Wodehouse») если они общие.
- Заявления о coverage если в data есть coverage info.
- Названия инструментов и intent в ответе.
- Любые цифры внутри tool's `data` поля, даже если они в nested objects.

Верни ТОЛЬКО JSON без markdown, без объяснений:
{
  "verified": true|false,
  "unsupported_claims": [],   // максимум 2 строки; пустой массив = чисто
  "missing_caveats": [],
  "summary": "1 предложение"
}

Empty arrays = clean answer = verified true. Это наиболее частый случай. Не ищи проблем где их нет."""


@dataclass
class CriticVerdict:
    verified: bool
    unsupported_claims: list[str]
    missing_caveats: list[str]
    summary: str
    raw_text: str = ""  # for debug
    # Sprint 17 — Ollama-side token counts so the admin dashboard can
    # see «critic prompt size» distribution. None when critic didn't
    # actually call the LLM (trust / skip paths).
    prompt_tokens: int | None = None
    eval_tokens: int | None = None

    @classmethod
    def trust(cls) -> "CriticVerdict":
        """Pass-through verdict — use when critic is disabled or skipped."""
        return cls(verified=True, unsupported_claims=[],
                   missing_caveats=[], summary="(critic disabled)")

    def has_issues(self) -> bool:
        return bool(self.unsupported_claims) or bool(self.missing_caveats)


def _build_payload_for_critic(answer: str, tool_results_summary: list[dict],
                              intent: str) -> dict:
    """Compact context window for the critic — keep it small so we don't
    burn 4k tokens just on prior tool dumps.

    B119 (R-28) — cap raised 8 → 12 (= RequestBudget.tool_calls_max).
    The learning_books plan is 11 results (top_books pool + 10 ×
    book_readability); the old [:8] silently dropped the readability of
    ranks 7-9, so the critic reviewed against a partial trusted set.
    """
    compact_results = []
    for r in tool_results_summary[:12]:
        compact_results.append({
            "tool": r.get("tool"),
            "ok": r.get("ok", True),
            "data": _shrink_table_aware(r.get("data"), max_chars=600),
            "coverage": r.get("coverage"),
            "warnings": [w.get("message") for w in (r.get("warnings") or [])
                         if isinstance(w, dict)][:3],
        })
    return {
        "intent": intent,
        "tool_results": compact_results,
        "answer": answer[:3000],
    }


def _shrink(value, *, max_chars: int) -> object:
    """Return a json-safe summary that fits in max_chars."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        s = str(value)
    if len(s) <= max_chars:
        return value
    return s[:max_chars] + "...(truncated)"


# B119 (R-28) — row fields the critic verifies entity claims against.
# Keep names (title/author/word) and the headline metrics; drop verbose
# stats so a 10-row table fits where the blind 600-char cut used to
# chop the JSON string mid-table (rank 5 «Pride and Prejudice» fell off
# the end → critic flagged a REAL book as fabricated in smoke 2.7.8
# S1–S3).
_CRITIC_ROW_KEYS = ("rank", "pg_id", "id", "title", "author", "word",
                    "lemma", "downloads", "cefr_heuristic",
                    "flesch_reading_ease")
_CRITIC_TABLE_KEYS = ("top", "results", "matches", "rows")


def _shrink_table_aware(value, *, max_chars: int) -> object:
    """Table-aware variant of `_shrink` for critic payloads.

    For dict data carrying a list-of-dicts table (top / results /
    matches / rows) keep EVERY row but only its name-bearing +
    headline-metric fields, so the critic always sees the full set of
    titles/authors/words the renderer echoed. Non-table data falls back
    to the plain char-cap (`_render_note` is dropped first — it's a
    renderer instruction, not evidence).
    """
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "_render_note"}
        for key in _CRITIC_TABLE_KEYS:
            rows = value.get(key)
            if (isinstance(rows, list) and rows
                    and all(isinstance(r, dict) for r in rows[:3])):
                compact_rows = [
                    {k: _shrink(r.get(k), max_chars=80)
                     for k in _CRITIC_ROW_KEYS if r.get(k) is not None}
                    for r in rows[:20] if isinstance(r, dict)
                ]
                scalars = {
                    k: _shrink(v, max_chars=120)
                    for k, v in value.items()
                    if k not in _CRITIC_TABLE_KEYS
                    and not isinstance(v, (list, dict))
                }
                return {**scalars, key: compact_rows}
    return _shrink(value, max_chars=max_chars)


# B119 (R-28) — deterministic trusted-set enforcement. The LLM critic
# kept flagging «Pride and Prejudice» as fabricated while the title sat
# in tool data (rank 5 of top_books + its own book_readability result).
# Before a claim ships to the user, extract the entities it names and
# check them against the FULL (unshrunken) tool records: an entity that
# IS in tool data cannot support an «unsupported claim» — drop it.
# Claims naming entities absent from the data (real fabrications) pass
# through unchanged.
#
# R5 — both regexes have positive + negative cases in
# tests/v2/test_r28_learning_polish.py.
_CLAIM_QUOTED_RE = re.compile(r"[«\"“]([^«»\"“”]{3,80})[»\"”]")
_CLAIM_PG_ID_RE = re.compile(r"\bPG\d{1,7}\b", re.IGNORECASE)
# ≥2-word Latin TitleCase run (connectors allowed): «Pride and
# Prejudice», «A Tale of Two Cities». Single capitalized words are NOT
# extracted — too noisy (Flesch, CEFR, Anna…); single-word titles are
# still caught when the claim quotes them.
_CLAIM_TITLECASE_RE = re.compile(
    r"\b[A-Z][a-z]+(?:['’][a-z]+)?"
    r"(?:\s+(?:of|the|and|a|an|in|on|to|"
    r"[A-Z][a-z]+(?:['’][a-z]+)?)){1,7}\b")


def _claim_entity_candidates(claim: str) -> list[str]:
    """Entity-ish substrings a claim names: quoted spans, PG ids,
    multi-word Latin TitleCase runs. Deduped, order preserved."""
    cands: list[str] = []
    cands += [m.group(1).strip() for m in _CLAIM_QUOTED_RE.finditer(claim)]
    cands += _CLAIM_PG_ID_RE.findall(claim)
    cands += [m.group(0).strip() for m in _CLAIM_TITLECASE_RE.finditer(claim)]
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        cl = c.lower()
        if cl and cl not in seen:
            seen.add(cl)
            out.append(c)
    return out


def filter_claims_with_data_evidence(
    claims: list, tool_records: list[dict],
) -> tuple[list, list]:
    """Split critic claims into (kept, suppressed_by_evidence).

    A claim is suppressed when ANY entity it names occurs (case-
    insensitive substring) in the serialized data/query of ANY tool
    record — the full records, not the shrunken critic payload.
    Claims naming nothing recognizable are kept (numbers-only claims
    stay the LLM's call)."""
    blob_parts: list[str] = []
    for rec in tool_records or []:
        if not isinstance(rec, dict):
            continue
        for key in ("data", "query"):
            try:
                blob_parts.append(
                    json.dumps(rec.get(key), ensure_ascii=False,
                               default=str))
            except Exception:
                blob_parts.append(str(rec.get(key)))
    blob = " ".join(blob_parts).lower()
    kept: list = []
    suppressed: list = []
    for c in claims:
        cands = _claim_entity_candidates(str(c))
        if cands and any(cand.lower() in blob for cand in cands):
            suppressed.append(c)
        else:
            kept.append(c)
    return kept, suppressed


# Sprint 11.1 + Sprint 17 extension: skip critic entirely for intents
# that are inherently table-data echoes — the renderer just copies rows
# from tool_results, the critic adds 3-5s of LLM latency without
# catching real hallucinations. Sprint 16 Phase D's programmatic numeric
# audit now catches the highest-value class of fabrications (made-up
# counts) deterministically, so the LLM critic's role on tabular intents
# is largely redundant.
_INTENT_SKIP_CRITIC = {
    # learning_words returns ranked rows; renderer literally copies them
    "learning",
    # top_authors_by / top_books_by_downloads: pure table
    "top_authors_books",
    # learning vocab passport already heavy-cached + audited
    "vocab_passport",
    # Sprint 17 extension — Phase E/F/G intents that are pure table echo:
    # author_lookup    → author_metadata.sample_titles (list of strings)
    # corpus_extremum  → top_authors_by(top=1) (1-row table)
    # book_extremum    → top_books_by_downloads(top=1) or clarify
    # topic_book_search → find_book_by_topic (dedupped book list)
    # book_pub_year    → find_book.matches[0].pub_year (single value)
    # book_lookup      → find_book.matches (title resolution table)
    # Numeric audit (Phase D) catches the fabricated-count failure mode
    # on these intents programmatically; LLM critic was just noise.
    "author_lookup",
    "corpus_extremum",
    "book_extremum",
    "topic_book_search",
    "book_pub_year",
    "book_lookup",
    # Sprint 17: book_similar → find_book_by_topic returns a deduped
    # book list (same table-echo shape as topic_book_search).
    "book_similar",
}


def review(answer: str, tool_results_summary: list[dict], *,
           intent: str = "unknown",
           model: str = CRITIC_MODEL,
           ollama_host: str | None = None) -> CriticVerdict:
    """Run the critic call. Returns trust() on any failure path.

    Skips the LLM round-trip entirely for table-data intents (Sprint 11.1
    add) — the critic over-flagged tabular answers in bench v2.0.7
    (23/32 noise vs ~3 real catches). For those intents the planner +
    tool envelope already gates correctness; the second LLM pass added
    cost without benefit.
    """
    if not CRITIC_ENABLED:
        return CriticVerdict.trust()
    if not answer or not answer.strip():
        return CriticVerdict.trust()
    if not tool_results_summary:
        # Pure no-tool answers (introduction, clarify, out_of_scope) — nothing
        # to verify against.
        return CriticVerdict.trust()
    if intent in _INTENT_SKIP_CRITIC:
        return CriticVerdict(
            verified=True, unsupported_claims=[], missing_caveats=[],
            summary=f"(critic skipped for table intent: {intent})",
        )

    host = ollama_host or os.environ.get("OLLAMA_HOST", "http://ollama:11434")
    payload_for_critic = _build_payload_for_critic(
        answer, tool_results_summary, intent)

    # Sprint 22+ alpha5 — Token Budget Layer. Critic was overflow-prone
    # on large tool_results just like the renderer (Stan Christie case
    # had critic clean ⇒ critic also confabulating). Now adaptive
    # shrink before the request goes to Ollama, plus explicit num_ctx.
    from scripts.v2.token_budget import TokenBudget
    budget = TokenBudget(model=model)
    payload_for_critic, shrink_report = budget.shrink_to_fit(payload_for_critic)
    if shrink_report.actions:
        log.info("critic shrink applied: %s (initial=%d → final=%d, "
                 "util=%d%%)",
                 shrink_report.actions, shrink_report.initial_tokens,
                 shrink_report.final_tokens,
                 shrink_report.utilization_pct())
    if not shrink_report.fits:
        log.warning("critic payload over budget after ladder: "
                    "est=%d, budget=%d (still attempting)",
                    shrink_report.final_tokens, shrink_report.budget)

    messages = [
        {"role": "system", "content": _CRITIC_PROMPT},
        {"role": "user", "content": "Контекст:\n```json\n"
                                    + json.dumps(payload_for_critic,
                                                 ensure_ascii=False, default=str)
                                    + "\n```"},
    ]
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.1, "num_ctx": budget.ctx},
        "think": False,
        "format": "json",
    }
    try:
        resp = requests.post(f"{host}/api/chat", json=body,
                             timeout=CRITIC_TIMEOUT_S)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning("critic call failed: %s", e)
        return CriticVerdict.trust()

    body = resp.json() or {}
    try:
        from scripts.v2.observability import log_llm_latency
        log_llm_latency("critic", model, budget.ctx, body)
    except Exception:
        pass
    content = (body.get("message") or {}).get("content", "")
    # Sprint 17 — surface Ollama prompt/eval token counts for the admin
    # dashboard. Even when the verdict parses as trust, we keep the
    # tokens (the call still cost VRAM + time).
    critic_prompt_tokens = body.get("prompt_eval_count")
    critic_eval_tokens = body.get("eval_count")
    if not content.strip():
        v = CriticVerdict.trust()
        v.prompt_tokens = critic_prompt_tokens
        v.eval_tokens = critic_eval_tokens
        return v
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        log.warning("critic JSON parse failed: %s; raw=%r", e, content[:200])
        v = CriticVerdict.trust()
        v.prompt_tokens = critic_prompt_tokens
        v.eval_tokens = critic_eval_tokens
        return v

    unsupported = list(parsed.get("unsupported_claims") or [])
    caveats = list(parsed.get("missing_caveats") or [])
    # B119 (R-28) — deterministic evidence check against the FULL
    # records before any flag reaches the user: a claim naming an
    # entity that IS in tool data is a critic false-positive («волки-
    # волки» on Pride and Prejudice, smoke 2.7.8 S1–S3), not a catch.
    unsupported, evidence_suppressed = filter_claims_with_data_evidence(
        unsupported, tool_results_summary)
    if evidence_suppressed:
        log.info("critic: %d claim(s) suppressed — named entities found "
                 "in tool data: %r",
                 len(evidence_suppressed),
                 [str(c)[:120] for c in evidence_suppressed])
    # Sanity guard: when the critic flags >= MAX_FLAGS items, it's almost
    # certainly confused (the renderer just pulled rows from a table, every
    # number is "unsupported" from a strict perspective). Trust the answer
    # and surface only the count via summary so the UI badge stays useful.
    # Sprint 11.1: tightened to 2 (was 4) so even one borderline case
    # treats the critic as confused. Real hallucinations almost always
    # produce exactly 1 distinct flag (the one fabricated number); 3+ is
    # a critic-quality issue, not the renderer.
    MAX_FLAGS = 2
    if len(unsupported) > MAX_FLAGS:
        log.info("critic over-flagged %d claims for intent=%s — guarding to trust",
                 len(unsupported), payload_for_critic.get("intent"))
        return CriticVerdict(
            verified=True, unsupported_claims=[], missing_caveats=caveats,
            summary=(f"({len(unsupported)} weak flags suppressed — "
                     f"answer renders a table; treat numbers as data echo)"),
            raw_text=content[:500],
            prompt_tokens=critic_prompt_tokens,
            eval_tokens=critic_eval_tokens,
        )
    verified = bool(parsed.get("verified", True))
    if evidence_suppressed and not unsupported:
        # B119 — the LLM's «false» was carried by claims that didn't
        # survive the evidence check; don't ship a red badge with zero
        # remaining claims.
        verified = True
    return CriticVerdict(
        verified=verified,
        unsupported_claims=unsupported,
        missing_caveats=caveats,
        summary=str(parsed.get("summary") or "(no summary)"),
        raw_text=content[:500],
        prompt_tokens=critic_prompt_tokens,
        eval_tokens=critic_eval_tokens,
    )


def annotate_answer(answer: str, verdict: CriticVerdict) -> str:
    """Append a ⚠️ block to the answer if the critic flagged issues.

    Stays out of the way when verified — no decoration."""
    if verdict.verified and not verdict.has_issues():
        return answer
    lines = [answer.rstrip(), "", "---", ""]
    if verdict.unsupported_claims:
        lines.append("⚠️ **Проверь следующие утверждения** — они не подкреплены явными данными tool:")
        for c in verdict.unsupported_claims[:5]:
            lines.append(f"- {c}")
        lines.append("")
    if verdict.missing_caveats:
        lines.append("ℹ️ **Не упомянутые ограничения покрытия:**")
        for m in verdict.missing_caveats[:3]:
            lines.append(f"- {m}")
        lines.append("")
    lines.append(f"_Critic: {verdict.summary}_")
    return "\n".join(lines)


# =====================================================================
# WP2b (R-27, 2026-06-10) — claimed-but-not-performed operation guard.
#
# Repro af384edfae2d / B109: affinity_by_author was called with ONLY
# pos_filter, yet the renderer wrote «после фильтрации имён собственных
# и русских фамилий» — claiming a name/surname filter that no tool
# performed. The LLM critic prompt now lists this as a fabrication sign
# (probabilistic); this deterministic pass is the enforcing back-stop:
# the claim is excised from the answer BEFORE it ships (suppression,
# not warn-only).
#
# Evidence model — a name/surname-filter claim is SUPPORTED iff:
#   * any tool's args (`query`) reference such a filter
#     (propn / surname / exclude_names / name_filter keys), OR
#   * any tool's `data` explicitly discloses name-filter drops —
#     affinity_by_author emits `proper_noun_filter` («v2 surname
#     blocklist dropped N…») and a `_render_note` mentioning «фильтра
#     имён собственных» when its built-in PROPN pipeline dropped rows.
# pos_filter / min_corpus_count are NOT evidence of name filtering.
# =====================================================================

# Claim shapes: «после фильтрации имён/фамилий», «исключены имена
# собственные», «отфильтрованы русские фамилии», EN variants.
_OP_CLAIM_NAME_FILTER_RE = re.compile(
    r"(?:после\s+фильтрации[^.!?\n]{0,60}?(?:им[ёе]н|фамили)|"
    r"фильтраци\w+[^.!?\n]{0,40}?(?:им[ёе]н|фамили)|"
    r"(?:исключ\w+|отфильтрова\w+|убра\w+|удал[ёе]?н\w*)[^.!?\n]{0,40}?"
    r"(?:имена?\s+собственн\w+|им[ёе]н\w*\s+собственн\w+|фамили\w+)|"
    r"(?:имена\s+собственные|фамилии)[^.!?\n]{0,40}?"
    r"(?:исключ\w+|отфильтрован\w+|убран\w+|удал[ёе]н\w+)|"
    r"filter(?:ed|ing)?\s+(?:out\s+)?proper\s+(?:names|nouns)|"
    r"(?:proper\s+(?:names|nouns)|surnames)\s+(?:were\s+|have\s+been\s+)?"
    r"(?:filtered|excluded|removed))",
    re.IGNORECASE | re.UNICODE,
)

# Evidence tokens in serialized tool args / data that make the claim true.
_NAME_FILTER_EVIDENCE_RE = re.compile(
    r"propn|proper_noun|surname|exclude_names|name_filter|"
    r"фильтра?\s+им[ёе]н|им[ёе]н\w*\s+собственн",
    re.IGNORECASE | re.UNICODE,
)


@dataclass
class OperationClaimReport:
    """Unsupported operation claims found in the rendered answer."""
    claims: list[str]

    def has_issues(self) -> bool:
        return bool(self.claims)


def _records_carry_name_filter_evidence(tool_records) -> bool:
    for rec in tool_records or []:
        if not isinstance(rec, dict):
            continue
        for key in ("query", "data"):
            try:
                blob = json.dumps(rec.get(key), ensure_ascii=False,
                                  default=str)
            except Exception:
                blob = str(rec.get(key))
            if blob and _NAME_FILTER_EVIDENCE_RE.search(blob):
                return True
    return False


def audit_operation_claims(answer: str,
                           tool_records: list[dict]) -> OperationClaimReport:
    """Find filter/operation claims in `answer` that no tool performed.

    Deterministic — no LLM call. Currently covers the name/surname-filter
    claim class (the af384edfae2d / B109 prod bug); extend the claim
    regex when a new claimed-operation class shows up in feedback."""
    rep = OperationClaimReport(claims=[])
    if not answer or not answer.strip():
        return rep
    matches = list(_OP_CLAIM_NAME_FILTER_RE.finditer(answer))
    if not matches:
        return rep
    if _records_carry_name_filter_evidence(tool_records):
        return rep  # claim is backed by args or data disclosure — honest
    for m in matches[:3]:
        rep.claims.append(m.group(0).strip())
    return rep


def suppress_operation_claims(answer: str,
                              report: OperationClaimReport) -> str:
    """Excise sentences carrying unsupported operation claims and append
    an honest disclosure instead. The disclosure deliberately does NOT
    quote the removed claim — re-quoting would put the false statement
    back into the answer."""
    if not report.has_issues():
        return answer
    from scripts.v2.numeric_audit import _excise_sentence
    repaired = answer
    removed = 0
    for claim in report.claims:
        cut, ok = _excise_sentence(repaired, claim)
        if ok:
            repaired = cut
            removed += 1
    if not removed:
        return answer
    lines = [repaired.rstrip(), "", "---", "",
             "🔧 **Honesty guard** — из ответа удалено утверждение о "
             "фильтрации имён/фамилий: инструменты такую операцию в этом "
             "запросе не выполняли (её нет в tool calls/args). Если нужна "
             "такая фильтрация — она пока не поддерживается; в списке "
             "могут встречаться имена собственные."]
    return "\n".join(lines)


# =====================================================================
# R-27 WP3 (2026-06-11) — claimed-vs-shown name-filter guard.
#
# Q7/Q8 live repro (prod 2.7.3): the answer said «после фильтрации имён
# собственных … осталось 24» while the visible table carried hélène,
# sergius, petrovna, hippolyte, nicholas. The WP2b guard above could not
# catch it: the claim WAS backed by a data disclosure (word_dict drops),
# so per rule 21 it survived — «правда, что фильтровали — ложь, что
# отфильтровали». This pass closes that nuance deterministically: when
# the answer CLAIMS a name/proper-noun filter, run the SAME pure name
# detector the tools use (scripts.v2.tools.authors._propn_gazetteer)
# over the words actually shown in the answer's markdown tables. Any
# hit → the assertive claim is excised (WP2b suppression mechanics) and
# an honest «фильтр имён частичный» disclosure is appended. No LLM —
# pure function, unit-testable.
# =====================================================================

# Sentence-level escape hatch: a claim that ALREADY discloses partiality
# («применён частичный фильтр…», «могли остаться имена») is honest and
# must not be excised.
_PARTIAL_DISCLOSURE_RE = re.compile(
    r"частичн|могли\s+остаться|могут\s+(?:встречаться|оставаться)|partial",
    re.IGNORECASE | re.UNICODE,
)

# Markdown table plumbing — same line-shape detection numeric_audit uses.
_MD_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?[-=]{2,}\s*[:|]?")
# A cell token that can be a "word" in our word lists: letters (latin or
# cyrillic, accents allowed), apostrophe/hyphen, 2+ chars.
_WORD_CELL_RE = re.compile(r"^[A-Za-zÀ-ÿЀ-ӿ][A-Za-zÀ-ÿЀ-ӿ'’-]+$")


@dataclass
class ClaimedVsShownReport:
    """Name-filter claims contradicted by names visible in the answer."""
    claims: list[str]
    shown_names: list[str]

    def has_issues(self) -> bool:
        return bool(self.claims and self.shown_names)


def _visible_table_words(answer: str) -> list[str]:
    """Word-like tokens from the answer's markdown table cells.

    Strips markdown emphasis/backticks per cell; keeps only single
    word-shaped tokens (the shape affinity/learning lists render).
    Numeric cells, headers like «Слово»/«rank» and multi-word prose
    cells fall out naturally via the shape check + detector."""
    words: list[str] = []
    seen: set[str] = set()
    for line in _MD_TABLE_LINE_RE.findall(answer):
        if _MD_TABLE_SEP_RE.match(line):
            continue
        for cell in line.strip().strip("|").split("|"):
            token = cell.strip().strip("*_`").strip()
            if not token or token in seen:
                continue
            if _WORD_CELL_RE.match(token):
                seen.add(token)
                words.append(token)
    return words


def audit_claimed_vs_shown(answer: str) -> ClaimedVsShownReport:
    """Deterministic «заявлено vs показано»: name-filter claim in the
    text + name detector over the visible table rows. Pure function."""
    rep = ClaimedVsShownReport(claims=[], shown_names=[])
    if not answer or not answer.strip():
        return rep
    matches = list(_OP_CLAIM_NAME_FILTER_RE.finditer(answer))
    if not matches:
        return rep
    from scripts.v2.tools.authors._propn_gazetteer import find_proper_names
    shown = find_proper_names(_visible_table_words(answer))
    if not shown:
        return rep
    for m in matches[:3]:
        # Locate the claim's sentence; an already-partial disclosure is
        # honest — skip it.
        start = answer.rfind("\n", 0, m.start())
        for brk in ".!?…":
            cut = answer.rfind(brk, 0, m.start())
            if cut > start:
                start = cut
        end_candidates = [answer.find(brk, m.end()) for brk in ".!?…\n"]
        end_candidates = [e for e in end_candidates if e != -1]
        end = min(end_candidates) if end_candidates else len(answer)
        sentence = answer[start + 1:end]
        if _PARTIAL_DISCLOSURE_RE.search(sentence):
            continue
        rep.claims.append(m.group(0).strip())
    if rep.claims:
        rep.shown_names = shown[:10]
    return rep


def suppress_partial_filter_claims(answer: str,
                                   report: ClaimedVsShownReport) -> str:
    """Excise assertive name-filter claims contradicted by the visible
    rows; append the honest partial-filter disclosure (WP2b mechanics)."""
    if not report.has_issues():
        return answer
    from scripts.v2.numeric_audit import _excise_sentence
    repaired = answer
    removed = 0
    for claim in report.claims:
        cut, ok = _excise_sentence(repaired, claim)
        if ok:
            repaired = cut
            removed += 1
    if not removed:
        return answer
    sample = ", ".join(report.shown_names[:5])
    lines = [repaired.rstrip(), "", "---", "",
             "🔧 **Honesty guard** — из ответа удалено утверждение о "
             "полной фильтрации имён: в показанном списке остались имена "
             f"({sample}). Фильтр имён частичный — в списке могли "
             "остаться имена."]
    return "\n".join(lines)


# =====================================================================
# R-28 B114 (2026-06-11) — учебный факт без tool-опоры не публикуется.
#
# Тест-ран Q4 + смоук S4: в учебных word-бандлах LLM выдумывал
# этимологию (ajar/garlic «от греческого krokos») и переводы
# (galatz→«стеклянный сосуд»). Источник — enrich_word (FIXTURE_EXEMPT,
# контент генерится LLM): фабрикация входила в ответ через ДАННЫЕ
# инструмента, поэтому evidence-модель WP2b её не ловила («данные
# подтверждают» клейм). Два детерминированных пасса ниже закрывают это
# на уровне ответа (suppress, не warn — механика WP2b):
#
#   * audit_etymology_claims — этимологическое утверждение в ответе
#     поддержано ТОЛЬКО выходом word_etymology/find_words_by_etymology
#     (Wiktionary-backed family_chain/raw_codes/primary_family).
#     enrich_word-поля этимологии стираются на враппере и опорой не
#     считаются. Честная констатация отсутствия («Этимологии этого
#     слова в данных корпуса нет») не вырезается.
#   * audit_example_quotes — blockquote-цитаты (примеры употребления)
#     должны иметь корпусную опору в данных инструментов; сочинённый
#     LLM пример вырезается построчно.
#
# R5 — оба класса регексов имеют позитивные И негативные кейсы в
# tests/v2/test_r28_honest_learning.py.
# =====================================================================

# Claim shapes: «Этимология: …», «происходит от греческого/латинского…»,
# «восходит к прагерманскому корню», «заимствовано из…», EN variants.
# Verb-anchored: голое «from Greek» / «происходит от лица рассказчика»
# (повествовательный смысл) совпадать НЕ должно — негативные кейсы в R5.
_ETYM_CLAIM_RE = re.compile(
    r"(?:этимолог\w*|"
    r"происходит\s+от\s+(?:греч|лат|древн|стар[оа]|франц|герман|англ"
    r"|прото|санскрит|слова|корня)\w*|"
    r"восходит\s+к\s+(?:греч|лат|древн|стар[оа]|франц|герман|англ"
    r"|прото|санскрит|корн|слов)\w*|"
    r"заимствован\w*\s+(?:из|от)\s|"
    r"etymolog\w*|"
    r"(?:derive[sd]?|originate[sd]?|comes?|borrowed)\s+from\s+"
    r"(?:the\s+)?(?:ancient\s+)?(?:greek|latin|old|middle|proto|sanskrit"
    r"|french|german|norse))",
    re.IGNORECASE | re.UNICODE,
)

# Честная констатация отсутствия — не клейм, не вырезается. Канонический
# текст рендера: «Этимологии этого слова в данных корпуса нет».
_ETYM_ABSENCE_RE = re.compile(
    r"(?:в\s+данных\s+корпуса\s+нет|не\s+извлек|не\s+указан|недоступн"
    r"|отсутству|нет\s+данных|no\s+etymology|not\s+(?:available|found))",
    re.IGNORECASE | re.UNICODE,
)

# Tool-опора этимологии: только Wiktionary-backed инструменты.
# enrich_word сюда НЕ входит — его этимология была LLM-генерацией и
# стирается на враппере (scripts/v2/tools/learning/enrich.py).
_ETYM_EVIDENCE_TOOLS = frozenset({"word_etymology", "find_words_by_etymology"})
# Непустой family_chain/raw_codes (списки) или primary_family (строка)
# в сериализованных data.
_ETYM_EVIDENCE_RE = re.compile(
    r"\"(?:family_chain|raw_codes)\":\s*\[\s*\"|\"primary_family\":\s*\"\w")


@dataclass
class EtymologyClaimReport:
    """Etymology claims in the answer with no word_etymology backing."""
    claims: list[str]

    def has_issues(self) -> bool:
        return bool(self.claims)


def _records_carry_etymology_evidence(tool_records) -> bool:
    for rec in tool_records or []:
        if not isinstance(rec, dict):
            continue
        if rec.get("tool") not in _ETYM_EVIDENCE_TOOLS:
            continue
        if rec.get("ok") is False:
            continue
        try:
            blob = json.dumps(rec.get("data"), ensure_ascii=False,
                              default=str)
        except Exception:
            blob = str(rec.get("data"))
        if blob and _ETYM_EVIDENCE_RE.search(blob):
            return True
    return False


def _sentence_around(answer: str, start: int, end: int) -> str:
    """The sentence (or markdown line) containing answer[start:end]."""
    s = answer.rfind("\n", 0, start)
    for brk in ".!?…":
        cut = answer.rfind(brk, 0, start)
        if cut > s:
            s = cut
    end_candidates = [answer.find(brk, end) for brk in ".!?…\n"]
    end_candidates = [e for e in end_candidates if e != -1]
    e = min(end_candidates) if end_candidates else len(answer)
    return answer[s + 1:e]


def audit_etymology_claims(answer: str,
                           tool_records: list[dict]) -> EtymologyClaimReport:
    """Deterministic: etymology claims without word_etymology backing.

    Binary evidence model per answer (как WP2b): если в записях есть
    word_etymology/find_words_by_etymology с непустым family_chain —
    этимологические клеймы считаются опёртыми и не трогаются. Иначе
    каждый клеймовый sentence (кроме честных absence-констатаций)
    попадает в report. Pure function, no LLM."""
    rep = EtymologyClaimReport(claims=[])
    if not answer or not answer.strip():
        return rep
    matches = list(_ETYM_CLAIM_RE.finditer(answer))
    if not matches:
        return rep
    if _records_carry_etymology_evidence(tool_records):
        return rep
    for m in matches[:5]:
        sentence = _sentence_around(answer, m.start(), m.end())
        if _ETYM_ABSENCE_RE.search(sentence):
            continue  # честная констатация отсутствия
        rep.claims.append(m.group(0).strip())
    return rep


def suppress_etymology_claims(answer: str,
                              report: EtymologyClaimReport) -> str:
    """Excise unsupported etymology sentences; append the honest
    canonical absence text (WP2b suppression mechanics)."""
    if not report.has_issues():
        return answer
    from scripts.v2.numeric_audit import _excise_sentence
    repaired = answer
    removed = 0
    for claim in report.claims:
        cut, ok = _excise_sentence(repaired, claim)
        if ok:
            repaired = cut
            removed += 1
    if not removed:
        return answer
    lines = [repaired.rstrip(), "", "---", "",
             "🔧 **Honesty guard** — из ответа удалено этимологическое "
             "утверждение без опоры в данных инструментов. Этимологии "
             "этого слова в данных корпуса нет."]
    return "\n".join(lines)


# Blockquote line = пример/цитата. Латинские токены ≥3 символов; цитата
# короче 4 токенов не проверяется (заголовки, русские пояснения,
# шаблонные подсказки выпадают сами).
_BLOCKQUOTE_LINE_RE = re.compile(r"^[ \t]*>\s?(.+)$", re.MULTILINE)
_QUOTE_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]{3,}")
_QUOTE_SUPPORT_THRESHOLD = 0.7


@dataclass
class ExampleQuoteReport:
    """Blockquote example lines with no support in tool data."""
    lines: list[str]

    def has_issues(self) -> bool:
        return bool(self.lines)


def _tool_data_blob(tool_records) -> str:
    parts: list[str] = []
    for rec in tool_records or []:
        if not isinstance(rec, dict):
            continue
        try:
            parts.append(json.dumps(rec.get("data"), ensure_ascii=False,
                                    default=str))
        except Exception:
            parts.append(str(rec.get("data")))
    return "\n".join(parts).lower()


def audit_example_quotes(answer: str,
                         tool_records: list[dict]) -> ExampleQuoteReport:
    """Deterministic: blockquote examples must be corpus-backed.

    Цитата поддержана, когда ≥70% её латинских токенов (≥3 символов)
    присутствуют в сериализованных data инструментов. Усечения «…» и
    атрибуция (— Author, *Title*) проходят: их токены тоже из data.
    enrich_word.example_sentence стёрт на враппере, поэтому сочинённый
    пример опоры не имеет. Pure function, no LLM."""
    rep = ExampleQuoteReport(lines=[])
    if not answer or not answer.strip():
        return rep
    matches = list(_BLOCKQUOTE_LINE_RE.finditer(answer))
    if not matches:
        return rep
    blob = _tool_data_blob(tool_records)
    for m in matches:
        tokens = [t.lower() for t in _QUOTE_TOKEN_RE.findall(m.group(1))]
        if len(tokens) < 4:
            continue
        present = sum(1 for t in tokens if t in blob)
        if present / len(tokens) < _QUOTE_SUPPORT_THRESHOLD:
            rep.lines.append(m.group(0))
    return rep


def suppress_unsupported_quotes(answer: str,
                                report: ExampleQuoteReport) -> str:
    """Remove fabricated blockquote example lines; append the honest
    disclosure (WP2b mechanics, построчное удаление)."""
    if not report.has_issues():
        return answer
    bad = set(report.lines)
    kept = [ln for ln in answer.splitlines() if ln not in bad]
    removed = len(answer.splitlines()) - len(kept)
    if not removed:
        return answer
    repaired = "\n".join(kept)
    lines = [repaired.rstrip(), "", "---", "",
             "🔧 **Honesty guard** — из ответа удалены примеры-цитаты, "
             "отсутствующие в корпусных данных инструментов. Примеры "
             "публикуются только из корпуса (hybrid_search / "
             "word_contexts)."]
    return "\n".join(lines)
