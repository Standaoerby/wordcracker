"""Operational flag registry — the narrow scope of ADR-B5 (S-B4).

This module is *the allowlist*, not an indirection layer. Code still
reads ``os.environ.get("WC_X", "on")`` directly; the registry's only
role is to declare which binary-toggle flags are intentionally
code-default-on (no compose entry) versus experimental (no compose
entry yet) versus dead (must be removed).

The lint test ``tests/v2/test_flag_lint.py`` walks ``scripts/`` for
``os.environ.get("WC_*", <binary-default>)`` reads and asserts each
toggle is either (a) live in ``docker-compose.yml`` ``environment:``,
or (b) listed below. Configuration values (paths, model names, ints,
floats) are explicitly out of scope of the lint — they are values, not
flags.

The full ``env_registry.py`` indirection from the 2026-05-24 Proposed
ADR-B5 (~26 reads migrated) is carried forward as B5-Phase 2. The
current module is intentionally tiny: ship the allowlist that catches
the harm class today; defer the structured index until S-B4 has baked.

To add a new entry: append to ``CODE_DEFAULT_ON`` or ``EXPERIMENTAL``
with a one-line rationale and the path:line where the read lives.
"""
from __future__ import annotations

from typing import Final

# Toggles whose default is intentionally baked in code (``"on"`` /
# ``"off"`` literal). Not set in compose by design — flipping them is a
# code change, not a config tweak. Each entry: name → "owning module —
# rationale". Keep entries lowercase-keyed by flag name.
CODE_DEFAULT_ON: Final[dict[str, str]] = {
    "WC_CRITIC": (
        "scripts/v2/critic.py — critic LLM verification pass; on for "
        "prod since D-P1-7. Off via ad-hoc env when debugging the "
        "critic itself."
    ),
    "WC_NUMERIC_AUDIT": (
        "scripts/v2/numeric_audit.py — number-vs-source audit on "
        "rendered answers; on for prod since D-P1-7. Off via ad-hoc "
        "env when measuring critic-only effects."
    ),
    "WC_CACHE_AST_INVALIDATION": (
        "scripts/v2/cache.py — AST-hash cache invalidation (ADR-F1 / "
        "D67, S-F1, 2026-05-25). On = wrapper/v1-callee source flips "
        "cache_key automatically (E18 fix). Off = revert to "
        "(schema, wrapper_version, args) only, the pre-S-F1 contract. "
        "Permanent kill-switch — flipping off is a developer-debugging "
        "action when an AST-walk regression is suspected."
    ),
}

# Flags that are honestly experimental — opt-in only, default off, no
# compose entry. The lint allows them through. Each entry: name →
# "owning module — rationale + when to expect promotion or removal".
# Empty today; first honestly-experimental flag lands here.
EXPERIMENTAL: Final[dict[str, str]] = {}


def is_known_flag(name: str) -> bool:
    """True iff ``name`` is declared in either registry. Used by the
    lint to decide whether an out-of-compose binary toggle is dark
    code (fail) or known-and-declared (allow)."""
    return name in CODE_DEFAULT_ON or name in EXPERIMENTAL
