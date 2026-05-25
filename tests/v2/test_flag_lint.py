"""S-B4 / ADR-B5 — flag lifecycle lint.

Two rules enforce the "no dark code" contract for binary-toggle env
vars (R1 + R8 in CLAUDE.md, ADR-B5 in decisions.md):

* **Rule 1** — no commented-out ``WC_*`` line in any
  ``docker-compose*.yml``. A flag is either live (uncommented) or
  absent (deleted). "Documentation-grade dead config" is the exact
  failure class that kept ``WC_DEFAULT_ENGINE=v2`` alive in
  ``v2-engine.conf`` for weeks after the code reading it was deleted
  (see 2026-05-24 ADR-B5 context).

* **Rule 2** — every ``os.environ.get("WC_*", "on"|"off"|"0"|"1"|
  "true"|"false")`` read in ``scripts/`` must be either:
    - present as a live key in ``docker-compose.yml`` ``environment:``
      blocks (services.*.environment or x-app-env anchor); OR
    - listed in ``scripts/v2/flag_registry.CODE_DEFAULT_ON`` /
      ``EXPERIMENTAL`` with a rationale.

  Reads that are NOT binary-toggle shape (paths, model names, ints,
  floats, base URLs) are OUT of scope for this lint — they are
  configuration, not flags, and have no dark-code risk.

The mirror direction is also covered: every entry in the registry
must have at least one matching ``os.environ.get("WC_X", "<binary>")``
read in ``scripts/`` — orphan whitelist entries fail the test, so the
registry cannot accumulate stale promises.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# yaml is imported lazily inside _compose_env_keys so that pytest
# COLLECTION (R10) does not fail on a CI runner that has not installed
# PyYAML yet. The collect-only gate must stay decoupled from runtime
# deps; the actual test body imports yaml the moment it is needed.

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
COMPOSE_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.dev.yml",
]


# Binary-toggle defaults that promote a ``WC_*`` read into the lint
# scope. Anything else (paths, models, numbers) is configuration, not
# a flag.
_BINARY_DEFAULTS = {"on", "off", "0", "1", "true", "false"}

# ``os.environ.get("WC_FOO", "on")`` shape, single- or double-quoted
# name and default. Pulls the flag name and its default literal so the
# caller can decide if it's a binary toggle.
_ENV_GET_RE = re.compile(
    r"""os\.environ\.get\(\s*['"](WC_[A-Z0-9_]+)['"]\s*,\s*['"]([^'"]*)['"]\s*\)""",
    re.IGNORECASE,
)


def _iter_py_files() -> list[Path]:
    """All ``scripts/**/*.py`` — the surface ADR-B5 governs. Tests
    themselves and notebooks are excluded by living elsewhere.

    ``flag_registry.py`` is excluded because it IS the registry — its
    docstring contains the pattern ``os.environ.get("WC_X", "on")`` as
    documentation, not as a flag read, and including it would create
    a chicken-and-egg failure on the lint that itself enforces the
    registry's reach.
    """
    excluded = {SCRIPTS_DIR / "v2" / "flag_registry.py"}
    return sorted(p for p in SCRIPTS_DIR.rglob("*.py") if p not in excluded)


def _collect_binary_toggle_reads() -> dict[str, list[str]]:
    """Map ``WC_*`` flag name → list of ``path:line`` sites where
    ``os.environ.get("WC_*", <binary>)`` is read. Only binary defaults
    promote a name into the result — configuration reads (paths,
    models, ints) are filtered out before the map is built.
    """
    out: dict[str, list[str]] = {}
    for py in _iter_py_files():
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _ENV_GET_RE.finditer(line):
                name = m.group(1)
                default = m.group(2).lower().strip()
                if default in _BINARY_DEFAULTS:
                    out.setdefault(name, []).append(
                        f"{py.relative_to(REPO_ROOT)}:{lineno}"
                    )
    return out


def _compose_env_keys(compose_path: Path) -> set[str]:
    """All ``WC_*`` keys live in any service's ``environment:`` mapping
    (or in the ``x-app-env`` YAML anchor used by services). Returns
    only keys (not values) — the lint cares about presence.

    yaml is imported lazily (R10): the test file's collection must
    not depend on PyYAML being installed.
    """
    import yaml  # lazy — see module-level note on R10

    if not compose_path.exists():
        return set()
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    keys: set[str] = set()

    # YAML anchors (`x-app-env: &app-env`) appear at top level and are
    # picked up by `services.*.environment: *app-env`. We collect from
    # both surfaces; PyYAML resolves anchors at load time so they appear
    # under both the original top-level key and any referencing service.
    def _add_from(env_block: object) -> None:
        if isinstance(env_block, dict):
            for k in env_block:
                if isinstance(k, str) and k.startswith("WC_"):
                    keys.add(k)
        elif isinstance(env_block, list):
            for entry in env_block:
                if isinstance(entry, str) and "=" in entry:
                    name = entry.split("=", 1)[0]
                    if name.startswith("WC_"):
                        keys.add(name)

    # Top-level x-app-env anchor.
    for top_key, top_val in data.items():
        if str(top_key).startswith("x-app-env"):
            _add_from(top_val)

    # Per-service environment blocks.
    for svc in (data.get("services") or {}).values():
        if isinstance(svc, dict):
            _add_from(svc.get("environment"))

    return keys


# ---------------------------------------------------------------------------
# Rule 1: no commented WC_* in any docker-compose*.yml
# ---------------------------------------------------------------------------

# Line shape that fails: a leading-comment line that mentions a WC_*
# flag *in a value-position* (`#  WC_FOO=on` or `#  WC_FOO: on`). A
# free-form comment that simply *mentions* a flag name in prose
# (`# WC_FOO is documented in decisions.md`) does not match.
_COMMENTED_FLAG_RE = re.compile(
    r"""^\s*\#[^\n]*?\bWC_[A-Z0-9_]+\s*[:=]""",
    re.MULTILINE,
)


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_commented_wc_flag_in_compose(compose_path: Path):
    """ADR-B5 Rule 1: a commented-out ``WC_*`` value line is dark
    config. Either uncomment it (live) or delete it (dead). The
    v2-engine.conf incident showed that "documentation-grade dead
    config" silently drifts from code reality — caught here so the
    drift cannot accumulate."""
    if not compose_path.exists():
        pytest.skip(f"{compose_path.name} not present")
    text = compose_path.read_text(encoding="utf-8")
    matches = _COMMENTED_FLAG_RE.findall(text)
    assert not matches, (
        f"{compose_path.name} contains commented-out WC_* value lines "
        f"(ADR-B5 Rule 1). Uncomment to make live, or delete. "
        f"Found: {matches}"
    )


# ---------------------------------------------------------------------------
# Rule 2: every binary-toggle read is either live in compose or registered
# ---------------------------------------------------------------------------


def test_binary_toggle_flag_either_live_or_in_registry():
    """ADR-B5 Rule 2: every ``os.environ.get("WC_*", "on"|...)`` site
    in ``scripts/`` must be either in compose env or in the registry.
    An unknown-toggle read with no compose entry is dark code — it's
    silently default-on/off with no externally-visible declaration."""
    from scripts.v2.flag_registry import CODE_DEFAULT_ON, EXPERIMENTAL

    reads = _collect_binary_toggle_reads()
    if not reads:
        pytest.skip("no binary-toggle reads found in scripts/ — nothing to lint")

    compose_keys: set[str] = set()
    for compose_path in COMPOSE_FILES:
        compose_keys |= _compose_env_keys(compose_path)
    known_registry = set(CODE_DEFAULT_ON) | set(EXPERIMENTAL)

    dark: dict[str, list[str]] = {}
    for flag, sites in reads.items():
        if flag in compose_keys or flag in known_registry:
            continue
        dark[flag] = sites

    assert not dark, (
        "ADR-B5 Rule 2: binary-toggle WC_* read in code but neither "
        "live in compose nor declared in scripts/v2/flag_registry.py. "
        "Either add to compose `environment:` OR add to "
        "`flag_registry.CODE_DEFAULT_ON` / `EXPERIMENTAL` with "
        "rationale. Dark flags:\n  "
        + "\n  ".join(f"{f}  @ {sites}" for f, sites in dark.items())
    )


def test_flag_registry_entries_match_actual_code_defaults():
    """NOT-X mirror of Rule 2: every entry in the registry must have
    at least one matching binary-toggle read in ``scripts/``. Orphans
    are dead entries — they make the registry lie about prod state.
    """
    from scripts.v2.flag_registry import CODE_DEFAULT_ON, EXPERIMENTAL

    reads = _collect_binary_toggle_reads()
    declared = set(CODE_DEFAULT_ON) | set(EXPERIMENTAL)

    orphans = [name for name in declared if name not in reads]
    assert not orphans, (
        "scripts/v2/flag_registry.py declares flags that no code reads "
        "as a binary toggle. Either add the read or remove the registry "
        f"entry. Orphan entries: {orphans}"
    )


def test_flag_registry_entries_have_rationale():
    """Each registry entry's value must be a non-empty rationale
    string. An empty rationale is a TODO masquerading as a decision.
    """
    from scripts.v2.flag_registry import CODE_DEFAULT_ON, EXPERIMENTAL

    bad: list[str] = []
    for name, rationale in {**CODE_DEFAULT_ON, **EXPERIMENTAL}.items():
        if not rationale or not rationale.strip():
            bad.append(name)
    assert not bad, (
        "scripts/v2/flag_registry.py entries with empty rationale "
        f"(ADR-B5 requires a one-line rationale per entry): {bad}"
    )
