FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# ADR-B1 phase 2: install from requirements.lock with hash verification.
# Phase 1 added requirements.in + lockfile generator + compose tag
# template; phase 3 will wire the deploy hook and drop the :-latest
# fallback. See docs/v2/decisions.md → ADR-B1.

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

# Bootstrap pip / setuptools / wheel BEFORE the hashed install.
# pip-compile leaves setuptools unpinned (it's a build-time dep — see
# the `# WARNING` block at the bottom of requirements.lock). Installing
# it explicitly here lets `pip install --require-hashes` proceed without
# requiring --allow-unsafe regeneration of the lock.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

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

WORKDIR /workspace
EXPOSE 8888

# jupyter on 8888 stays the default CMD. chat_server / admin_server
# are launched into this container via `docker compose exec` from
# systemd — ADR-B3 will split them into their own compose services
# (proper PID 1 / graceful SIGTERM).
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--ServerApp.token=''", "--ServerApp.password=''"]
