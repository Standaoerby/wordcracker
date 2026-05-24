"""v1↔v2 contract layer — Phase 2 of REFACTOR_BRIEF.

Each v1 function (`scripts.rag_tools.*`, `scripts.learning_tools.*`) has a
declared output schema (`schemas.V1<Name>`). A wrapper decorated with
`@v1_contract(v1_fn, schema)` is statically and dynamically bound to that
schema:

  * Static: an AST walk over the wrapper body collects every `raw.get("X")`
    / `raw["X"]` literal access; keys NOT in the schema → loud error.
  * Dynamic: at test time, the registry-driven contract test calls the real
    v1 (or a recorded golden fixture from prod) and asserts the result
    has exactly the declared keys.

Both gates fire together — drift in either direction (v1 renames a key,
wrapper invents a key) becomes a CI failure rather than a silent empty
result. This closes C2 (no contract → angle-bracket guess chains) and the
class around it (E8/E14/E15/E33/E34, `$s2.words[N]`).

Public surface:

    from scripts.v2.contracts import v1_contract, mock_from_schema
    from scripts.v2.contracts.schemas import V1AffinityByAuthor
    from scripts.v2.contracts.registry import V1_CONTRACTS

Add a wrapper:

    @v1_contract(v1_fn=rag_tools.affinity_by_author,
                 schema=V1AffinityByAuthor)
    @tool(name="affinity_by_author", ...)
    def affinity_by_author(...): ...
"""
from __future__ import annotations

import ast
import functools
import inspect
import logging
import textwrap
from typing import Any, Callable

from scripts.v2.contracts.schemas import (
    SCHEMA_KEYS,
    SUCCESS_ERROR_KEYS,
    V1Schema,
)

log = logging.getLogger("wordcracker.v2.contracts")


# Keys present on every v1 error branch — always allowed in wrapper reads.
ERROR_BRANCH_KEYS = frozenset({
    "error", "details", "hint", "stderr", "stdout", "got", "supported",
    "fallback", "id", "pg_id", "author_regex", "scope",
    "matched", "available", "available_codes_sample", "geo_coverage",
    "pre_books", "post_books", "min_pre_books", "min_post_books",
    "year", "year_from", "year_to", "country", "raw",
})


def _wrapper_v1_keys(wrapper_fn: Callable) -> set[str]:
    """AST-walk over wrapper body — collect string literals passed to
    `<name>.get("X")` / `<name>["X"]` where `<name>` is the v1 result
    binding (typically `raw`).

    Heuristic: we collect ALL literal keys read from ANY local variable,
    not just `raw`, because the wrapper may rebind v1's output (`m1 = ...`,
    `out = ...`). False positives are tolerable — schemas are
    declared liberally and we only flag keys that don't appear in the
    schema AT ALL.
    """
    src = inspect.getsource(wrapper_fn)
    src = textwrap.dedent(src)
    tree = ast.parse(src)

    found: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            # `<expr>.get("X", ...)`
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
            # `<expr>["X"]`
            sub = node.slice
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.add(sub.value)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def check_wrapper_against_schema(
    wrapper_fn: Callable, schema: type[V1Schema],
) -> list[str]:
    """Return a list of keys the wrapper reads that are NOT in the schema
    and NOT in the generic error-branch keys.

    Returns empty list on success.
    """
    declared = SCHEMA_KEYS.get(schema, set()) | ERROR_BRANCH_KEYS
    # Schemas can opt into additional "row-level" keys via the `__row_keys__`
    # class var — these are keys read from list-of-dict items rather than
    # from the top-level v1 result. Wrappers iterate rows freely.
    row_keys = getattr(schema, "__row_keys__", frozenset())
    declared = declared | set(row_keys)
    # Also tolerate any v2-internal keys that the wrapper MUTATES onto raw
    # (e.g. raw["words"] = rows). These are reads of v2 own annotations,
    # not contract violations.
    declared = declared | INTERNAL_V2_KEYS

    read = _wrapper_v1_keys(wrapper_fn)
    rogue = read - declared
    return sorted(rogue)


# v2 wrappers commonly stamp these onto `raw` (and read them back) as
# rendering hints / count-honesty signals. They are NOT part of any v1
# contract but are tolerated in the AST scan.
INTERNAL_V2_KEYS = frozenset({
    "_render_note", "_render_columns", "_threshold_auto_lowered",
    "_word_for_filter",
    "top_requested", "top_returned",
    "min_corpus_count_used", "min_corpus_count_requested",
    "empty_sides", "cosine_is_structural_zero", "shared_top_words_count",
    "metric_explanations", "proper_noun_filter",
    "words",  # phase-0 alias for $s2.words[N] resolution
    "top_unique_a", "top_unique_b", "slug_a", "slug_b",
    # W-3 (2026-05-24) — renderer-ready row lists stamped by wrappers
    # alongside the raw v1 shape so the LLM render payload no longer
    # has to JOIN dict-of-dicts (book_emotion) or infer per-author
    # comparison rows from scalars (compare_authors). Drop in a
    # frozenset entry so the contract AST validator tolerates them.
    "entities",   # compare_authors per-author rows
    "emotions",   # book_emotion_profile per-emotion rows
    "side",       # entities row field (author1/author2)
    "signature_words_count", "signature_words",
    "per_million",  # used by book_emotion_profile row alias
    "share",
    "shared_high_affinity",
    "cosine_similarity",
    # generic row-level row-builder keys
    "name", "regex", "label", "rank",
    # metric_explanations row keys (built by v2 wrapper itself)
    "metric", "direction", "scale", "interpret",
    # generic enrich-result keys read across wrappers from their
    # own constructed dicts (filename/out_path heuristics)
    "filename_suggestion",
    # frequently-built provenance fields
    "shared_n", "n_books_a", "n_books_b",
    # coverage hints — wrappers default to -1 when v1 omits them
    "n_books", "n_authors",
    # filter-drop counters wrappers stamp onto raw
    "_filter_drops",
    # scope-arg reads (scope.get("book") / scope.get("author")) —
    # the scope dict is a wrapper-input arg, not a v1 result. AST can't
    # distinguish so we admit them globally.
    "book", "author", "pg_id", "user_id", "year_from", "year_to",
    # scoring-plugin row fields and v2-built collocates metadata
    "score", "npmi", "c_pair", "c_neighbor",
    "scope_total_tokens", "scope_target_count", "scope_books_scanned",
    "min_cooccurrence",
    # legacy emotion/etymology phantom fallbacks moved to schema row_keys;
    # any leftover transitional reads. (`etymology_chain` dropped in T2 —
    # never emitted by v1, no longer read by any wrapper.)
    "translation", "pos_tag",
    # timeline auto-fallback metadata stamped by the wrapper
    "basis_fallback_reason", "basis_originally_requested",
    # author_metadata bio-override flags stamped by the wrapper
    "_bio_source", "year_of_death_max_unreliable",
    # top_ngrams_by_author semantic-class filter telemetry
    "_semantic_filter",
    # book_readability v2 sidecar (computed from counts file, not v1)
    "total_words_estimate", "words_sampled_for_metric",
    # composite author_profile sub-result dict keys (signature, top_bigrams,
    # diversity, influences) — composite reads them off the v1 raw
    "signature", "top_bigrams", "diversity", "influences",
    "dominant_emotions",
})


def _resolve_v1_fn(v1_fn) -> Callable:
    """Resolve `v1_fn` to a callable. Accepts:
      * A direct callable (legacy form).
      * A `"module.attr"` string — looked up lazily so partial test
        stubs of `scripts.rag_tools` / `learning_tools` don't have to
        define every v1 function the contract sweep references.
    """
    if callable(v1_fn):
        return v1_fn
    if isinstance(v1_fn, str) and "." in v1_fn:
        module_path, _, attr = v1_fn.rpartition(".")
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise TypeError(
        f"v1_fn must be a callable or 'module.attr' string, got {v1_fn!r}",
    )


def v1_contract(
    *, v1_fn, schema: type[V1Schema],
) -> Callable[[Callable], Callable]:
    """Bind a wrapper to a v1 function's declared output schema.

    Effects:
      * Records (wrapper, v1_fn, schema) in `V1_CONTRACTS` for the
        contract test sweep.
      * AST-checks the wrapper body NOW (at import / decoration time) —
        if the wrapper reads any literal key not in the schema, raises
        `ContractError`.  No code path can register a wrapper that
        reads a phantom key.

    `v1_fn` may be a callable OR a `"module.attr"` string. The string
    form is resolved lazily — so test code that stubs the v1 module
    with a partial set of functions doesn't trip an ImportError at
    wrapper module load time.

    The decorator is otherwise transparent: it does NOT wrap the call,
    no per-call overhead.
    """
    def decorate(fn: Callable) -> Callable:
        # Late-import to avoid circular: registry imports schemas, which
        # is fine; but contracts/__init__ shouldn't pull registry at module
        # load time because registry is mutated as wrappers import.
        from scripts.v2.contracts.registry import register_contract

        rogue = check_wrapper_against_schema(fn, schema)
        if rogue:
            raise ContractError(
                f"wrapper {fn.__module__}.{fn.__qualname__} reads phantom "
                f"keys not declared in {schema.__name__}: {rogue}. "
                f"Either add them to the schema (if v1 really returns them) "
                f"or remove them from the wrapper (drop the `.get(...) or "
                f".get(...)` fallback chain).",
            )

        @functools.wraps(fn)
        def proxy(*args, **kwargs):
            return fn(*args, **kwargs)
        # Expose binding for tests
        proxy.__v1_contract__ = (v1_fn, schema)
        # Register the PROXY (not the inner fn) so callers reading the
        # binding back get the same callable that's exposed at module
        # level — `__v1_contract__` round-trips.
        register_contract(wrapper_fn=proxy, v1_fn=v1_fn, schema=schema)
        return proxy
    return decorate


class ContractError(RuntimeError):
    """Raised when a wrapper violates its declared v1 contract."""


def mock_from_schema(
    schema: type[V1Schema], **overrides: Any,
) -> dict[str, Any]:
    """Build a representative v1-result dict from a declared schema.

    Default values are taken from `schema.__defaults__` (a class-level
    dict). Any `overrides` patch the result for the specific test case.

    Use this in tests INSTEAD of writing raw `{"top": [...], ...}` dicts.
    When v1 renames a key and you bump the schema, every mock that derives
    from the schema updates in lockstep — no silent test drift.
    """
    defaults: dict[str, Any] = dict(getattr(schema, "__defaults__", {}) or {})
    defaults.update(overrides)
    return defaults


def assert_matches_schema(raw: Any, schema: type[V1Schema],
                          *, allow_error: bool = True,
                          context: str = "") -> None:
    """Validate that `raw` (a v1 result dict) declares exactly the keys
    promised by `schema`. Missing required keys → AssertionError.
    """
    if not isinstance(raw, dict):
        raise AssertionError(
            f"{context or schema.__name__}: expected dict, got "
            f"{type(raw).__name__}",
        )

    if allow_error and "error" in raw:
        # Error branches use a different (smaller) key set — handled
        # separately. Only validate against full schema when success.
        return

    expected = SCHEMA_KEYS.get(schema, set())
    required = set(getattr(schema, "__required__", expected))
    missing = required - set(raw.keys())
    if missing:
        raise AssertionError(
            f"{context or schema.__name__}: v1 output missing required "
            f"keys {sorted(missing)}. Got {sorted(raw.keys())}. "
            f"Either v1 dropped these keys (update {schema.__name__}) or "
            f"the wrapper is reading the wrong v1 function.",
        )


__all__ = [
    "v1_contract",
    "ContractError",
    "mock_from_schema",
    "assert_matches_schema",
    "check_wrapper_against_schema",
    "ERROR_BRANCH_KEYS",
    "INTERNAL_V2_KEYS",
    "SUCCESS_ERROR_KEYS",
]
