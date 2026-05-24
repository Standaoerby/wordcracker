#!/usr/bin/env bash
# Generate requirements.lock from requirements.in (ADR-B1).
#
# Runs pip-compile inside a one-shot gutenberg-lab container so the
# resolver sees the actual prod runtime (Linux x86_64 + CUDA 12.4 +
# the image's Python). Generating from a different host (Windows
# dev box, ARM mac) would resolve different torch / chromadb wheels
# — wrong artifact.
#
# Usage (on the prod host):
#
#     bash build_lockfile.sh
#
# After completion: review `git diff requirements.lock`, commit
# alongside any matching `requirements.in` change. Per R7 — one
# commit per structural change; lock regeneration is its own commit.
#
# This script does NOT touch the running gutenberg-lab service.
# `docker compose run --rm` spins up a fresh one-shot container
# that exits when pip-compile finishes.

set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f requirements.in ]]; then
    echo "ERROR: requirements.in missing — nothing to compile" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found on PATH" >&2
    exit 1
fi

echo "Generating requirements.lock against the gutenberg-lab image..."
echo "(this takes 2-5 minutes; pip-compile resolves the transitive graph)"

docker compose run --rm \
    -v "$(pwd):/lockwork" \
    -w /lockwork \
    --entrypoint sh \
    gutenberg-lab \
    -c 'pip install --quiet pip-tools && \
        pip-compile \
            --output-file=requirements.lock \
            --resolver=backtracking \
            --generate-hashes \
            --strip-extras \
            --allow-unsafe \
            --quiet \
            requirements.in && \
        echo "lock generated"'

echo ""
echo "Done. Next steps:"
echo "  1. git diff requirements.lock      (review the resolved set)"
echo "  2. git add requirements.in requirements.lock"
echo "  3. git commit -m 'build: regenerate requirements.lock'"
echo ""
echo "Then proceed to ADR-B1 phase 2 (rewrite Dockerfile to install"
echo "from lock with --require-hashes)."
