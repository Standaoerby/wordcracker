"""V1_CONTRACTS — registry of (wrapper, v1_fn, schema) triples.

Populated by `@v1_contract(...)` at wrapper import time. The contract sweep
in `tests/v2/test_v1_contracts.py` iterates this dict.
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from typing import Callable

from scripts.v2.contracts.schemas import V1Schema


@dataclass(frozen=True)
class ContractBinding:
    wrapper_fn: Callable
    # v1_fn may be Callable or "module.attr" string — see _resolve_v1_fn.
    v1_fn: object
    schema: type[V1Schema]
    wrapper_qualname: str = field(default="")
    v1_qualname: str = field(default="")
    schema_name: str = field(default="")

    def resolved_v1_fn(self) -> Callable:
        """Resolve the v1 callable lazily, supporting late-bound modules."""
        if callable(self.v1_fn):
            return self.v1_fn
        if isinstance(self.v1_fn, str) and "." in self.v1_fn:
            import importlib
            module_path, _, attr = self.v1_fn.rpartition(".")
            return getattr(importlib.import_module(module_path), attr)
        raise TypeError(f"unresolvable v1_fn: {self.v1_fn!r}")


V1_CONTRACTS: dict[str, ContractBinding] = {}


def register_contract(
    *,
    wrapper_fn: Callable,
    v1_fn: object,
    schema: type[V1Schema],
) -> None:
    """Add (wrapper, v1_fn, schema) to the registry.

    Key is the wrapper's qualname so a v2 tool name doesn't need to be
    threaded through (a single v1 function may back multiple v2 tools).
    Re-registering the same wrapper is a no-op (test-suite hot reload).

    `v1_fn` may be a callable (eager resolution) or a `"module.attr"`
    string (lazy resolution, preferred for wrappers so test-time module
    stubs don't have to define every v1 function).
    """
    key = f"{wrapper_fn.__module__}.{wrapper_fn.__qualname__}"
    if key in V1_CONTRACTS:
        return
    if callable(v1_fn):
        v1_qualname = f"{v1_fn.__module__}.{v1_fn.__qualname__}"
    else:
        v1_qualname = str(v1_fn)
    V1_CONTRACTS[key] = ContractBinding(
        wrapper_fn=wrapper_fn,
        v1_fn=v1_fn,
        schema=schema,
        wrapper_qualname=key,
        v1_qualname=v1_qualname,
        schema_name=schema.__name__,
    )


# ============================================================
# AST-fingerprint utilities (R-23 Tier 1A · ADR-F1 · D67)
# ============================================================
#
# Cache-key contract (`scripts/v2/cache.py`):
#
#   hash(CACHE_SCHEMA_VERSION ∥ wrapper_version ∥ ast_fp ∥ norm_args)
#
# `ast_fp` here is the SHA-256 of `ast.dump(ast.parse(getsource(fn)))`
# for the wrapper, its bound v1 function (when `@v1_contract` is
# present), AND each of their depth=1 same-`scripts.`-module callees.
# Editing a wrapper body OR a shared helper (e.g. `_title_lookup`)
# automatically flips the fingerprint — no manual `wrapper_version`
# bump required for behaviour-only changes.
#
# Depth=1 ceiling is deliberate (ADR-F1 D-SF1-3): depth=N would over-
# invalidate; depth=0 misses `_title_lookup`-class shared helpers.


def _source_or_empty(fn: Callable) -> str:
    """`inspect.getsource(fn)` with try/except for C-extensions / builtins.

    Empty string is interpreted upstream as "no AST contribution" — the
    fingerprint falls back to a stable `unsource:` marker so the tool
    still has a deterministic key.
    """
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def _depth1_callees(fn: Callable) -> list[Callable]:
    """Same-`scripts.`-module callables referenced from `fn` body (depth=1).

    Uses `fn.__code__.co_names` (static-bytecode read) → `fn.__globals__`
    lookup. A callee is "in scope" iff its `__module__` starts with
    `scripts.` — keeps third-party libraries (pandas, spacy, …) out of
    the fingerprint (D-SF1-3). Self-references and duplicates dropped.
    """
    try:
        names = fn.__code__.co_names
        globals_ = fn.__globals__
    except AttributeError:
        return []
    found: list[Callable] = []
    seen_ids: set[int] = set()
    for name in names:
        obj = globals_.get(name)
        if obj is None or not callable(obj):
            continue
        obj_mod = getattr(obj, "__module__", "") or ""
        if not obj_mod.startswith("scripts."):
            continue
        if obj is fn:
            continue
        oid = id(obj)
        if oid in seen_ids:
            continue
        seen_ids.add(oid)
        found.append(obj)
    return found


def _ast_part_for(fn: Callable) -> tuple[str, str]:
    """Return (qualname, ast_dump_or_marker) for one callable.

    The qualname is used to sort parts deterministically before hashing
    so `ast_fingerprint(a, b) == ast_fingerprint(b, a)`.

    Source is `textwrap.dedent`-normalized before parsing so closures
    and nested functions (whose `inspect.getsource` returns indented
    blocks) parse cleanly. Without dedent, any closure would fall into
    the SyntaxError branch and contribute its raw indented source to
    the hash — which is sensitive to whitespace and breaks the
    "cosmetic edits don't flip fp" invariant.
    """
    import ast
    import textwrap
    qualname = (f"{getattr(fn, '__module__', '?')}."
                f"{getattr(fn, '__qualname__', '?')}")
    src = _source_or_empty(fn)
    if not src:
        return qualname, f"unsource:{qualname}"
    src = textwrap.dedent(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return qualname, src
    return qualname, ast.dump(tree, annotate_fields=False)


def ast_fingerprint(*fns) -> str:
    """Stable 12-char hex hash of (`fns` + their depth=1 callees).

    Each `fn` may be a callable or a `"module.attr"` string (lazy
    resolution — string failures contribute `unresolved:<str>` to the
    hash so the cache-key is still deterministic). Depth=1 callees are
    discovered via `fn.__code__.co_names` (see `_depth1_callees`).

    Order-independent: parts are sorted by qualname before concatenation
    so `ast_fingerprint(wrapper, v1_fn) == ast_fingerprint(v1_fn, wrapper)`.

    Acceptance contract (ADR-F1 / S-F1):
      * body edit on `fn` flips the hash;
      * body edit on any depth=1 callee of `fn` flips the hash;
      * whitespace/comment edits on either do NOT flip the hash
        (AST-dump strips them before hashing);
      * a depth=2 (helper-of-a-helper) edit does NOT flip the hash —
        the ceiling is intentional, see D-SF1-3.
    """
    parts: dict[str, str] = {}
    seen_ids: set[int] = set()

    def _resolve(fn) -> Callable | None:
        if isinstance(fn, str):
            try:
                import importlib
                module_path, _, attr = fn.rpartition(".")
                return getattr(importlib.import_module(module_path), attr)
            except Exception:
                parts[f"unresolved:{fn}"] = ""
                return None
        return fn

    def _add(fn: Callable) -> None:
        oid = id(fn)
        if oid in seen_ids:
            return
        seen_ids.add(oid)
        qualname, dump = _ast_part_for(fn)
        # If two distinct callables share a qualname (unlikely; only via
        # dynamic exec) we still keep both by suffixing with id — but
        # this path is unreachable in practice. Normal case: first
        # writer wins.
        if qualname in parts:
            return
        parts[qualname] = dump

    for fn in fns:
        resolved = _resolve(fn)
        if resolved is None:
            continue
        _add(resolved)
        for callee in _depth1_callees(resolved):
            _add(callee)

    # Sort by qualname for determinism — order of fns in the call to
    # ast_fingerprint(...) must not matter.
    blob = "\0".join(parts[k] for k in sorted(parts)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def wrapper_fingerprint_for_tool(tool_name: str) -> str | None:
    """AST-fingerprint of the named tool's wrapper + its dependencies.

    Two paths:

      * **Contract-bound** (`@v1_contract` present): hash
        `(wrapper_fn, v1_fn)` plus each of their depth=1 callees. Catches
        the E18 failure class — a v1 helper edit (`_normalize_lang`,
        `_select_books`, `_title_lookup`) invalidates the cache of every
        wrapper that calls it.
      * **v2-native fallback** (no contract binding, ~5/37 tools at
        S-F1 time — `hybrid_search_books`, `lexical_search_books`,
        `lemma_profile`, `resolve_entity`, `corpus_overview`,
        `find_book_by_topic`): hash `spec.fn` plus its depth=1 callees
        directly. Lifts coverage from 32/37 to 37/37; body edits to
        v2-native tools now invalidate the cache without needing a
        manual `wrapper_version` bump.

    Returns None only when the registry import fails or the tool name
    is not registered (in which case the cache key falls back to
    `wrapper_version`-only, equivalent to pre-S-F1).
    """
    try:
        from scripts.v2.tool_registry import REGISTRY
    except ImportError:
        return None

    spec = REGISTRY.get(tool_name)
    if spec is None:
        return None

    # The @tool / @v1_contract decorators wrap with @functools.wraps;
    # peek behind __wrapped__ when present to compare function identity.
    target = spec.fn
    target_inner = getattr(target, "__wrapped__", target)

    for binding in V1_CONTRACTS.values():
        candidate = binding.wrapper_fn
        candidate_inner = getattr(candidate, "__wrapped__", candidate)
        if candidate is target or candidate_inner is target_inner:
            return ast_fingerprint(binding.wrapper_fn, binding.v1_fn)

    # v2-native fallback (S-F1 / ADR-F1): no @v1_contract binding —
    # fingerprint the registered fn directly so body edits still flip
    # the cache key. Walks the same depth=1 path, so any
    # `scripts.`-module helper the fn calls is folded in too.
    return ast_fingerprint(target_inner)


__all__ = [
    "V1_CONTRACTS",
    "ContractBinding",
    "register_contract",
    "ast_fingerprint",
    "wrapper_fingerprint_for_tool",
]
