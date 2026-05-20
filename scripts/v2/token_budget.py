"""Token budget layer — unified protection against num_ctx overflow.

Sprint 22+ Round 12 post-deploy (Stan 2026-05-20): «200 фирменных слов
Кристи» rendered as art-gallery confabulation because tool result blew
qwen3:14b's 8k context. Previous defense was magic-number caps
(RENDER_LIST_CAP=50, RENDER_STR_CAP=2000) wired only into the renderer.

This module provides a single contract:
  1. Estimate token count of payload (cheap, conservative)
  2. Iteratively shrink payload until it fits budget
  3. Report what was cut so callers can disclose

The estimator uses chars/token=3 as a conservative heuristic:
  - English text: ~4 chars/token (we over-estimate, that's fine)
  - Russian text: ~2 chars/token (we under-estimate slightly — Cyrillic
    UTF-8 bytes are denser than English)
  - JSON structure: ~5 chars/token (predictable BPE for keys)
  Net: ~3 is conservative for mixed payloads, biases toward shrinking
  earlier than strictly necessary. False positives (shrink when fits)
  beat false negatives (overflow undetected).

Ladder strategy (applied in order, stop when fits):
  1. cap_lists_50      — any list > 50 items → first 50 + marker
  2. cap_strings_2000  — any string > 2000 chars
  3. cap_lists_20      — any list > 20 items
  4. cap_strings_1000  — any string > 1000 chars
  5. drop_render_notes — remove _render_note + _truncated_to markers
  6. cap_lists_10      — any list > 10 items
  7. cap_strings_500   — any string > 500 chars

If still doesn't fit after all rungs: report.fits=False, caller decides
whether to fail honestly or attempt anyway.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("wordcracker.v2.token_budget")


# ---------- per-model context windows ----------
#
# qwen3:14b stock default is 8192. We override to 16384 via Ollama
# `options.num_ctx` at request time (env: WC_OLLAMA_NUM_CTX). With
# bumped ctx the budget allows larger payloads but the contract is
# still enforced — adaptive shrink uses whatever's available.

MODEL_CTX_DEFAULTS = {
    "qwen3:14b":   16384,    # default in Ollama, with our override
    "qwen3:14b-32k": 32768,
    "qwen3:32b":   32768,
    "qwen2.5:14b": 8192,
    "qwen2.5:32b": 32768,
}
DEFAULT_CTX = 8192
DEFAULT_HEADROOM = 1500       # tokens reserved for response
CHARS_PER_TOKEN = 3.0          # conservative heuristic


def get_model_ctx(model: str) -> int:
    """Return ctx limit for `model`. Honors WC_OLLAMA_NUM_CTX override
    when present (applies to all models — single global ctx bump)."""
    override = os.environ.get("WC_OLLAMA_NUM_CTX")
    if override:
        try:
            return int(override)
        except ValueError:
            log.warning("WC_OLLAMA_NUM_CTX=%r is not an integer", override)
    return MODEL_CTX_DEFAULTS.get(model, DEFAULT_CTX)


@dataclass
class ShrinkReport:
    """What happened during shrink_to_fit. Logged to obs + can be
    surfaced to user as a disclosure note."""
    initial_tokens: int
    final_tokens: int
    budget: int
    fits: bool
    actions: list[str] = field(default_factory=list)
    lists_capped: int = 0
    strings_capped: int = 0
    fields_dropped: int = 0

    def utilization_pct(self) -> int:
        if self.budget <= 0:
            return 0
        return int(self.final_tokens / self.budget * 100)

    def confabulation_risk(self) -> str:
        """High/medium/low risk that the LLM produces hallucinated text
        due to ctx pressure. Based on utilization after shrink."""
        u = self.utilization_pct()
        if u >= 95:
            return "high"
        if u >= 80:
            return "medium"
        return "low"


# ---------- ladder operations ----------


def _cap_lists(obj: Any, *, cap: int, _depth: int = 0) -> tuple[Any, int]:
    """Recurse and cap any list with > `cap` items at `cap` + marker.
    Returns (modified, n_lists_capped_at_this_depth_and_below)."""
    if _depth > 8:
        return obj, 0
    if isinstance(obj, list):
        if len(obj) > cap:
            head: list[Any] = []
            sub_n = 0
            for x in obj[:cap]:
                w, n = _cap_lists(x, cap=cap, _depth=_depth + 1)
                head.append(w)
                sub_n += n
            head.append({
                "_truncated_to": cap,
                "_original_length": len(obj),
                "_note": f"list truncated to {cap} of {len(obj)} for token budget",
            })
            return head, 1 + sub_n
        out_list = []
        sub_n = 0
        for x in obj:
            w, n = _cap_lists(x, cap=cap, _depth=_depth + 1)
            out_list.append(w)
            sub_n += n
        return out_list, sub_n
    if isinstance(obj, dict):
        out_dict = {}
        sub_n = 0
        for k, v in obj.items():
            w, n = _cap_lists(v, cap=cap, _depth=_depth + 1)
            out_dict[k] = w
            sub_n += n
        return out_dict, sub_n
    return obj, 0


def _cap_strings(obj: Any, *, cap: int, _depth: int = 0) -> tuple[Any, int]:
    """Recurse and cap any string > `cap` chars. Returns (modified,
    n_strings_capped)."""
    if _depth > 8:
        return obj, 0
    if isinstance(obj, str):
        if len(obj) > cap:
            return obj[:cap] + f"…[truncated, {len(obj)} chars total]", 1
        return obj, 0
    if isinstance(obj, list):
        out_list = []
        sub_n = 0
        for x in obj:
            w, n = _cap_strings(x, cap=cap, _depth=_depth + 1)
            out_list.append(w)
            sub_n += n
        return out_list, sub_n
    if isinstance(obj, dict):
        out_dict = {}
        sub_n = 0
        for k, v in obj.items():
            w, n = _cap_strings(v, cap=cap, _depth=_depth + 1)
            out_dict[k] = w
            sub_n += n
        return out_dict, sub_n
    return obj, 0


_OPTIONAL_KEYS = frozenset({
    "_render_note", "_truncated_to", "_original_length", "_note",
    "metric_explanations", "_filter_drops", "_threshold_auto_lowered",
    "_corpus_version",
})


def _drop_optional_fields(obj: Any, _depth: int = 0) -> tuple[Any, int]:
    """Drop optional/cosmetic fields that the renderer doesn't strictly
    need. Last-resort shrink before failing."""
    if _depth > 8:
        return obj, 0
    if isinstance(obj, dict):
        out_dict = {}
        dropped = 0
        for k, v in obj.items():
            if k in _OPTIONAL_KEYS:
                dropped += 1
                continue
            w, n = _drop_optional_fields(v, _depth=_depth + 1)
            out_dict[k] = w
            dropped += n
        return out_dict, dropped
    if isinstance(obj, list):
        out_list = []
        sub_n = 0
        for x in obj:
            w, n = _drop_optional_fields(x, _depth=_depth + 1)
            out_list.append(w)
            sub_n += n
        return out_list, sub_n
    return obj, 0


# ---------- main class ----------


@dataclass
class TokenBudget:
    """Per-LLM-call token budget. Use this for ANY Ollama call where
    payload size is variable.

    Example:
        budget = TokenBudget(model="qwen3:14b")
        payload, report = budget.shrink_to_fit(summary_payload)
        if not report.fits:
            log.warning("hard-fail: %s", report)
            # decide: emit error event OR send anyway with risk
        # ...send to Ollama...
    """
    model: str
    headroom: int = DEFAULT_HEADROOM

    @property
    def ctx(self) -> int:
        return get_model_ctx(self.model)

    @property
    def input_budget(self) -> int:
        return max(self.ctx - self.headroom, 1024)

    def estimate(self, obj: Any) -> int:
        """Cheap token estimate. Serializes to JSON for non-string."""
        if isinstance(obj, str):
            return int(len(obj) / CHARS_PER_TOKEN)
        try:
            s = json.dumps(obj, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return self.input_budget  # treat as max — caller must shrink
        return int(len(s) / CHARS_PER_TOKEN)

    def fits(self, obj: Any) -> bool:
        return self.estimate(obj) <= self.input_budget

    def shrink_to_fit(self, payload: Any) -> tuple[Any, ShrinkReport]:
        """Run the shrink ladder until payload fits or ladder exhausted.

        Returns (possibly-modified payload, ShrinkReport).
        Idempotent: if already fits, returns payload unchanged with
        report.fits=True and zero actions.
        """
        initial = self.estimate(payload)
        report = ShrinkReport(
            initial_tokens=initial,
            final_tokens=initial,
            budget=self.input_budget,
            fits=(initial <= self.input_budget),
        )
        if report.fits:
            return payload, report

        # Ladder — ordered by cost (cheapest cuts first, most aggressive last)
        ladder = [
            ("cap_lists_50",     lambda p: _cap_lists(p, cap=50)),
            ("cap_strings_2000", lambda p: _cap_strings(p, cap=2000)),
            ("cap_lists_20",     lambda p: _cap_lists(p, cap=20)),
            ("cap_strings_1000", lambda p: _cap_strings(p, cap=1000)),
            ("drop_optional",    _drop_optional_fields),
            ("cap_lists_10",     lambda p: _cap_lists(p, cap=10)),
            ("cap_strings_500",  lambda p: _cap_strings(p, cap=500)),
        ]
        for action_name, applier in ladder:
            payload, n_changed = applier(payload)
            if n_changed > 0:
                report.actions.append(action_name)
                if "lists" in action_name:
                    report.lists_capped += n_changed
                elif "strings" in action_name:
                    report.strings_capped += n_changed
                elif "drop" in action_name:
                    report.fields_dropped += n_changed
            current = self.estimate(payload)
            report.final_tokens = current
            if current <= self.input_budget:
                report.fits = True
                return payload, report

        # Exhausted ladder — return as-is with fits=False
        report.final_tokens = self.estimate(payload)
        return payload, report

    def to_log_dict(self, report: ShrinkReport) -> dict:
        """Per-call log structure for obs_mod.log_request consumption."""
        return {
            "budget_input": self.input_budget,
            "budget_ctx": self.ctx,
            "estimate_initial_tokens": report.initial_tokens,
            "estimate_final_tokens": report.final_tokens,
            "budget_utilization_pct": report.utilization_pct(),
            "shrink_applied": bool(report.actions),
            "shrink_actions": report.actions,
            "shrink_lists_capped": report.lists_capped,
            "shrink_strings_capped": report.strings_capped,
            "shrink_fields_dropped": report.fields_dropped,
            "confabulation_risk": report.confabulation_risk(),
            "budget_fits": report.fits,
        }


__all__ = [
    "TokenBudget",
    "ShrinkReport",
    "MODEL_CTX_DEFAULTS",
    "DEFAULT_CTX",
    "DEFAULT_HEADROOM",
    "CHARS_PER_TOKEN",
    "get_model_ctx",
]
