"""Disk-backed + in-process cache for v2 tool results.

Contract: docs/v2/SPECS.md §6.

Key invariants:
  * Cache key is (tool_name, sorted JSON of args). FilterSpec args get
    converted to their dict form before hashing.
  * Disk entries are tagged with corpus_version. When the version changes,
    entries become "stale" — readers still get the result with a
    ToolWarning("stale_cache") attached. They can ignore the warning if
    the data class is version-independent (etymology, lemma_profile) or
    re-run if it matters (heavy queries).
  * In-process LRU is a thin layer above disk to dodge JSON parse cost on
    rapid repeat calls (e.g. clarify → retry sequences).

The router calls `cache_get(name, args)` → `Optional[ToolResult]` and
`cache_put(name, args, result)` around `dispatch()` when ToolSpec.cacheable
is true. This module is opt-in — calling code can ignore it entirely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.v2.corpus_version import current_source_info
from scripts.v2._types import (
    Coverage, SourceInfo, ToolError, ToolResult, ToolWarning,
)

log = logging.getLogger("wordcracker.v2.cache")

# ---- config ----

CACHE_ROOT = Path(os.environ.get("WC_V2_CACHE_DIR", "/data/v2_cache"))
LRU_SIZE = 512

# v3.3.1 — global cache schema version. Bump on any change that affects
# ToolResult shape (new fields, enum reshuffles, dataclass refactors).
# Folded into cache_key so old entries under different schema become
# automatically unreachable without manual cache eviction.
#
#   "v1"        — pre-v5 (no view / data_validity in ToolResult)
#   "v2-views"  — v5 Phase 2+ tools emit view + data_validity
#   "v3-ast-fp" — Phase 2 (REFACTOR_BRIEF): AST-fingerprint of
#                  (wrapper, v1_callee) folded into key. Editing either
#                  source invalidates cache automatically — no more
#                  manual `wrapper_version` bumps to track.
CACHE_SCHEMA_VERSION = "v3-ast-fp"

# Per-tool TTL in seconds. Tools not listed = infinite (corpus_version is the
# only invalidator). Etymology stays forever (Wiktionary is immutable for
# practical purposes); enrich_word ~30d.
TTL_BY_TOOL = {
    "word_etymology": None,          # Wiktionary contents are stable
    "enrich_word": 30 * 86400,       # LLM-generated, refresh monthly
    "find_book": 7 * 86400,          # metadata can shift when uploads land
    "author_metadata": 7 * 86400,
}

# ---- in-process LRU ----

_lru: "OrderedDict[str, ToolResult]" = OrderedDict()
_lru_lock = threading.Lock()


def _normalize_args(args: dict) -> str:
    """Stable JSON for cache key — keys sorted, dataclasses dumped, None dropped."""
    def _serializable(v: Any) -> Any:
        # FilterSpec / SourceInfo / Coverage are dataclasses but most callers
        # pass plain dicts already; handle both for robustness.
        if hasattr(v, "__dataclass_fields__"):
            return asdict(v)
        return v
    cleaned = {k: _serializable(v) for k, v in args.items() if v is not None}
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str)


def _ast_invalidation_on() -> bool:
    """Lazy read of WC_CACHE_AST_INVALIDATION (ADR-F1 kill-switch).

    Default is on (production state). Off short-circuits the AST
    walk: `cache_key` falls back to the pre-S-F1 contract,
    `(schema, wrapper_version, args)` only. Declared in
    [flag_registry.CODE_DEFAULT_ON](flag_registry.py) per ADR-B5's
    third category; the flag-lint test enforces R1 ("no dark code").
    """
    val = os.environ.get("WC_CACHE_AST_INVALIDATION", "on").strip().lower()
    return val not in ("0", "off", "false", "no")


def _pin_fingerprint_mode() -> bool:
    """Lazy read of WC_CACHE_PIN_FINGERPRINT (ADR-F1 startup snapshot).

    When on, the first miss for any tool triggers a full registry sweep;
    the resulting fingerprint dict is frozen for the process lifetime.
    Two guarantees: (a) prod canary observes a deterministic key set
    even under module hot-reload; (b) `inspect.getsource` per-miss cost
    is amortized at warm-up. Default off — lazy lookup, one entry per
    cache miss. D-SF1-2 declares this is a mode selector, not a
    flag-lint-scoped binary toggle.
    """
    val = os.environ.get("WC_CACHE_PIN_FINGERPRINT", "0").strip().lower()
    return val in ("1", "on", "true", "yes")


_AST_FP_CACHE: dict[str, str] = {}
_AST_FP_PINNED: bool = False
_AST_FP_LOCK = threading.Lock()


def _populate_ast_fp_snapshot() -> None:
    """Walk the full REGISTRY once and freeze every tool's fingerprint.

    Idempotent: subsequent calls are a no-op. Failures for individual
    tools are logged and recorded as empty-string in the snapshot
    (equivalent to "no AST contribution" — cache falls back to
    `wrapper_version`-only for that tool).
    """
    global _AST_FP_PINNED
    with _AST_FP_LOCK:
        if _AST_FP_PINNED:
            return
        try:
            from scripts.v2.tool_registry import REGISTRY
        except ImportError as e:
            log.warning("pin-fingerprint: registry import failed: %s", e)
            _AST_FP_PINNED = True
            return
        try:
            from scripts.v2.contracts.registry import (
                wrapper_fingerprint_for_tool,
            )
        except ImportError as e:
            log.warning("pin-fingerprint: contracts import failed: %s", e)
            _AST_FP_PINNED = True
            return
        for tool_name in list(REGISTRY):
            if tool_name in _AST_FP_CACHE:
                continue
            try:
                _AST_FP_CACHE[tool_name] = (
                    wrapper_fingerprint_for_tool(tool_name) or ""
                )
            except Exception as e:
                log.debug(
                    "pin-fingerprint: lookup failed for %s: %s", tool_name, e,
                )
                _AST_FP_CACHE[tool_name] = ""
        _AST_FP_PINNED = True
        log.info(
            "cache fingerprints pinned for %d tools (WC_CACHE_PIN_FINGERPRINT)",
            len(_AST_FP_CACHE),
        )


def _ast_fingerprint_for(tool: str) -> str:
    """Compute an AST fingerprint of (wrapper_fn, v1_callee + depth=1
    callees) for `tool`. See ADR-F1 in
    [docs/v2/decisions.md](../../docs/v2/decisions.md).

    Returns "" when the tool isn't in the registry or the lookup fails.
    Process-cached: fingerprints derive from source on disk which is
    immutable for the lifetime of a deployed image. When
    `WC_CACHE_PIN_FINGERPRINT=1`, the full registry is snapshotted at
    first call.
    """
    if _pin_fingerprint_mode() and not _AST_FP_PINNED:
        _populate_ast_fp_snapshot()

    cached = _AST_FP_CACHE.get(tool)
    if cached is not None:
        return cached

    try:
        from scripts.v2.contracts.registry import wrapper_fingerprint_for_tool
        fp = wrapper_fingerprint_for_tool(tool) or ""
    except Exception as e:
        log.debug("ast fingerprint lookup failed for %s: %s", tool, e)
        fp = ""
    _AST_FP_CACHE[tool] = fp
    return fp


def _reset_ast_fp_cache_for_tests() -> None:
    """Test helper: drop the fingerprint snapshot.

    Tests that toggle `WC_CACHE_PIN_FINGERPRINT` between cases or
    monkey-patch source need to clear the cached fingerprints so the
    next call re-resolves. Production code never calls this.
    """
    global _AST_FP_PINNED
    with _AST_FP_LOCK:
        _AST_FP_CACHE.clear()
        _AST_FP_PINNED = False


def cache_key(tool: str, args: dict, wrapper_version: str = "v1") -> str:
    norm = _normalize_args(args)
    # ADR-F1 (S-F1, 2026-05-25) — AST fingerprint of the wrapper + its
    # v1 callee + their depth=1 same-`scripts.`-module callees. Touching
    # any of those sources flips the fingerprint, so the next call after
    # deploy reads through-not-from-cache automatically. Previously
    # `wrapper_version` had to be bumped by hand and was forgotten on
    # ~10 of 37 tools (commit 2a958f8 retroactively swept those).
    # Kill-switch: `WC_CACHE_AST_INVALIDATION=off` reverts to the
    # pre-S-F1 contract.
    ast_fp = _ast_fingerprint_for(tool) if _ast_invalidation_on() else ""
    h = hashlib.sha256(
        (f"{CACHE_SCHEMA_VERSION}\0{wrapper_version}\0{ast_fp}\0" + norm)
        .encode("utf-8")
    ).hexdigest()[:16]
    # v3.3.1 — "__" separator (was ":") — NTFS reserves ":" for ADS, so
    # the old key shape broke any Windows-side dev/testing of cache.
    # Schema bump already invalidates old entries, so renaming here is
    # safe (no migration needed).
    return f"{tool}__{h}"


# ---- disk layer ----


def _disk_path(tool: str, key: str) -> Path:
    # Two-level fan-out so a single directory doesn't get 100k+ entries.
    return CACHE_ROOT / tool / key[:2] / f"{key}.json"


def _read_disk(tool: str, key: str) -> dict | None:
    p = _disk_path(tool, key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cache read failed for %s: %s", key, e)
        return None


def _write_disk(tool: str, key: str, payload: dict) -> None:
    # Race-safe: previous code computed `tmp = p.with_suffix(".tmp")` —
    # a fixed name per cache_key. Two ThreadingHTTPServer threads
    # writing the same key both targeted the same .tmp; first
    # replace() won, the second's source was gone → ENOENT (prod
    # 2026-05-24, two concurrent top_ngrams_by_author calls). Silent
    # corruption when the race went unobserved: thread A's payload
    # could land in B's .tmp before B's replace ran, so the cached
    # bytes were the wrong thread's. NamedTemporaryFile(delete=False)
    # gives each writer a unique name in the same directory, so the
    # final replace() is an independent atomic rename per writer.
    p = _disk_path(tool, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("cache mkdir failed for %s: %s", key, e)
        return
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            prefix=f"{p.stem}.", suffix=".tmp",
            dir=str(p.parent), delete=False,
        ) as tmp_f:
            json.dump(payload, tmp_f, ensure_ascii=False, default=str)
            tmp_path = Path(tmp_f.name)
        tmp_path.replace(p)
        tmp_path = None  # ownership transferred to p; skip cleanup
    except OSError as e:
        log.warning("cache write failed for %s: %s", key, e)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---- public API ----


def cache_get(tool: str, args: dict,
              wrapper_version: str = "v1") -> ToolResult | None:
    """Look up a cached ToolResult. Returns None on miss / stale-incompatible."""
    key = cache_key(tool, args, wrapper_version=wrapper_version)

    with _lru_lock:
        cached = _lru.get(key)
        if cached is not None:
            _lru.move_to_end(key)
            return cached.with_cache_hit(True)

    raw = _read_disk(tool, key)
    if raw is None:
        return None

    # TTL check
    ttl = TTL_BY_TOOL.get(tool)
    stamp = raw.get("_cached_at")
    if ttl is not None and stamp is not None and (time.time() - stamp) > ttl:
        return None

    # Corpus version check — entries from a stale corpus are still returned
    # but tagged with a warning. The caller can decide whether to honor.
    current_cv = current_source_info()
    stale = (raw.get("_corpus_version") != current_cv.corpus_version)

    try:
        result = _from_payload(raw, tool=tool, stale=stale)
    except Exception as e:
        log.warning("cache deserialize failed for %s: %s", key, e)
        return None

    with _lru_lock:
        _lru[key] = result
        if len(_lru) > LRU_SIZE:
            _lru.popitem(last=False)
    return result.with_cache_hit(True)


def cache_put(tool: str, args: dict, result: ToolResult,
              wrapper_version: str = "v1") -> None:
    if not result.ok:
        # Don't poison the cache with errors.
        return
    key = cache_key(tool, args, wrapper_version=wrapper_version)
    payload = result.to_dict()
    payload["_cached_at"] = time.time()
    payload["_corpus_version"] = (
        result.source_info.corpus_version if result.source_info else "unknown"
    )

    with _lru_lock:
        _lru[key] = result
        if len(_lru) > LRU_SIZE:
            _lru.popitem(last=False)

    _write_disk(tool, key, payload)


def cache_stats() -> dict:
    """For status_server / observability."""
    with _lru_lock:
        lru_size = len(_lru)
    disk_count = 0
    if CACHE_ROOT.exists():
        try:
            disk_count = sum(1 for _ in CACHE_ROOT.rglob("*.json"))
        except OSError:
            pass
    return {"lru_size": lru_size, "lru_capacity": LRU_SIZE,
            "disk_entries": disk_count, "cache_root": str(CACHE_ROOT)}


def cache_clear() -> None:
    """For tests + manual ops."""
    with _lru_lock:
        _lru.clear()


# ---- payload reconstruction ----


def _from_payload(raw: dict, *, tool: str, stale: bool) -> ToolResult:
    """Rebuild a ToolResult from disk JSON."""
    warnings_data = raw.get("warnings", []) or []
    warnings = [ToolWarning(code=w["code"], message=w["message"],
                            details=w.get("details", {}))
                for w in warnings_data]
    if stale:
        warnings.append(ToolWarning(
            code="stale_cache",
            message="cache entry from older corpus_version; result may be outdated",
            details={"cached_version": raw.get("_corpus_version", "unknown")},
        ))

    cov_data = raw.get("coverage") or {}
    coverage = Coverage(
        books_matched=cov_data.get("books_matched", -1),
        books_total=cov_data.get("books_total", -1),
        tokens_analyzed=cov_data.get("tokens_analyzed"),
        metadata_completeness=cov_data.get("metadata_completeness", {}),
    )

    src_data = raw.get("source_info") or {}
    source_info = SourceInfo(
        corpus_version=src_data.get("corpus_version", "unknown"),
        analytics_version=src_data.get("analytics_version", "unknown"),
        spgc_baseline=src_data.get("spgc_baseline", "SPGC-2018-07-18"),
        chroma_collection=src_data.get("chroma_collection"),
    ) if src_data else None

    err_data = raw.get("error")
    error = ToolError(
        type=err_data["type"], message=err_data["message"],
        details=err_data.get("details", {}),
        retryable=err_data.get("retryable", False),
    ) if err_data else None

    # v3.3.1 — restore v5 fields (view + data_validity) across cache
    # roundtrip. ROOT CAUSE of Stage 3 silent view emission failure
    # 2026-05-21: cache hits returned ToolResult with view=None because
    # this function ignored the JSON-serialized "view" / "data_validity"
    # fields entirely. select_primary_view found no views → ERROR_FRIENDLY
    # fallback for every query.
    view = None
    view_data = raw.get("view")
    if view_data:
        try:
            from scripts.v2.view_types import RenderableView
            view = RenderableView.from_dict(view_data)
        except Exception as e:
            log.warning("cache view restore failed for %s: %s", tool, e)
            view = None

    data_validity = None
    validity_str = raw.get("data_validity")
    if validity_str:
        try:
            from scripts.v2.view_types import DataValidity
            data_validity = DataValidity(validity_str)
        except (ValueError, ImportError) as e:
            log.debug("cache data_validity restore failed for %s: %s", tool, e)
            data_validity = None

    return ToolResult(
        ok=raw.get("ok", True),
        tool=raw.get("tool", tool),
        query=raw.get("query", {}),
        data=raw.get("data"),
        warnings=warnings,
        coverage=coverage,
        source_info=source_info,
        runtime_ms=raw.get("runtime_ms", 0),
        cache_hit=False,  # overridden by caller
        error=error,
        view=view,
        data_validity=data_validity,
    )
