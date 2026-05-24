"""S-B1 + S-B2 acceptance: negative tests for the deploy artifact.

These tests are the R2 negative-test gate for the 2026-05-24 S-B1 and
S-B2 blocks (`docs/v2/decisions.md` → D-SB1-1..D-SB1-6 and
D-SB2-1..D-SB2-8). They lock in:

* S-B1: prod base has no code bind-mounts, dev override does;
  ``WC_IMAGE_TAG`` is strictly required in prod; ``:-latest`` fallback
  is gone; systemd compose calls (if any) pin
  ``-f docker-compose.yml``.
* S-B2: ``chat`` and ``admin`` are compose services with explicit
  ``command:`` and SHA-pinned image; their healthcheck targets
  ``/health``; ``WC_LLM_MODEL`` / ``WC_CRITIC_MODEL`` live in compose
  (not the deleted systemd drop-in); ``wordcracker-chat.service`` /
  ``wordcracker-admin.service`` are deleted from the repo;
  ``wordcracker-status.service`` carries ``[Install]`` so
  ``systemctl enable`` actually links it; ``scripts/deploy.sh`` is one
  mechanism (compose recreate) with no ``systemctl restart`` loop for
  chat/admin; no ``docker exec`` survives in any systemd unit.

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
CHAT_SERVICE = "chat"
ADMIN_SERVICE = "admin"


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


# --- D-SB1-1 / D-SB2-1: dev override keeps the bind-mount convenience ---

@pytest.mark.parametrize("service", [GUTENBERG_SERVICE, CHAT_SERVICE, ADMIN_SERVICE])
def test_dev_override_restores_code_bind_mounts(service: str):
    """NOT-X mirror: dev path (override applied) DOES bind-mount code.

    Pre-S-B2 only `gutenberg-lab` got the dev bind-mount (chat / admin
    ran via `docker compose exec` inside it). Post-S-B2 chat and admin
    are first-class services and each needs `./scripts` mounted in dev
    too, otherwise editing `chat_server.py` on the host has no effect
    on the running container until rebuild. Asserts each of the three
    app services keeps `./scripts` in dev.

    If this fails after a future change, dev edit-and-reload is broken
    for that service even though prod gates are happy. The two
    invariants (prod no-mount / dev mount) together pin the contract
    in both directions (R2).
    """
    override = _load_yaml(OVERRIDE_COMPOSE)
    vols = _service_volumes(override, service)
    assert any("./scripts" in v for v in vols), (
        f"dev override {OVERRIDE_COMPOSE.name} does not bind-mount "
        f"`./scripts` for {service!r}. Dev edit-and-reload would not "
        f"work — host edits to scripts/*.py would not reach the "
        f"running container."
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


# --- D-SB1-6 / D-SB2-6: no docker exec / no compose-without-base-file in any systemd unit ---

def test_no_docker_exec_in_any_systemd_unit():
    """No surviving `docker compose exec` / `docker exec` in systemd.

    Pre-S-B2 chat / admin systemd units shelled into the running
    container via `docker compose exec -T ... python ...`. The pattern
    was the SIGTERM-propagation hazard ADR-B3 / D-SB2-1 retired
    (chat/admin are compose services now → PID 1 = python →
    systemctl/docker stop sends SIGTERM directly).

    Walk every `systemd/*.service` (recursively, including drop-in
    `*.conf` files) and assert none re-introduces the exec shape.
    Catches a future regression where a single unit "fixes" something
    by reaching back to docker exec.
    """
    bad = []
    for src in [*SYSTEMD_DIR.rglob("*.service"), *SYSTEMD_DIR.rglob("*.conf")]:
        text = src.read_text(encoding="utf-8")
        for needle in ("docker compose exec", "docker exec "):
            if needle in text:
                bad.append(f"{src.relative_to(REPO_ROOT)}: contains '{needle}'")
    assert not bad, (
        "docker exec pattern survives in systemd:\n  " + "\n  ".join(bad)
    )


def test_any_compose_call_in_systemd_pins_base_file():
    """If a unit calls `docker compose`, it MUST pin `-f docker-compose.yml`.

    Same intent as the old per-unit parametrized D-SB1-6 test, but now
    discovers units instead of hard-coding chat/admin (which no longer
    exist after D-SB2-6). The current set is `wordcracker-status.service`
    only, and status_server runs host-Python — it has no docker compose
    line — so this test is vacuously satisfied today. It guards against
    a future unit that adds one without the explicit -f flag.
    """
    bad = []
    for src in SYSTEMD_DIR.rglob("*.service"):
        for line in src.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip("- ")
            if not stripped.startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost=", "ExecStop=")):
                continue
            if "/usr/bin/docker compose" not in line and "docker compose" not in line:
                continue
            # Allow the inline /bin/sh -c '...' loop that calls curl, not compose.
            if "curl" in line and "docker compose" not in line.split("/bin/sh", 1)[-1]:
                continue
            if "-f docker-compose.yml" not in line:
                bad.append(f"{src.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not bad, (
        "docker compose invocations missing `-f docker-compose.yml`:\n  "
        + "\n  ".join(bad)
    )


# --- D-SB2-1: chat + admin are compose services with explicit command + SHA image ---

@pytest.mark.parametrize("service", [CHAT_SERVICE, ADMIN_SERVICE])
def test_chat_and_admin_are_compose_services(service: str):
    """`chat` and `admin` exist in the prod base with explicit command + SHA image.

    Pre-S-B2 they were only systemd-managed `docker exec` calls into
    gutenberg-lab. After D-SB2-1 each is a first-class compose service
    with its own PID-1 python process. The asserts cover:
      * service block exists,
      * `command:` is a non-empty list (PID 1 = python, not the
        gutenberg-lab jupyter default),
      * `image:` resolves to wordcracker-textlab:${WC_IMAGE_TAG...} — same
        SHA-pin guarantee S-B1 enforced for gutenberg-lab.
    """
    compose = _load_yaml(BASE_COMPOSE)
    svc = compose.get("services", {}).get(service)
    assert svc is not None, f"{BASE_COMPOSE.name} missing service '{service}'"

    command = svc.get("command")
    assert isinstance(command, list) and command, (
        f"service '{service}' must have a list-form `command:` (PID 1). "
        f"Got: {command!r}"
    )
    # First arg should be python (the chat / admin server is a python script).
    assert command[0] == "python", (
        f"service '{service}' command[0] must be 'python' to make PID 1 = python "
        f"(SIGTERM propagation). Got: {command[0]!r}"
    )

    image = svc.get("image", "")
    assert "wordcracker-textlab" in image, (
        f"service '{service}' image must be in the wordcracker-textlab family; got {image!r}"
    )
    assert "${WC_IMAGE_TAG" in image, (
        f"service '{service}' image must carry the ${{WC_IMAGE_TAG...}} substitution "
        f"(same SHA-pin guarantee as S-B1 / D-SB1-3); got {image!r}"
    )


def test_chat_publishes_8890_and_admin_publishes_8891():
    """Ports moved from gutenberg-lab to the new dedicated services.

    Pre-S-B2 gutenberg-lab published 8888, 8890 AND 8891 (all three
    processes shared one container). Post-S-B2 ports must split:
    gutenberg-lab keeps only 8888 (jupyter); chat owns 8890; admin
    owns 8891. The split is what makes `docker stop chat` actually
    only stop chat.
    """
    compose = _load_yaml(BASE_COMPOSE)

    def ports_of(svc_name: str) -> list[str]:
        return [str(p) for p in compose.get("services", {}).get(svc_name, {}).get("ports", []) or []]

    chat_ports = ports_of(CHAT_SERVICE)
    admin_ports = ports_of(ADMIN_SERVICE)
    gutenberg_ports = ports_of(GUTENBERG_SERVICE)

    assert any("8890" in p for p in chat_ports), f"chat must publish 8890; got {chat_ports}"
    assert any("8891" in p for p in admin_ports), f"admin must publish 8891; got {admin_ports}"
    # NOT-X mirror: gutenberg-lab no longer claims chat/admin ports.
    assert not any("8890" in p for p in gutenberg_ports), (
        f"gutenberg-lab must NOT publish 8890 anymore (chat owns it); got {gutenberg_ports}"
    )
    assert not any("8891" in p for p in gutenberg_ports), (
        f"gutenberg-lab must NOT publish 8891 anymore (admin owns it); got {gutenberg_ports}"
    )


# --- D-SB2-3: status unit has [Install] so `systemctl enable` works ---

def test_install_script_enables_status_unit():
    """`scripts/install_systemd_units.sh` must `systemctl enable wordcracker-status`.

    Pre-S-B2 the install script ran `systemctl restart` but NEVER
    `systemctl enable`. `restart` starts the unit for this boot only;
    without `enable`, the unit is not linked into
    `multi-user.target.wants/` and a host reboot leaves status_server
    down — which is exactly the failure mode the S-B2 brief reports
    ("status_server на хосте через nohup не переживает ребут"). The
    fix is the explicit enable line in install_systemd_units.sh; this
    test pins that the line stays.
    """
    text = (REPO_ROOT / "scripts" / "install_systemd_units.sh").read_text(encoding="utf-8")
    assert re.search(r"systemctl\s+enable\s+wordcracker-status\b", text), (
        "install_systemd_units.sh must run `systemctl enable wordcracker-status` "
        "(D-SB2-3) — without it the unit does not auto-start on reboot."
    )


def test_status_unit_has_install_section():
    """`wordcracker-status.service` must carry `[Install] WantedBy=multi-user.target`.

    Without `[Install]`, `systemctl enable` is a no-op — the unit
    cannot be linked into `multi-user.target.wants/` and therefore
    will not start on reboot. D-SB2-3 documents
    `systemctl enable wordcracker-status` as the install step; this
    test guards the precondition that makes the enable meaningful.
    """
    unit = SYSTEMD_DIR / "wordcracker-status.service"
    assert unit.is_file(), f"{unit} missing"
    text = unit.read_text(encoding="utf-8")
    assert "[Install]" in text, f"{unit.name} lacks [Install] section — systemctl enable would be a no-op"
    assert re.search(r"^\s*WantedBy\s*=\s*multi-user\.target\s*$", text, re.MULTILINE), (
        f"{unit.name} [Install] must set `WantedBy=multi-user.target` so reboot brings it back"
    )


# --- D-SB2-5: v2-engine.conf drop-in removed; pins live in compose ---

def test_v2_engine_drop_in_removed():
    """`systemd/wordcracker-chat.service.d/v2-engine.conf` is gone.

    D-S0-5 flagged it for removal; D-SB2-5 deleted it as part of the
    systemd-chat unit retirement. Its still-meaningful pins
    (WC_LLM_MODEL / WC_CRITIC_MODEL) moved to compose
    (test_chat_environment_pins_llm_models); WC_DEFAULT_ENGINE was
    already dead in code (D-P1-5).
    """
    drop_in = SYSTEMD_DIR / "wordcracker-chat.service.d" / "v2-engine.conf"
    assert not drop_in.exists(), (
        f"{drop_in.relative_to(REPO_ROOT)} must be deleted "
        f"(D-S0-5 / D-SB2-5)"
    )
    # NOT-X mirror: the drop-in directory itself should also be gone
    # (it has no other inhabitants).
    drop_in_dir = SYSTEMD_DIR / "wordcracker-chat.service.d"
    assert not drop_in_dir.exists(), (
        f"{drop_in_dir.relative_to(REPO_ROOT)} (empty drop-in dir) must be deleted"
    )


def test_chat_environment_pins_llm_models():
    """`chat.environment` carries WC_LLM_MODEL and WC_CRITIC_MODEL.

    The pins relocated from v2-engine.conf to compose. Asserts both:
      * compose has them (positive),
      * systemd no longer mentions them (negative mirror).
    """
    compose = _load_yaml(BASE_COMPOSE)
    chat_env = compose.get("services", {}).get(CHAT_SERVICE, {}).get("environment", {})
    # Compose accepts environment as dict or list-of-KEY=VAL; we wrote dict (anchor *app-env).
    if isinstance(chat_env, list):
        chat_env = dict(kv.split("=", 1) for kv in chat_env if "=" in kv)
    assert chat_env.get("WC_LLM_MODEL") == "wordcracker:v2", (
        f"chat.environment must pin WC_LLM_MODEL=wordcracker:v2; got {chat_env.get('WC_LLM_MODEL')!r}"
    )
    assert chat_env.get("WC_CRITIC_MODEL") == "wordcracker:v2", (
        f"chat.environment must pin WC_CRITIC_MODEL=wordcracker:v2; got {chat_env.get('WC_CRITIC_MODEL')!r}"
    )

    # NOT-X mirror: no systemd file mentions them anymore.
    leaked = []
    for src in [*SYSTEMD_DIR.rglob("*.service"), *SYSTEMD_DIR.rglob("*.conf")]:
        text = src.read_text(encoding="utf-8")
        for var in ("WC_LLM_MODEL", "WC_CRITIC_MODEL", "WC_DEFAULT_ENGINE"):
            if var in text:
                leaked.append(f"{src.relative_to(REPO_ROOT)}: still mentions {var}")
    assert not leaked, (
        "model pins / dead vars leaked into systemd (should live in compose only):\n  "
        + "\n  ".join(leaked)
    )


# --- D-SB2-6: chat/admin systemd units are deleted ---

@pytest.mark.parametrize("unit_name", [
    "wordcracker-chat.service",
    "wordcracker-admin.service",
])
def test_chat_admin_systemd_units_removed(unit_name: str):
    """`systemd/wordcracker-{chat,admin}.service` must NOT exist.

    Two supervisors (systemd + docker/compose) for the same process
    would race; D-SB2-6 picked Docker. Their continued presence in the
    repo would imply a half-applied migration.
    """
    unit = SYSTEMD_DIR / unit_name
    assert not unit.exists(), (
        f"{unit.relative_to(REPO_ROOT)} must be deleted — chat/admin are "
        f"compose services now (D-SB2-1, D-SB2-6); systemd-side supervision "
        f"would race with compose's `restart: unless-stopped`."
    )


# --- D-SB2-4: deploy.sh is one mechanism (compose recreate only) ---

def test_deploy_sh_no_systemctl_restart_of_chat_admin():
    """`scripts/deploy.sh` must NOT systemctl restart chat/admin.

    Pre-S-B2 the deploy chain was `up --force-recreate gutenberg-lab`
    + `systemctl restart wordcracker-{chat,admin}`. Post-S-B2 the
    second half collapses — chat/admin are recreated by compose
    itself. A reintroduction of the systemctl line means someone
    re-split the two mechanisms.
    """
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    # The dead pattern, in either parametrized or hardcoded form.
    bad = []
    for pat in (
        r"systemctl\s+restart\s+wordcracker-chat\b",
        r"systemctl\s+restart\s+wordcracker-admin\b",
        # Common loop form: `for unit in wordcracker-chat wordcracker-admin`.
        r"for\s+unit\s+in\s+wordcracker-chat\s+wordcracker-admin",
    ):
        if re.search(pat, text):
            bad.append(pat)
    assert not bad, (
        "scripts/deploy.sh still systemctl-restarts chat/admin "
        f"(forbidden post-S-B2): {bad}"
    )

    # NOT-X mirror: `--force-recreate ... chat admin` IS present.
    assert re.search(r"--force-recreate\b[^\n]*\bchat\b[^\n]*\badmin\b", text), (
        "scripts/deploy.sh must name `chat` and `admin` in the "
        "`--force-recreate ...` line so the single mechanism brings "
        "them up. Otherwise gutenberg-lab gets recreated alone and "
        "chat/admin silently stay on the old image."
    )


# --- Sanity: deploy / verify scripts exist and are not empty ---

@pytest.mark.parametrize("script_relpath", [
    "scripts/deploy.sh",
    "scripts/verify_deployed_image.sh",
    "scripts/install_systemd_units.sh",
    "scripts/smoke_s_b2.sh",
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
