"""Sprint 16 Phase D — post-render numeric audit.

Programmatic check that catches the renderer hallucinating numbers
that don't appear in any tool result. Targets the specific failure
mode of v1.0e / Stan round 6: «у Doyle 47 книг» when tool data
showed 30, or «около 200 раз» when total_occurrences was 12.

This is NOT a general fact-checker — it just checks numbers. Other
kinds of fabrication (fake book titles, invented quotes) stay with
the critic LLM pass.

Pipeline placement: runs after `critic_mod.review()` and writes its
own footer block via `annotate_with_audit()`. Both critic and audit
footers can co-exist on the same answer (rare).

Tunables (env):
  WC_NUMERIC_AUDIT   — on (default) / off
  WC_AUDIT_MIN       — int >= 5 (default 5); ignore tiny numbers
  WC_AUDIT_TOL       — float (default 0.10); ±10% match tolerance
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger("wordcracker.v2.numeric_audit")

AUDIT_ENABLED = os.environ.get("WC_NUMERIC_AUDIT", "on").lower() in (
    "on", "1", "true")
AUDIT_MIN = int(os.environ.get("WC_AUDIT_MIN", "5"))
AUDIT_TOL = float(os.environ.get("WC_AUDIT_TOL", "0.10"))

# Skip intents whose answers don't need numeric verification.
_INTENT_SKIP = frozenset({
    "introduction", "clarify", "out_of_scope",
})

# Recognize numbers like 47, 1,234, 3.14, 55K, 12.5%.
_NUMBER_RE = re.compile(
    # No letter/digit/underscore/dot before — keeps us off PG1342 / v2.10.
    r"(?<![A-Za-zЀ-ӿ0-9_.])"
    # Number body: thousand-grouped (1,234 / 1 234 / nbsp) OR plain int/decimal.
    r"(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)"
    # Optional unit suffix.
    r"(?:\s*(%|тысяч\.?|тыс\.?|млн|[KkКк]))?"
    # Trailing lookahead — must be followed by non-letter so "200 книг"
    # does NOT bind the К as a 1000x suffix.
    r"(?=[\s.,;:!?)\]/\\-]|$)",
    re.UNICODE,
)

# Numbers in this range are likely birth/death years — handled by the
# `_bio_source: hardcoded` channel, not the audit.
_YEAR_LO, _YEAR_HI = 1500, 2100

# W-7 (2026-05-24) — bio-event context patterns. Used by `_is_year_like`
# to detect «год смерти / death year / умер / born» phrasings. Stan prod
# bug 2026-05-22: rendered «год смерти Marlowe 2008» when data carried
# only birth_year=1564. Previously the audit trusted any year-in-range,
# so the fabricated death year went unflagged. Now: if context is a bio
# event AND the specific year is not in data, the audit falls through
# to regular matching and the mismatch is flagged.
_BIO_DEATH_RE = re.compile(
    r"(?:год\s+смерт|death\s*year|умер|скончал|погиб|died|deceased|"
    r"умир|"
    r"годы\s+жизни|years?\s+lived|lifespan|"
    # year-range «(1564–2008)» / «1564—2008» / «1564-2008» implies a
    # birth–death pair; both members get bio-context treatment so the
    # death-side fabrication can be caught.
    r"\(?\s*\d{3,4}\s*[–—\-]\s*\d{3,4}\s*\)?)",
    re.IGNORECASE | re.UNICODE,
)
_BIO_BIRTH_RE = re.compile(
    r"(?:год\s+рожден|birth\s*year|родил|был\s+рожд[её]н|born|"
    r"рожд[её]н|"
    r"годы\s+жизни|years?\s+lived|lifespan|"
    r"\(?\s*\d{3,4}\s*[–—\-]\s*\d{3,4}\s*\)?)",
    re.IGNORECASE | re.UNICODE,
)


@dataclass
class NumericMismatch:
    """One number in the answer that isn't supported by tool data."""
    value: float
    formatted: str   # the literal text as it appears in the answer
    context: str     # surrounding ~40-char snippet for the footer
    nearest_in_data: float | None = None
    nearest_distance_pct: float | None = None


@dataclass
class AuditReport:
    """Result of audit_numbers()."""
    mismatches: list[NumericMismatch] = field(default_factory=list)
    skipped_reason: str | None = None
    numbers_checked: int = 0

    def has_issues(self) -> bool:
        return bool(self.mismatches)

    @classmethod
    def trust(cls, reason: str = "(audit disabled)") -> "AuditReport":
        return cls(skipped_reason=reason)


# ----- Number extraction -----

def _parse_number(raw: str, suffix: str | None) -> float | None:
    """Convert '1,234' / '3.14' / '55K' / '50%' to float. Returns None
    on shapes we shouldn't audit."""
    text = raw.strip()
    if "." in text:
        # Has a dot → it's a decimal. Strip thousand separators
        # (commas / whitespace incl. nbsp) but keep the dot.
        cleaned = re.sub(r"[,\s]", "", text)
        try:
            v = float(cleaned)
        except ValueError:
            return None
    else:
        # No dot → plain int or thousand-grouped (commas/spaces).
        cleaned = re.sub(r"[,\s]", "", text)
        if not cleaned.isdigit():
            return None
        v = float(cleaned)
    if suffix:
        s = suffix.strip().lower().rstrip(".")
        if s == "%":
            return v               # percent stays as written number
        if s in ("k", "к"):
            return v * 1_000
        if s in ("тыс", "тысяч"):
            return v * 1_000
        if s == "млн":
            return v * 1_000_000
    return v


def _extract_numbers(text: str) -> list[tuple[float, str, str]]:
    """Yield (value, formatted, context) for each number in `text`."""
    out: list[tuple[float, str, str]] = []
    for m in _NUMBER_RE.finditer(text):
        raw, suffix = m.group(1), m.group(2)
        val = _parse_number(raw, suffix)
        if val is None:
            continue
        lo = max(0, m.start() - 25)
        hi = min(len(text), m.end() + 25)
        ctx = text[lo:hi].replace("\n", " ")
        formatted = m.group(0).strip()
        out.append((val, formatted, ctx))
    return out


# Sprint 20+ B13 — markdown table detection. Numbers inside table cells
# are deterministically formatted from tool data (counts, percentages,
# row indices); auditing them produces a high false-positive rate when
# the LLM renders a derived column (e.g. share-of-total %) that the
# tool didn't surface verbatim. We strip the table region from audit
# input — narrative claims in prose are still checked.
_TABLE_LINE_RE = re.compile(
    r"^\s*\|.*\|\s*$",   # « | cell | cell | »
    re.MULTILINE,
)
# Markdown table separator: « | --- | --- | » — between header and rows.
_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?[-=]{2,}\s*[:|]?(?:\s*[-=]{2,}.*)?\s*\|?\s*$",
    re.MULTILINE,
)


def _strip_table_content(text: str) -> str:
    """Remove markdown table rows and separator lines from `text`.

    Returns the remaining prose / list / heading lines. Audit operates
    on the remainder. Detection is line-based: a line is treated as a
    table cell row when it has both a leading and trailing pipe with
    content between them.

    We don't try to detect HTML tables or grid tables — markdown pipe
    tables are what the renderer emits.
    """
    if "|" not in text:
        return text
    stripped = _TABLE_LINE_RE.sub("", text)
    stripped = _TABLE_SEP_RE.sub("", stripped)
    return stripped


# ----- Data-side number harvest -----

def _walk_numbers(obj: Any, out: set[float], *, depth: int = 0) -> None:
    """Recursively collect every numeric value reachable from `obj`.
    Caps depth so a pathological structure can't blow up the audit."""
    if depth > 6:
        return
    if isinstance(obj, bool):
        return  # exclude bools (which are ints in Python)
    if isinstance(obj, (int, float)):
        try:
            f = float(obj)
            if f != f or f == float("inf") or f == float("-inf"):
                return
            out.add(f)
        except Exception:
            pass
        return
    if isinstance(obj, str):
        # Numbers embedded in tool-data strings count too (e.g. "1881-1955"
        # in metadata). Use the same regex.
        for v, _, _ in _extract_numbers(obj):
            out.add(v)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_numbers(v, out, depth=depth + 1)
        return
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk_numbers(v, out, depth=depth + 1)
        return


def collect_data_numbers(tool_records: Iterable[dict]) -> set[float]:
    """Build the «trusted numbers» set from tool results.

    Also includes lengths of any lists found (so «top 10» can match the
    actual length of the returned list even if the answer phrases it as
    a count rather than echoing every number in the list).

    E17 (2026-05-22) — also walks `query` (the tool's request parameters
    like min_corpus_count=200) and `_view_filter_values` (numbers from
    view.empty_state.filters_applied / provenance.requested). Stan «Dorian
    Gray ADJ» bug: empty_state said «при min_corpus_count=200» and audit
    flagged 200 because the harvest only looked at `data` + `coverage`.
    The 200 IS in the user-visible answer, IS the actual filter value
    the wrapper used — but the previous harvest missed it.
    """
    out: set[float] = set()
    for rec in tool_records:
        if not isinstance(rec, dict):
            continue
        data = rec.get("data")
        _walk_numbers(data, out)
        # List lengths — capture for count-style claims
        _walk_list_lengths(data, out, depth=0)
        cov = rec.get("coverage")
        _walk_numbers(cov, out)
        # E17 — request parameters (filter values shown to user)
        q = rec.get("query")
        _walk_numbers(q, out)
        # E17 — view-side filter values harvested by rag_v2 helper
        view_aux = rec.get("_view_filter_values")
        _walk_numbers(view_aux, out)
    return out


def _walk_list_lengths(obj: Any, out: set[float], *, depth: int) -> None:
    if depth > 6 or obj is None:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_list_lengths(v, out, depth=depth + 1)
    elif isinstance(obj, (list, tuple)):
        out.add(float(len(obj)))
        for v in obj:
            _walk_list_lengths(v, out, depth=depth + 1)


# ----- Match check -----

def _is_matched(value: float, data_numbers: set[float],
                 tol: float = AUDIT_TOL) -> tuple[bool, float | None, float | None]:
    """Return (matched, nearest_value, nearest_distance_pct).

    Match is True iff some data number is within max(1, value*tol) of value."""
    if value in data_numbers:
        return True, value, 0.0
    if not data_numbers:
        return False, None, None
    # Find nearest data number
    nearest = min(data_numbers, key=lambda d: abs(d - value))
    delta = abs(nearest - value)
    if value == 0:
        return delta < 0.5, nearest, None
    margin = max(1.0, abs(value) * tol)
    matched = delta <= margin
    pct = (delta / abs(value)) * 100 if value else None
    return matched, nearest, pct


def _is_year_like(value: float, context: str,
                  data_numbers: set[float] | None = None) -> bool:
    """Return True iff `value` should be skipped by the audit as a
    «trusted year-like» (the `_bio_source: hardcoded` channel surfaces
    real bio years, so an unrestricted year-check would over-flag).

    W-7 (2026-05-24) — bio-year fabrication clause. When `data_numbers`
    is supplied AND the context references a bio event (death/birth)
    AND `value` is not in `data_numbers`, fall through to regular
    matching so the year gets flagged. Stan prod-bug 2026-05-22:
    «год смерти Marlowe 2008» — 2008 was in [1500, 2100], context said
    «год смерти», but the only year in data was birth_year=1564. The
    old trust-all-years logic let it through; this clause closes the
    gap.

    `data_numbers=None` keeps the old behaviour for callers that
    haven't been updated. Inside `audit_numbers` we always pass the
    harvested data_numbers set."""
    if not value.is_integer():
        return False
    iv = int(value)
    if not (_YEAR_LO <= iv <= _YEAR_HI):
        return False
    ctx_lc = context.lower()
    # Tokens that argue the number is NOT a year despite shape — counts
    # / occurrence claims happening to fall in 1500..2100.
    bad_words = ("книг", "слов", "раз", "упомин", "books", "words",
                 "occurrenc", "times")
    if any(b in ctx_lc for b in bad_words):
        return False
    # W-7 — bio-year fabrication path. Only kicks in when the caller
    # supplies data_numbers (so we can check if the specific year is
    # backed). Skip if the year IS in data (real bio year from
    # `_bio_source`). Skip if context isn't bio (general timestamp,
    # publication year reference — those still get trust).
    if data_numbers is not None and value not in data_numbers:
        is_bio = (_BIO_DEATH_RE.search(context) is not None or
                  _BIO_BIRTH_RE.search(context) is not None)
        if is_bio:
            return False  # let it through to _is_matched → flagged
    return True


# ----- Audit entry point -----

def audit_numbers(
    answer: str,
    tool_records: Iterable[dict],
    *,
    intent: str = "unknown",
    min_value: int = AUDIT_MIN,
    tol: float = AUDIT_TOL,
) -> AuditReport:
    """Programmatic check: every number in `answer` should also appear
    in (or close to) some number in tool_records.

    Returns AuditReport.trust() on disabled / skip / no-data paths.
    """
    if not AUDIT_ENABLED:
        return AuditReport.trust("(WC_NUMERIC_AUDIT=off)")
    if not answer or not answer.strip():
        return AuditReport.trust("(empty answer)")
    if intent in _INTENT_SKIP:
        return AuditReport.trust(f"(skip intent: {intent})")
    records = list(tool_records)
    if not records:
        return AuditReport.trust("(no tool records)")

    data_numbers = collect_data_numbers(records)

    # Sprint 20 — count-honesty pass. Affinity tools and learning_words
    # now surface `top_requested` + `top_returned` when filtering shrinks
    # the list. If the renderer writes the REQUESTED count near words
    # like «слов»/«списка»/«top»/«words», that's a known Stan-class
    # hallucination — explicitly flag it even if the requested number
    # happens to appear in tool data elsewhere (e.g. min_corpus_count=50).
    count_report = _audit_count_claims(answer, records)
    # Sprint 22+ Round 12 post-deploy: table-vs-zero contradiction.
    # Stan 2026-05-20 screenshot: 30-row table + footer «возвращено
    # 0 слов» slipped past every existing audit because 0 is below
    # AUDIT_MIN and the answer didn't trip the top_requested vs
    # top_returned delta path.
    table_zero_report = _audit_table_vs_zero_count(answer)
    if table_zero_report.mismatches:
        # Prepend so it shows first — it's the most user-visible kind
        # of contradiction.
        count_report.mismatches = (table_zero_report.mismatches
                                    + count_report.mismatches)

    # W-7 (2026-05-24) — removed the «no data → trust» early-return
    # bypass. Previously the audit silently skipped when collect_data_
    # numbers came back empty (e.g. tool returned only string fields,
    # or only `author_canonical: "..."`). That masked the Stan-class
    # fabrication «Автор родился в 1850, написал 47 произведений» on
    # records where data carried no numbers at all — both the year and
    # the count were free-form invention, yet audit returned trust().
    #
    # Now: still audit. Bio-year fabrications are caught via
    # `_is_year_like(..., data_numbers)` (returns False in bio context
    # when value not in data, including the data={} case). Non-year
    # numbers above min_value with empty data → flagged as fabrication
    # candidates. Non-bio years (publication, general timestamps) keep
    # trust via `_is_year_like`.

    # Sprint 20+ B13 — strip markdown table content before extracting
    # numbers. Table cells are formatted deterministically from tool
    # data; counts and derived percentages produced false positives
    # at high rate in Round 11. Prose / list / heading numbers still
    # get audited from `prose_only`.
    prose_only = _strip_table_content(answer)
    extracted = _extract_numbers(prose_only)
    report = AuditReport(numbers_checked=len(extracted),
                         mismatches=list(count_report.mismatches))
    seen_values: set[float] = {m.value for m in report.mismatches}
    for value, formatted, context in extracted:
        if value < min_value:
            continue
        if value in seen_values:
            continue
        seen_values.add(value)
        # W-7: pass data_numbers so bio-year fabrications fall through
        # to regular matching (year not in data + bio context → flagged).
        if _is_year_like(value, context, data_numbers):
            continue
        if not data_numbers:
            # W-7: empty data_numbers + reached this point means the
            # value isn't a trusted year (already filtered above). Flag
            # as fabrication candidate — no data to back any number.
            report.mismatches.append(NumericMismatch(
                value=value, formatted=formatted,
                context=context.strip(),
                nearest_in_data=None,
                nearest_distance_pct=None,
            ))
            if len(report.mismatches) >= 3:
                break
            continue
        matched, nearest, pct = _is_matched(value, data_numbers, tol=tol)
        if matched:
            continue
        report.mismatches.append(NumericMismatch(
            value=value, formatted=formatted,
            context=context.strip(),
            nearest_in_data=nearest,
            nearest_distance_pct=pct,
        ))
        # Cap report to top 3 — beyond that it's noise and the renderer
        # is doing something wholesale wrong, not a tactical hallucination.
        if len(report.mismatches) >= 3:
            break
    return report


# Patterns that suggest a count claim in the answer body. We pair them
# with `top_requested` / `top_returned` from tool data to spot misleads.
_COUNT_CLAIM_RE = re.compile(
    r"(?:список\s+из\s+|вот\s+|представлен[ао]?\s+(?:тебе\s+)?список\s+из\s+|"
    r"показываю\s+|показано\s+|"
    r"top[\s-]?|топ[\s-]?)"
    r"(\d{1,4})"
    r"\s*"
    r"(?:слов\w*|words?|элемент\w*|записе?й?|items?|"
    r"любим\w+\s+слов\w*|"
    r"фирменн\w+\s+слов\w*|"
    r"характерн\w+\s+слов\w*|"
    r"аффинн\w+\s+слов\w*)",
    re.IGNORECASE,
)


def _audit_count_claims(answer: str, records: Iterable[dict]) -> AuditReport:
    """Spot «список из 50 слов» when tool data says top_returned=19.

    Looks at each record for `top_requested` ≠ `top_returned`. If the
    answer mentions the requested number near a count word, flag it.

    Returns AuditReport (possibly empty). Caller merges into the
    main report.
    """
    rep = AuditReport()
    deltas: list[tuple[float, float]] = []  # (requested, returned)
    for r in records:
        data = (r.get("data") if isinstance(r, dict) else None) or {}
        if not isinstance(data, dict):
            continue
        req = data.get("top_requested")
        ret = data.get("top_returned")
        if isinstance(req, (int, float)) and isinstance(ret, (int, float)):
            if int(req) != int(ret):
                deltas.append((float(req), float(ret)))
    if not deltas:
        return rep

    # ANY count claim that doesn't match top_returned is suspect when
    # a delta is present. Two failure modes seen in prod:
    #   (a) claimed == top_requested but data filtered to top_returned
    #       — Stan 2026-05-19 first screenshot («50 слов» when data=19,
    #       request was 50).
    #   (b) claimed is neither top_requested nor top_returned — Stan's
    #       follow-up («50 слов» when request was 100 and data=19).
    #       Pure hallucination; renderer pulled a v1 default from
    #       somewhere or just made it up.
    # Both fail the same audit: claim must equal top_returned (truth)
    # to escape the flag. Surface both deltas in the report so the
    # user sees the full picture.
    returned_set = {int(ret) for _req, ret in deltas}
    for m in _COUNT_CLAIM_RE.finditer(answer):
        try:
            claimed = float(m.group(1))
        except ValueError:
            continue
        if int(claimed) in returned_set:
            continue  # truth — let it pass
        # Pick the closest (req, ret) pair for the report. Diagnostic
        # quality: nearest_in_data points at the actual returned count;
        # context shows the offending phrase.
        req, ret = min(deltas, key=lambda p: abs(p[1] - claimed))
        start = max(0, m.start() - 35)
        end = min(len(answer), m.end() + 25)
        ctx = answer[start:end]
        rep.mismatches.append(NumericMismatch(
            value=claimed,
            formatted=m.group(0),
            context=ctx.strip(),
            nearest_in_data=ret,
            nearest_distance_pct=abs(claimed - ret) / max(claimed, 1) * 100,
        ))
        if len(rep.mismatches) >= 3:
            break
    return rep


# Sprint 22+ Round 12 post-deploy: «таблица 30 строк ↔ возвращено 0
# слов» contradiction. Stan 2026-05-20 screenshot showed renderer
# hallucinating a footer «было возвращено 0 слов, хотя запрашивалось
# 30» over a perfectly populated 30-row table. _audit_count_claims
# couldn't catch it because the claim was «0» which is below AUDIT_MIN
# and there was no top_requested mismatch in data. New audit: count
# table rows, then scan for «возвращено N слов» phrasings where N
# wildly disagrees with the table.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?[-=]{2,}\s*[:|]?",  # | --- | -- |
)
_ZERO_COUNT_PHRASE_RE = re.compile(
    r"(?:было\s+возвращено|вернулось|returned)\s+"
    r"(\d{1,4})\s*"
    r"(?:слов|words|строк|rows|элемент\w*)",
    re.IGNORECASE,
)


def _count_table_rows(answer: str) -> int:
    """Count data rows in markdown pipe tables. Subtracts header (first
    `|...|` line) + separator (`|---|...`) per table. Multiple tables
    sum together — this is a coarse signal for «answer has tabular data
    of this size»."""
    lines = _TABLE_ROW_RE.findall(answer)
    if not lines:
        return 0
    # Drop separator-like lines from count
    data_rows = [l for l in lines if not _TABLE_SEPARATOR_RE.match(l)]
    # Heuristic: first row of each contiguous block is a header. Hard
    # to detect block boundaries without state — approximate with -1
    # per detected table separator (each separator implies one header
    # before it).
    n_separators = sum(1 for l in lines if _TABLE_SEPARATOR_RE.match(l))
    n_data = max(0, len(data_rows) - n_separators)
    return n_data


def _audit_table_vs_zero_count(answer: str) -> AuditReport:
    """Flag «возвращено N слов» footers that contradict the table."""
    rep = AuditReport()
    table_rows = _count_table_rows(answer)
    if table_rows == 0:
        return rep
    for m in _ZERO_COUNT_PHRASE_RE.finditer(answer):
        try:
            claimed = int(m.group(1))
        except ValueError:
            continue
        # Allow exact match (renderer correctly says «вернулось 30»
        # alongside 30-row table). Flag when claim wildly diverges.
        if claimed == table_rows:
            continue
        # Threshold: only flag clear contradictions where the gap is
        # >50% of the table size. Avoids picking up legitimate «top_
        # requested vs returned» disclosures.
        if abs(claimed - table_rows) <= max(2, table_rows // 5):
            continue
        start = max(0, m.start() - 40)
        end = min(len(answer), m.end() + 20)
        ctx = answer[start:end].replace("\n", " ").strip()
        rep.mismatches.append(NumericMismatch(
            value=float(claimed),
            formatted=m.group(0),
            context=f"[table has {table_rows} rows] {ctx}",
            nearest_in_data=float(table_rows),
            nearest_distance_pct=(abs(claimed - table_rows)
                                    / max(table_rows, 1) * 100),
        ))
        if len(rep.mismatches) >= 2:
            break
    return rep


def annotate_with_audit(answer: str, report: AuditReport) -> str:
    """Append a 📊 footer when mismatches were found. Silent otherwise."""
    if not report.has_issues():
        return answer
    lines = [answer.rstrip(), "", "---", "",
             "📊 **Numeric audit** — числа в ответе, которых нет в tool data:"]
    for m in report.mismatches:
        ctx = m.context if len(m.context) <= 80 else m.context[:77] + "..."
        nearest_str = ""
        if m.nearest_in_data is not None:
            # Format nearest cleanly: integers without trailing .0
            n = m.nearest_in_data
            n_str = str(int(n)) if n.is_integer() else f"{n:.2f}"
            nearest_str = f" (ближайшее в data: {n_str})"
        lines.append(f"- `{m.formatted}` в «…{ctx}…»{nearest_str}")
    lines.append("")
    lines.append("_Проверь tool trace выше; возможно renderer додумал._")
    return "\n".join(lines)
