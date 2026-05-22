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
# AST-fingerprint utilities (R-23 Tier 1A)
# ============================================================


def _source_or_empty(fn: Callable) -> str:
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def ast_fingerprint(*fns) -> str:
    """Stable 12-char hex hash of the concatenated source of each fn.

    Used as part of the cache-key (`scripts/v2/cache.py`) so that touching
    the wrapper OR the v1 it calls invalidates that tool's cache without
    a manual `wrapper_version` bump.

    Bypasses comments by hashing the AST `ast.dump(parse(src))` — this
    keeps the fingerprint stable across whitespace/comment edits but
    flips on any structural change. Accepts either callables or
    `"module.attr"` strings (resolved lazily — failures fall back to
    the string itself in the hash).
    """
    import ast
    parts: list[str] = []
    for fn in fns:
        resolved = fn
        if isinstance(fn, str):
            try:
                import importlib
                module_path, _, attr = fn.rpartition(".")
                resolved = getattr(importlib.import_module(module_path), attr)
            except Exception:
                parts.append(f"unresolved:{fn}")
                continue
        src = _source_or_empty(resolved)
        if not src:
            qn = getattr(resolved, "__qualname__", "?")
            mod = getattr(resolved, "__module__", "?")
            parts.append(f"unsource:{mod}.{qn}")
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            parts.append(src)
            continue
        parts.append(ast.dump(tree, annotate_fields=False))
    blob = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def wrapper_fingerprint_for_tool(tool_name: str) -> str | None:
    """Return AST-fingerprint of (wrapper, v1_fn) for the named tool.

    Looks up via the tool registry → finds the matching contract binding
    by wrapper function identity. Returns None if the tool isn't bound
    to a contract (e.g. legacy or v2-native composite).
    """
    try:
        from scripts.v2.tool_registry import REGISTRY
    except ImportError:
        return None

    spec = REGISTRY.get(tool_name)
    if spec is None:
        return None

    # Walk the binding table — match by the registered fn identity.
    target = spec.fn
    # The decorator wraps with @functools.wraps; the real wrapper sits
    # behind __wrapped__ on the proxy.
    target_inner = getattr(target, "__wrapped__", target)
    for binding in V1_CONTRACTS.values():
        candidate = binding.wrapper_fn
        candidate_inner = getattr(candidate, "__wrapped__", candidate)
        if candidate is target or candidate_inner is target_inner:
            return ast_fingerprint(binding.wrapper_fn, binding.v1_fn)
    return None


__all__ = [
    "V1_CONTRACTS",
    "ContractBinding",
    "register_contract",
    "ast_fingerprint",
    "wrapper_fingerprint_for_tool",
]
