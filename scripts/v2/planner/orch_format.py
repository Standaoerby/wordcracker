"""WC_ORCHESTRATOR_FORMAT — Ollama structured-output selector (WP-1 PR-1).

RAG_TASK_WP1 / D-R30-8·9·10. The live v2 orchestrator issues single-shot
`format=json` Ollama calls for the two classification steps that decide
routing — intent (`llm_intent`) and the v4 planner (`llm_planner`). This
module lets us flip those two calls to `format=<JSON-Schema>` ("structured
outputs") behind ONE env switch, so we can MEASURE whether schema-constrained
decoding lifts routing accuracy on the fast incumbent `wordcracker:v2`
(78.8 tok/s) *before* paying ~4× latency for a bigger planner.

Contract — NO-OP BY DEFAULT (keeps prod byte-for-byte):

    WC_ORCHESTRATOR_FORMAT unset / "json"  → orch_format(schema) == "json"
    WC_ORCHESTRATOR_FORMAT == "schema"     → orch_format(schema) == schema

Because the default is the incumbent literal "json", the on-disk 8890 runner
behaviour is unchanged when the env is unset — no re-record, auto-mergeable on
the live contour (R1: this is a measurement switch with a written DECISION GATE
in RAG_TASK_WP1, not dark code behind a permanently-off flag).

The two schemas describe exactly the shapes the existing prompts already ask
the model to emit, so schema-mode only CONSTRAINS the same contract — it never
asks for a different shape:

    PLANNER_SCHEMA ↔ scripts.v2.planner.plan_spec.from_json
                     (steps[].{id,tool,args,needs,…} + optional clarify)
    INTENT_SCHEMA  ↔ scripts.v2.planner.llm_intent.classify_and_extract
                     (intent-label enum + author/book_title/word/year_*/country)

The intent enum is derived from the live INTENTS taxonomy (never hand-copied),
so it can't drift out of sync with the classifier (guarded by a test).
"""
from __future__ import annotations

import os
from typing import Union

from scripts.v2.planner.intent import INTENTS

#: Env var the operator flips on SOW to run the schema arm of the eval.
ENV_VAR = "WC_ORCHESTRATOR_FORMAT"
_SCHEMA_MODE = "schema"


def is_schema_mode() -> bool:
    """True iff the operator opted into structured outputs for this process.

    Anything other than a case-insensitive "schema" (including unset, the
    empty string, or the literal "json") is the incumbent JSON-mode no-op."""
    return os.environ.get(ENV_VAR, "json").strip().lower() == _SCHEMA_MODE


def orch_format(schema: dict) -> Union[str, dict]:
    """Pick the Ollama ``format`` value for one orchestrator call.

    Default (env unset / "json") returns the literal ``"json"`` — identical to
    the incumbent behaviour (no-op). Only ``WC_ORCHESTRATOR_FORMAT=schema``
    swaps in the JSON Schema so Ollama constrains decoding to it.

    Read per-call (not cached) so the toggle is honoured the moment the env is
    set, and so a test can flip it with ``mock.patch.dict(os.environ, …)``."""
    return schema if is_schema_mode() else "json"


# --- Planner output schema — mirrors plan_spec.from_json --------------------
# Canonical keys only (id/tool/args/needs/optional/rationale + clarify, the
# `to_json` shape the few-shots emit); `from_json` ALSO tolerates aliases
# (tool_name/step_id/depends_on/…), but in schema-mode we constrain generation
# to the one canonical shape rather than inviting the alias quirks.
#
# No top-level `required`: a clarify-only plan ({"clarify": "…"}) is a valid
# terminal output (plan_spec.from_json / validate treat it as such), so the
# schema must accept a steps-less object too.
#
# `args` is an explicitly free-form object: each tool has its own arg shape and
# the routing signal we measure lives in `steps[].tool`, not in the arg values.
# Tool-name enum-constraining is deliberately NOT done here — it would force
# importing the heavy tool registry on every planner call and break this
# module's hermetic import; hallucinated tools already bounce at plan_spec.
# validate (→ retry). A tool enum is a clean follow-up if schema-mode wins.
PLANNER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intent_hint": {"type": "string"},
        "rationale": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string"},
                    "args": {"type": "object", "additionalProperties": True},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "optional": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "required": ["id", "tool"],
            },
        },
        "render_hint": {"type": "string"},
        "expected_cost": {"type": "string",
                          "enum": ["cheap", "medium", "heavy"]},
        "clarify": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}


# --- Intent + entity schema — mirrors llm_intent.classify_and_extract -------
# `intent` is enum-constrained to the LIVE taxonomy (imported, not copied) so a
# schema-constrained label is always a real one the router can dispatch — this
# is the high-value routing constraint. The entity slots are nullable; the
# parser's _clean_str/_clean_int/_clean_country coerce/validate the values, so
# the schema only needs to admit "string-or-null" / "integer-or-null".
INTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": sorted(INTENTS)},
        "author": {"type": ["string", "null"]},
        "book_title": {"type": ["string", "null"]},
        "word": {"type": ["string", "null"]},
        "year_from": {"type": ["integer", "null"]},
        "year_to": {"type": ["integer", "null"]},
        "country": {"type": ["string", "null"]},
    },
    "required": ["intent"],
}


__all__ = [
    "ENV_VAR",
    "is_schema_mode",
    "orch_format",
    "PLANNER_SCHEMA",
    "INTENT_SCHEMA",
]
