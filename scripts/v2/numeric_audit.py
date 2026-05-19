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
    a count rather than echoing every number in the list)."""
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


def _is_year_like(value: float, context: str) -> bool:
    """Skip plausible years that are likely from `_bio_source` channel
    or general timestamps. We trust the `_bio_source` mechanism for bio
    years."""
    if not value.is_integer():
        return False
    iv = int(value)
    if _YEAR_LO <= iv <= _YEAR_HI:
        # Year-like in answer; trust unless surrounded by clearly non-year
        # tokens like «книг», «слов», «раз», «упоминан».
        bad_words = ("книг", "слов", "раз", "упомин", "books", "words",
                     "occurrenc", "times")
        ctx_lc = context.lower()
        if any(b in ctx_lc for b in bad_words):
            return False
        return True
    return False


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

    if not data_numbers and not count_report.mismatches:
        return AuditReport.trust("(no numbers in tool data)")

    extracted = _extract_numbers(answer)
    report = AuditReport(numbers_checked=len(extracted),
                         mismatches=list(count_report.mismatches))
    seen_values: set[float] = {m.value for m in report.mismatches}
    for value, formatted, context in extracted:
        if value < min_value:
            continue
        if value in seen_values:
            continue
        seen_values.add(value)
        if _is_year_like(value, context):
            continue
        if not data_numbers:
            continue  # nothing to compare against (count-honesty already ran)
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

    for m in _COUNT_CLAIM_RE.finditer(answer):
        try:
            claimed = float(m.group(1))
        except ValueError:
            continue
        for req, ret in deltas:
            if claimed == req and claimed != ret:
                # Build context window around the claim
                start = max(0, m.start() - 35)
                end = min(len(answer), m.end() + 25)
                ctx = answer[start:end]
                rep.mismatches.append(NumericMismatch(
                    value=claimed,
                    formatted=m.group(0),
                    context=ctx.strip(),
                    nearest_in_data=ret,
                    nearest_distance_pct=abs(claimed - ret) / claimed * 100,
                ))
                break
        if len(rep.mismatches) >= 3:
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
