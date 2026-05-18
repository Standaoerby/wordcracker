# wordcracker v2.3.2 — pre-demon final harden

Pre-flight check before the red-team round. Two new vectors closed,
both architectural rather than behavioural.

## v1 fallback bypass

`_pick_engine()` used to honor client-side hints (`?engine=v1`,
`X-WC-Engine` header, `payload['engine']`). Anyone who knew the chat
URL could append `?engine=v1` and route around:

- v2 input caps (64 KB payload / 4 KB question / 50 turn history)
- 10-pattern prompt-injection guard
- structured copyright OOS
- critic fact-check pass

Old v1 path goes straight to the agentic LLM loop without any of those.

**Fix:** engine is now locked to `WC_DEFAULT_ENGINE` (= v2 in prod).
Client hints are ignored. Set `WC_ALLOW_ENGINE_OVERRIDE=1` to bring
back the legacy behavior — useful for A/B testing or the runner's
explicit `--engine v1` flag, but off by default.

Verified strict mode rejects 3 attack vectors:

```
?engine=v1                  → v2
X-WC-Engine: v1 header      → v2
{"engine":"v1"} in payload  → v2
```

## /api/stats reconnaissance hardening

The stats endpoint used to return the full `aggregate_recent()` payload,
which includes:

- `intents` histogram (which kinds of queries this server gets)
- `slow_tools` list with avg/p95 timings (which tools to hammer for a DoS)

For Stan's single-user setup behind Basic Auth that's not an immediate
problem, but it's reconnaissance fodder if an attacker ever slips past
nginx. The endpoint now emits only the 6 counters the footer actually
displays — `total`, `avg_elapsed_ms`, `cache_hit_rate`, `cache_hits`,
`cache_calls`, `critic_flagged`.

The status dashboard at `:8889` still has access to the full payload
via the in-process ring buffer (`recent_records()` / `aggregate_recent()`).

## Tests

- Unit: **206/206** (no behavioral changes)
- `_pick_engine` lock verified across STRICT and OVERRIDE modes
- `adversarial_probes.py --help` round-trips cleanly

## Ready for the demon

| Layer | Status |
|---|---|
| Input caps | ✓ enforced before parse |
| Control-char strip | ✓ |
| Prompt-injection guards (10 patterns) | ✓ planner-level, LLM never sees |
| Generation refusal (6 patterns) | ✓ |
| Engine lock | ✓ no client downgrade |
| Stats hardening | ✓ minimal payload |
| Critic fact-check | ✓ MAX_FLAGS=2 + per-intent skip |
| Modelfile baked SYSTEM | ✓ last line of defense |
| Adversarial probe suite | ✓ 28 probes ready to run |

What's still NOT covered (out of v2.3.2 scope):

- Rate limiting per IP (nginx layer)
- Slow-stream DoS (`ThreadingHTTPServer` doesn't cap concurrent threads)

Co-developed with Claude Opus 4.7 (1M context).
