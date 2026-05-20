"""Tool catalog serializer for the v4 LLM planner.

Goal: hand the LLM a *compressed* description of every available tool
so it can pick the right ones for a query, without blowing the context
window. Single source of truth is `tool_registry.REGISTRY` — adding a
new `@tool` automatically makes it available to the planner with zero
prompt updates.

This deliberately differs from `build_tools_spec()` (the OpenAI/Ollama
function-calling format) because the v4 planner emits a *plan*, not
individual tool calls. The catalog format optimizes for human-readable
compactness in the prompt:

    [authors] affinity_by_author(author_regex, top=50, ...) cost=medium
      → фирменные слова автора (affinity vs корпус). POS-фильтр через
        pos_filter=['ADJ'/'NOUN'/'VERB'].

Tool authors can add a `planner_hint` field to their @tool decorator
(future), but for now we synthesize from description + input_schema.

The catalog also bundles few-shot example plans, organized by query
pattern type (lookup / compound / triangulation / etymology-ratio / ...)
so the LLM has concrete templates to imitate.
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class CatalogEntry:
    name: str
    category: str
    description: str
    cost: str
    required: list[str]
    optional: list[str]


def build_catalog(*, registry: Optional[dict] = None,
                  category_filter: Optional[Iterable[str]] = None,
                  exclude: Optional[Iterable[str]] = None,
                  ) -> list[CatalogEntry]:
    """Build a list of CatalogEntry from the tool registry."""
    if registry is None:
        from scripts.v2.tool_registry import REGISTRY as _R
        registry = _R

    cats = set(category_filter) if category_filter else None
    skip = set(exclude or [])
    out: list[CatalogEntry] = []
    for name, spec in sorted(registry.items()):
        if name in skip:
            continue
        if cats and spec.category not in cats:
            continue
        schema = getattr(spec, "input_schema", None) or {}
        props = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        optional = [k for k in props if k not in required]
        out.append(CatalogEntry(
            name=name,
            category=spec.category,
            description=(spec.description or "").strip(),
            cost=getattr(spec, "cost", "medium"),
            required=required,
            optional=optional,
        ))
    return out


def render_catalog(entries: list[CatalogEntry], *,
                   max_desc_chars: int = 200) -> str:
    """Format the catalog as a compact text block for the LLM prompt.

    Grouped by category, each entry on 2 lines. We strip extreme prompt
    noise (multi-paragraph examples in tool docstrings) by capping the
    description length.
    """
    by_cat: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)
    lines: list[str] = []
    for cat in sorted(by_cat):
        lines.append(f"\n### {cat}\n")
        for e in by_cat[cat]:
            sig_args = list(e.required)
            for o in e.optional[:4]:  # cap optional shown
                sig_args.append(f"{o}?")
            sig = ", ".join(sig_args)
            desc = (e.description or "").replace("\n", " ")
            desc = " ".join(desc.split())  # collapse runs of ws
            if len(desc) > max_desc_chars:
                desc = desc[:max_desc_chars - 1].rstrip() + "…"
            lines.append(f"- `{e.name}({sig})` cost={e.cost}")
            if desc:
                lines.append(f"  {desc}")
    return "\n".join(lines).strip()


# ---------- few-shot examples ----------


def few_shot_examples() -> list[dict]:
    """Return canonical (query, plan_json) pairs.

    These are the LLM's main signal for the JSON output shape. Picked to
    cover the failure modes captured in test_external_claude rounds:
        - simple lookup
        - multi-book etymology ratio
        - triangulation (3 authors, "ближе к")
        - corpus-wide aggregation → clarify with recipe
        - genuine ambiguous → clarify
        - living-language phrasing → still works
    """
    return [
        # --- Simple book lookup ---
        {
            "query": "уровень сложности Pride and Prejudice",
            "plan": {
                "intent_hint": "book_readability",
                "rationale": "single-book readability lookup",
                "steps": [
                    {"id": "s1", "tool": "resolve_book_title",
                     "args": {"query": "Pride and Prejudice"}},
                    {"id": "s2", "tool": "book_readability",
                     "args": {"pg_id": "$s1.pg_id"},
                     "needs": ["s1"]},
                ],
                "render_hint": "readability_summary",
                "expected_cost": "medium",
            },
        },
        # --- Author signature words ---
        {
            "query": "фирменные слова Конан Дойля",
            "plan": {
                "intent_hint": "author_vocab",
                "rationale": "author affinity lookup",
                "steps": [
                    {"id": "s1", "tool": "resolve_author_name",
                     "args": {"query": "Конан Дойль"}},
                    {"id": "s2", "tool": "affinity_by_author",
                     "args": {"author_regex": "$s1.author_regex",
                              "top": 30, "min_corpus_count": 500},
                     "needs": ["s1"]},
                ],
                "render_hint": "signature_words_table",
                "expected_cost": "medium",
            },
        },
        # --- Multi-book etymology ratio (Stan 2026-05-19) ---
        {
            "query": "germanic vs latinate ratio в Beowulf и Paradise Lost",
            "plan": {
                "intent_hint": "etymology_ratio_compare",
                "rationale": ("compare germanic vs latinate signature word "
                              "counts per book"),
                "steps": [
                    {"id": "s1", "tool": "resolve_book_title",
                     "args": {"query": "Beowulf"}},
                    {"id": "s2", "tool": "resolve_book_title",
                     "args": {"query": "Paradise Lost"}},
                    {"id": "s3", "tool": "find_words_by_etymology",
                     "args": {"scope": {"book": "$s1.pg_id"},
                              "family": "germanic", "top": 20},
                     "needs": ["s1"]},
                    {"id": "s4", "tool": "find_words_by_etymology",
                     "args": {"scope": {"book": "$s1.pg_id"},
                              "family": "latin", "top": 20},
                     "needs": ["s1"]},
                    {"id": "s5", "tool": "find_words_by_etymology",
                     "args": {"scope": {"book": "$s2.pg_id"},
                              "family": "germanic", "top": 20},
                     "needs": ["s2"]},
                    {"id": "s6", "tool": "find_words_by_etymology",
                     "args": {"scope": {"book": "$s2.pg_id"},
                              "family": "latin", "top": 20},
                     "needs": ["s2"]},
                ],
                "render_hint": "etymology_ratio_table",
                "expected_cost": "heavy",
            },
        },
        # --- Triangulation (3 authors, "ближе к") ---
        {
            "query": "Burrows Delta между Dickens и Trollope: кто ближе к Eliot",
            "plan": {
                "intent_hint": "triangulation",
                "rationale": ("two pairwise compare_authors against the "
                              "third author; smaller Burrows = closer"),
                "steps": [
                    {"id": "s1", "tool": "resolve_author_name",
                     "args": {"query": "Dickens"}},
                    {"id": "s2", "tool": "resolve_author_name",
                     "args": {"query": "Trollope"}},
                    {"id": "s3", "tool": "resolve_author_name",
                     "args": {"query": "George Eliot"}},
                    {"id": "s4", "tool": "compare_authors",
                     "args": {"author1_regex": "$s1.author_regex",
                              "author2_regex": "$s3.author_regex",
                              "min_corpus_count": 500},
                     "needs": ["s1", "s3"]},
                    {"id": "s5", "tool": "compare_authors",
                     "args": {"author1_regex": "$s2.author_regex",
                              "author2_regex": "$s3.author_regex",
                              "min_corpus_count": 500},
                     "needs": ["s2", "s3"]},
                ],
                "render_hint": "triangulation_compare",
                "expected_cost": "heavy",
            },
        },
        # --- Genuinely ambiguous → clarify ---
        {
            "query": "расскажи про вампиров",
            "plan": {
                "clarify": ("Уточни — про что именно? Я умею: (а) найти "
                            "книги про вампиров по теме («рассказы о "
                            "вампирах в горгот-литературе»), (б) "
                            "сравнить vampires-словарь Stoker vs "
                            "Le Fanu, (в) показать упоминания vampire в "
                            "конкретной книге. Что из этого?")
            },
        },
        # --- Word contexts scoped by author ---
        {
            "query": "контексты использования слова duty у Austen",
            "plan": {
                "intent_hint": "word_contexts",
                "rationale": "fetch ±N-token contexts of `duty` across Austen",
                "steps": [
                    {"id": "s1", "tool": "resolve_author_name",
                     "args": {"query": "Austen"}},
                    {"id": "s2", "tool": "word_contexts",
                     "args": {"author_regex": "$s1.author_regex",
                              "word": "duty", "max_samples": 5},
                     "needs": ["s1"]},
                ],
                "render_hint": "word_contexts_panel",
                "expected_cost": "medium",
            },
        },
        # --- Followup: translate prior word list ---
        # Sprint 20+ — Stan chose v4 routing for all followups.
        # The «Previous assistant response» block shows the table; LLM
        # extracts column 1 and emits enrich_word steps per word.
        {
            "query": ("Previous user message: топ 100 любимых слов "
                       "Конан Дойля\n\n"
                       "Previous assistant response (truncated):\n"
                       "Вот топ 100 слов Конан Дойля:\n"
                       "| Word | Affinity |\n"
                       "| blighter | 850 |\n"
                       "| dashed | 720 |\n"
                       "| ripping | 540 |\n"
                       "| hullo | 410 |\n\n"
                       "Current query: возьми эти слова и переведи на русский"),
            "plan": {
                "intent_hint": "translate_prior_words",
                "rationale": ("user wants RU translations of the 4 words "
                              "in the prior table; enrich_word per word"),
                "steps": [
                    {"id": "s1", "tool": "enrich_word",
                     "args": {"word": "blighter", "target_lang": "ru"}},
                    {"id": "s2", "tool": "enrich_word",
                     "args": {"word": "dashed", "target_lang": "ru"}},
                    {"id": "s3", "tool": "enrich_word",
                     "args": {"word": "ripping", "target_lang": "ru"}},
                    {"id": "s4", "tool": "enrich_word",
                     "args": {"word": "hullo", "target_lang": "ru"}},
                ],
                "render_hint": "translation_table",
                "expected_cost": "heavy",
            },
        },
        # --- Followup: re-run with stricter proper-noun filter ---
        {
            "query": ("Previous user message: фирменные слова Дойла\n\n"
                       "Previous assistant response (truncated):\n"
                       "| Word | Affinity |\n"
                       "| holmes | 320 |\n"
                       "| watson | 280 |\n"
                       "| blighter | 65 |\n\n"
                       "Current query: убери из них имена собственные"),
            "plan": {
                "intent_hint": "author_vocab_strict_propn",
                "rationale": ("re-run prior affinity with stricter min_corpus_count "
                              "to drop character names that slipped through"),
                "steps": [
                    {"id": "s1", "tool": "resolve_author_name",
                     "args": {"query": "Дойл"}},
                    {"id": "s2", "tool": "affinity_by_author",
                     "args": {"author_regex": "$s1.author_regex",
                              "top": 30, "min_corpus_count": 5000},
                     "needs": ["s1"]},
                ],
                "render_hint": "signature_words_table",
                "expected_cost": "medium",
            },
        },
    ]


def render_examples(examples: Optional[list[dict]] = None) -> str:
    """Format the few-shot examples as prompt text."""
    examples = examples or few_shot_examples()
    blocks: list[str] = []
    for ex in examples:
        q = ex["query"]
        plan = json.dumps(ex["plan"], ensure_ascii=False, indent=2)
        blocks.append(f"### Example\nQuery: {q}\nPlan:\n```json\n{plan}\n```")
    return "\n\n".join(blocks)


# ---------- full prompt assembly ----------


SYSTEM_PROMPT_TEMPLATE = """\
You are the wordcracker query planner. Your job is to emit a JSON plan \
that the deterministic router will execute against the Project Gutenberg \
corpus.

CRITICAL RULES
1. Output **valid JSON only**, matching the PlanSpec schema. No prose, no \
markdown fences in the actual response.
2. You do NOT execute tools. Only emit JSON.
3. You may only use tools from the catalog below. Inventing tool names \
fails validation and the plan will be rejected.
4. Args MUST match each tool's input_schema (required fields).
5. Use `"$sN.field"` for step dependencies. Example: \
`"pg_id": "$s1.first_id"` injects the first_id field from step s1's result \
into step s2.
6. Compound queries → multiple steps. Triangulation → pairwise compares. \
Multi-book → fan-out via independent steps with the same template.
7. If you cannot construct a valid plan (genuinely ambiguous, missing \
information, or tools insufficient), return `{{"clarify": "<specific question \
in user's language>"}}` instead.
8. Set `render_hint` so the renderer knows how to format the result.
9. **ASCII-ONLY for JSON keys, tool names, and argument names.** Schema \
keys like `target_lang`, `bucket_years`, `author_regex` MUST be copied \
character-for-character. Never substitute non-ASCII (Chinese / Cyrillic / \
emoji) inside argument names — `target_lang` is one valid token, `target身` \
or `target_язык` will fail validation. String VALUES (the user-facing \
clarify text) can use any language; only KEYS and SCHEMA NAMES are \
ASCII-strict.
10. **Cap parallel fan-out at 10 steps.** Even if the user asks for «все \
слова» / «all words» / «top 100», emit AT MOST 10 enrich_word / \
find_words_by_etymology / word_freq_timeline steps in a single plan. \
Chat timeout is 90 s; per-step LLM/Wiktionary calls add up. If the user \
genuinely wants more, return 10 + a clarify offering «next 10» in the \
plan's rationale OR fewer steps + the rationale «capped at 10 of N for \
chat timeout; user can ask for next batch».
11. **Total plan steps cap at 12.** Validator rejects plans with >12 steps.

The user's language might be Russian, English, or mixed. Match the \
clarify language to the user's. JSON keys/values stay technical \
(ASCII per rule 9).

# AVAILABLE TOOLS
{catalog}

# EXAMPLES
{examples}
"""


def build_planner_prompt(*,
                          registry: Optional[dict] = None,
                          examples: Optional[list[dict]] = None,
                          ) -> str:
    """Assemble the full system prompt for the v4 LLM planner."""
    catalog = render_catalog(build_catalog(registry=registry))
    ex = render_examples(examples)
    return SYSTEM_PROMPT_TEMPLATE.format(catalog=catalog, examples=ex)


__all__ = [
    "CatalogEntry",
    "SYSTEM_PROMPT_TEMPLATE",
    "build_catalog",
    "build_planner_prompt",
    "few_shot_examples",
    "render_catalog",
    "render_examples",
]
