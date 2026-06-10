# --- S6 webapp: frontend build stage (D18 — multi-stage Vite build) ---
# The React SPA (web/) is compiled here and only its dist/ output is
# copied into the runtime image below. No node in the runtime image, no
# node on the host. NB: web/package-lock.json should be generated on the
# first prod build and committed — until then npm resolves from the
# semver ranges in package.json (flagged in the S6 PR).
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi
COPY web/ ./
RUN npm run build

FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# ADR-B1 phase 3 + ADR-B2 (S-B1 acceptance): install deps from
# requirements.lock with hash verification, then COPY the prod code
# tree (scripts/, tests/) into the image so the running container does
# not depend on a host bind-mount of the live repo. See
# docs/v2/decisions.md → S-B1 / D-SB1-2.

# OS-level deps. --no-install-recommends keeps the apt layer slim;
# removing /var/lib/apt/lists strips the package metadata cache.
RUN apt update && apt install -y --no-install-recommends \
        git \
        wget \
        curl \
        unzip \
        build-essential \
        tmux \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

# Drop torchvision / torchaudio from the base layer: both are pinned to
# torch==2.6.0 and become binary-incompatible when the lock upgrades
# torch to 2.12.0. Grep'd clean across scripts/ and tests/ on
# 2026-05-24 — neither is imported anywhere in this project. Removing
# now keeps the next layer's install output clean (no
# dependency-conflict warning) and saves ~3 GB of dead weight.
RUN pip uninstall -y torchvision torchaudio

# Hashed install of all runtime deps. --require-hashes verifies every
# wheel against requirements.lock; a tampered or unexpected wheel =
# build failure with a clear message. --no-cache-dir keeps the layer
# from carrying the pip download cache.
#
# Side-effect vs the base image: torch 2.6.0+cu124 (preinstalled) is
# upgraded to torch 2.12.0+cu130 (PyPI mainline now bundles its own
# CUDA 13 runtime via nvidia-cublas-cu13 / nvidia-cudnn-cu13 / etc.
# wheel deps). Verified `cuda available: True` on prod 2026-05-24 via
# `docker compose run --rm gutenberg-lab pip install --require-hashes ...`.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# spaCy models pinned by direct URL — version 3.8.0 matches the lock's
# spacy==3.8.14 (the 3.8.x line shares the 3.8.0 model release per
# spaCy's model versioning policy). Model wheels are published on
# GitHub Releases, not PyPI, so they sit outside the hashed lock set.
# The version embedded in the URL IS the pin — a tampered model would
# require a different URL. A follow-up ADR can fold the GitHub-Releases
# sha256 sidecar values into a hashed extension of the lock.
RUN pip install --no-cache-dir \
        https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl \
        https://github.com/explosion/spacy-models/releases/download/en_core_web_trf-3.8.0/en_core_web_trf-3.8.0-py3-none-any.whl

# S-R5 coldstart P11 (2026-05-31): bake the BGE cross-encoder reranker
# (BAAI/bge-reranker-base, ~440 MB) into the image. find_book_by_topic's
# planner path (book_similar / «что почитать после X») reranks with this
# model; it lazy-loads via CrossEncoder on first compute()
# (scripts/v2/scoring/__init__.py). The HF cache otherwise lives in the
# container's WRITABLE layer — docker-compose.yml app-volumes mounts only
# corpus/state, no HF cache volume — so a deploy with `--force-recreate`
# WIPES it and the 440 MB model RE-DOWNLOADS on the first rerank. That is
# the P11 cold cost: «что почитать после …» ran 128s cold (probe rule
# latency_under_s=60 → FAIL) vs 7.7s warm. Baking moves the download to
# build time (deterministic, captured in the layer, survives
# --force-recreate). If the download fails the BUILD fails — so a shipped
# image always carries the model, and the runtime warmup (chat_server
# _warmup) is then a fast disk-load, never a download. Build/runtime both
# run as root → cache at /root/.cache/huggingface. Placed before the code
# COPY so a SHA bump doesn't invalidate this heavy layer.
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-base')"

WORKDIR /workspace

# D-SB1-2: bake the production code tree into the image. Dev (bind-mount
# via docker-compose.dev.yml, dev opt-in only) overlays
# /workspace/scripts and /workspace/tests; prod (-f docker-compose.yml
# only) reads them straight from the layers below. .dockerignore keeps
# the build context small — corpus dirs (data/, raw_books/), .git,
# notebooks/, docs/ never enter the image.
COPY scripts/ /workspace/scripts/
COPY tests/ /workspace/tests/

# S6 webapp: FastAPI service code + the Vite-built SPA (webbuild stage
# above). Served by the `api` compose service (uvicorn :8000); FastAPI
# StaticFiles mounts /workspace/web/dist on "/" AFTER the /api routes
# (no SPA catch-all in S6 — single screen). See docs/webapp.md.
COPY api/ /workspace/api/
COPY --from=webbuild /web/dist /workspace/web/dist

# ADR-B3 / D-SB3-1 (S-B3): bake runtime build identity into the image.
# GIT_SHA + BUILD_TIME are surfaced via `/health` JSON (chat:8890 /
# admin:8891) and the chat UI header chip. Placed late so a fresh SHA
# only invalidates this layer + EXPOSE/CMD, not the heavy pip-install
# layers above. `deploy.sh` passes both via --build-arg; an operator
# who runs `docker build` by hand without them gets the "unknown"
# default — visible at /health and in the UI as a deploy-discipline
# signal. See docs/v2/decisions.md → 2026-05-25 S-B3.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=$GIT_SHA \
    BUILD_TIME=$BUILD_TIME

EXPOSE 8888

# jupyter on 8888 stays the default CMD. chat_server / admin_server
# are launched into this container via `docker compose exec` from
# systemd — ADR-B3 will split them into their own compose services
# (proper PID 1 / graceful SIGTERM).
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--ServerApp.token=''", "--ServerApp.password=''"]
