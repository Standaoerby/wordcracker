# wordcracker v2.10 — thermals dashboard + admin failed-log freeze fix

Stan's prod observations 2026-05-19:

1. SOW with 69h+ uptime + continuous Ollama+ChromaDB load → wants
   visibility into thermal margins before silicon cooks itself
2. `/admin/failed` page freezes after a while

## Thermals card in status dashboard

New «Thermals» card on `status.slovoeb.net` shows every available
temperature sensor:

- **Kernel `thermal_zone*`** (always present on Linux) — CPU package,
  integrated GPU, SOC zones via `/sys/class/thermal/thermal_zone*/temp`
- **NVIDIA GPU** — `nvidia-smi --query-gpu=temperature.gpu,
  temperature.memory` for the RTX 3090
- **lm-sensors JSON** if installed (`sensors -j`) — drives, motherboard,
  k10temp / coretemp / nvme

Top row: «Peak temperature» — max across all sources, **color-coded**:
🟢 < 70 °C  ·  🟠 70-85 °C  ·  🔴 > 85 °C — instant glance for «is
anything overheating».

All sensor reads are best-effort: missing tool / unreadable file =
empty list, page never crashes. If nothing detected (e.g. tools not
installed yet), card shows a single «(no sensors detected) — install
lm-sensors / mount /sys» row so Stan knows what to fix.

To enable extra sensors on the host:

```bash
sudo apt install lm-sensors
sudo sensors-detect --auto
# /sys/class/thermal is always available, no install needed
```

## Admin /failed page freeze — root cause + fix

`setInterval(load, 15000)` fired every 15 s regardless of whether the
previous `fetch('/api/failed')` had returned. When the API got slow
(big ring buffer + side-by-side `top_failed_phrases` aggregation),
overlapping fetches piled up, browser thrashed, page froze.

v2.10 rewrites the polling loop:

1. **Inflight guard** — `isLoading` flag refuses to start a new fetch
   while the previous one is in progress.
2. **`AbortController`** — if a new tick fires while old fetch still
   pending (rare with #1, defensive belt), the old request is cancelled.
3. **Chain-of-setTimeout** — next poll scheduled in `.finally`, only
   after the current one fully resolves. No more queue buildup.
4. **`visibilityState` pause** — when tab is hidden, polling stops.
   Resumes on focus + immediate reload. Stops background-tab pressure
   on the server when Stan has the admin tab open for hours.
5. Server payload capped at 100 fails + 30 top phrases (already was, but
   now hard-clipped on the client too as belt-and-braces).

## Tests

- Unit: **274/274** (no behavioral changes to v2 dispatch path)
- AST: 79/79 files valid
- Thermal collector smoke-tested locally — picked up RTX (GPU/VRAM)

## Deploy

```bash
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-status   # for thermals card
sudo systemctl restart wordcracker-admin    # for /failed freeze fix
```

Optional, to enable extra CPU/drive temps:
```bash
sudo apt install lm-sensors -y
echo y | sudo sensors-detect --auto
sudo systemctl restart wordcracker-status
```

Co-developed with Claude Opus 4.7 (1M context).
