"""S-B1 acceptance: negative tests for the deploy artifact.

These tests are the R2 negative-test gate for the 2026-05-24 S-B1 block
(`docs/v2/decisions.md` → D-SB1-1..D-SB1-6). They lock in the shape of
the compose files and systemd units so a future regression that
re-introduces a code bind-mount in the prod base, restores the
``:-latest`` fallback, or drops the ``-f docker-compose.yml`` flag from
systemd fails the suite immediately.

The tests do NOT exec docker — they parse the YAML / unit files
directly so they work on dev boxes (Windows, mac, Linux) and in CI
without a docker daemon.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERRIDE_COMPOSE = REPO_ROOT / "docker-compose.override.yml"
SYSTEMD_DIR = REPO_ROOT / "systemd"

GUTENBERG_SERVICE = "gutenberg-lab"


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _service_volumes(compose: dict, service: str) -> list[str]:
    return list(compose.get("services", {}).get(service, {}).get("volumes", []) or [])


# --- D-SB1-1 / D-SB1-2: prod base has no code bind-mounts ---

def test_no_code_bind_mounts_in_prod_base():
    """Prod base must NOT mount any path from the host repo (./...).

    Code is baked into the image via Dockerfile COPY (D-SB1-2). Data
    bind-mounts that point at absolute /data/... paths on the host are
    explicitly allowed by ADR-B2's trade-off section — corpus / chroma
    / spgc are too large to bake.
    """
    compose = _load_yaml(BASE_COMPOSE)
    vols = _service_volumes(compose, GUTENBERG_SERVICE)
    repo_relative = [v for v in vols if v.startswith("./") or v.startswith(".\\")]
    assert repo_relative == [], (
        f"prod base {BASE_COMPOSE.name} bind-mounts host repo paths "
        f"({repo_relative}). Move them to docker-compose.override.yml (dev-only)."
    )


def test_prod_base_keeps_data_bind_mounts():
    """Mirror of the above: corpus/state bind-mounts at /data/* MUST stay.

    Without them prod has no books, no chroma, no spgc. This guards
    against an over-eager `volumes: []` blow-away.
    """
    compose = _load_yaml(BASE_COMPOSE)
    vols = _service_volumes(compose, GUTENBERG_SERVICE)
    data_mounts = [v for v in vols if v.startswith("/data/")]
    assert data_mounts, (
        f"prod base {BASE_COMPOSE.name} has zero /data/* bind-mounts — "
        f"corpus/state would not be visible to the container."
    )


# --- D-SB1-1: dev override keeps the bind-mount convenience ---

def test_dev_override_restores_code_bind_mounts():
    """NOT-X mirror: dev path (override applied) DOES bind-mount code.

    If this fails after a future change, dev edit-and-reload is broken
    even though the prod gate above is happy. The two together pin the
    invariant in both directions (R2).
    """
    override = _load_yaml(OVERRIDE_COMPOSE)
    vols = _service_volumes(override, GUTENBERG_SERVICE)
    expected_substrings = ("./scripts", "./tests")
    for substring in expected_substrings:
        assert any(substring in v for v in vols), (
            f"dev override {OVERRIDE_COMPOSE.name} does not bind-mount "
            f"{substring!r} for {GUTENBERG_SERVICE}. Dev edit-and-reload "
            f"would not work."
        )


# --- D-SB1-3: WC_IMAGE_TAG is required; no `:-latest` fallback ---

def test_prod_image_tag_is_strictly_required():
    """`image:` in the prod base must use ${VAR:?msg} (fail-loud) form.

    A `:-latest` or `:-dev` fallback in the prod file would let an
    operator who forgot to set WC_IMAGE_TAG ship whatever the floating
    tag happens to point at — exactly the failure that wasted runs 2-5
    of the 2026-05-22 deploy epic.
    """
    text = BASE_COMPOSE.read_text(encoding="utf-8")
    # The strict-required form looks like ${WC_IMAGE_TAG:?...}.
    assert re.search(r"\$\{WC_IMAGE_TAG:\?", text), (
        f"{BASE_COMPOSE.name} must use ${{WC_IMAGE_TAG:?msg}} form "
        f"(strict-required substitution) on the image: line."
    )
    # And must NOT carry a default-value fallback (:-something).
    assert not re.search(r"\$\{WC_IMAGE_TAG:-", text), (
        f"{BASE_COMPOSE.name} contains a ${{WC_IMAGE_TAG:-...}} fallback. "
        f"Prod must fail loud when the tag is unset; move the fallback "
        f"to docker-compose.override.yml (dev)."
    )


def test_dev_override_provides_image_tag_fallback():
    """NOT-X mirror: dev override DOES provide a `:-dev` fallback.

    Bare `docker compose up` on a fresh dev box (no .env) must keep
    working. If this fails, dev onboarding broke even though prod is
    happy.
    """
    text = OVERRIDE_COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"\$\{WC_IMAGE_TAG:-", text), (
        f"{OVERRIDE_COMPOSE.name} should provide a ${{WC_IMAGE_TAG:-...}} "
        f"fallback so bare `docker compose up` works without an .env."
    )


# --- D-SB1-6: systemd uses -f docker-compose.yml ---

@pytest.mark.parametrize("unit_name", [
    "wordcracker-chat.service",
    "wordcracker-admin.service",
])
def test_systemd_compose_lines_carry_explicit_base_file(unit_name: str):
    """Every `docker compose` invocation in the unit must pin -f docker-compose.yml.

    Without -f, docker compose auto-applies docker-compose.override.yml
    on prod, which re-introduces code bind-mounts (D-SB1-6). The check
    is text-based: each Exec* line that calls `/usr/bin/docker compose`
    must contain the literal `-f docker-compose.yml` before any
    subcommand.
    """
    unit_path = SYSTEMD_DIR / unit_name
    text = unit_path.read_text(encoding="utf-8")
    bad = []
    for line in text.splitlines():
        stripped = line.lstrip("- ")  # strip leading '-' for ExecStartPre=-...
        if not stripped.startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost=", "ExecStop=")):
            continue
        if "/usr/bin/docker compose" not in line:
            continue
        # Allow the inline /bin/sh -c '...' loop in ExecStartPost (calls curl, not compose).
        if "curl" in line and "docker compose" not in line.split("/bin/sh", 1)[-1]:
            continue
        if "-f docker-compose.yml" not in line:
            bad.append(line.strip())
    assert not bad, (
        f"{unit_name}: docker compose invocations missing `-f docker-compose.yml`:\n  "
        + "\n  ".join(bad)
    )


# --- Sanity: deploy / verify scripts exist and are not empty ---

@pytest.mark.parametrize("script_relpath", [
    "scripts/deploy.sh",
    "scripts/verify_deployed_image.sh",
    "scripts/install_systemd_units.sh",
])
def test_deploy_scripts_exist(script_relpath: str):
    """Deploy / verify / install scripts exist and have non-trivial size.

    A meaningful regression would either delete them or replace them
    with stubs. Both fail this test.
    """
    p = REPO_ROOT / script_relpath
    assert p.is_file(), f"{script_relpath} missing"
    assert p.stat().st_size > 500, (
        f"{script_relpath} is suspiciously small ({p.stat().st_size} bytes); "
        f"someone may have stubbed it out."
    )
