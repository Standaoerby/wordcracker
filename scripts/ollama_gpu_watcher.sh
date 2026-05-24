#!/bin/bash
# ollama_gpu_watcher.sh — recreate ollama if it lost GPU passthrough.
#
# Docker network recycling (compose down/up of another service in the same
# project) sometimes detaches nvidia-container-runtime from a running
# container. Symptom: `nvidia-smi` inside ollama fails to init NVML and
# the next /api/generate loads the model on CPU (~1 tok/s instead of ~80).
#
# This watcher is cron-friendly. Idempotent. Logs to /var/log/ollama_watcher.log
# (or /tmp/ if /var/log is not writable).
#
# Install:
#   sudo cp ollama_gpu_watcher.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/ollama_gpu_watcher.sh
#   (crontab -l 2>/dev/null; echo '* * * * * /usr/local/bin/ollama_gpu_watcher.sh') | crontab -

set -u
LOG=${WATCHER_LOG:-/tmp/ollama_watcher.log}
COMPOSE_DIR=${COMPOSE_DIR:-/home/claude/wordcracker}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# 1) container up?
if ! docker ps --format '{{.Names}}' | grep -q '^ollama$'; then
  log "ollama container not running, skipping"
  exit 0
fi

# 2) does nvidia-smi work inside the container?
if docker exec ollama nvidia-smi -L >/dev/null 2>&1; then
  exit 0  # all good
fi

log "GPU lost in ollama (nvidia-smi failed). Recreating container..."
cd "$COMPOSE_DIR" || { log "cd $COMPOSE_DIR failed"; exit 1; }

# D-SB1-7: dev override (docker-compose.dev.yml) is no longer
# auto-applied and the watcher runs on prod via cron. Only the base
# file is needed — the dev override touches gutenberg-lab / chat /
# admin, never ollama.
docker compose -f docker-compose.yml down ollama >>"$LOG" 2>&1
docker compose -f docker-compose.yml up -d ollama >>"$LOG" 2>&1

sleep 8

if docker exec ollama nvidia-smi -L >/dev/null 2>&1; then
  log "GPU restored after recreate"
  # warm the pinned model back into VRAM (keep_alive=-1 expected from rag_query.py)
  curl -sS -m 90 http://localhost:11434/api/generate \
    -d '{"model":"qwen3:14b","prompt":"ok","stream":false,"keep_alive":-1,"think":false}' \
    >/dev/null 2>>"$LOG" && log "model re-warmed" || log "warm-up failed"
else
  log "GPU still missing after recreate — manual check needed"
fi
