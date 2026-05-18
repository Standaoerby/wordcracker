# wordcracker v2.2.2 — deploy hardening + functional 40/40 validated

Patch on top of v2.2.1. Adds two race-condition guards Stan hit during the
v2.2.1 deploy retest, then validates the whole stack against the
40-question functional suite end-to-end.

## What changed

ChromaDB + embedder warmup on the SOW box takes ~12 s after a chat-server
cold start. The deploy ritual `systemctl restart && sleep 5 && curl
/health && run_functional_40.py` fired the runner before the server bound
port 8890. All 40 requests came back as `ConnectionResetError [Errno 104]`
in 0.0 s and the report was 100 % junk.

Two complementary fixes:

1. **`systemd/wordcracker-chat.service` — `ExecStartPost` block.**
   `systemctl start` now blocks until `/health` returns 200 (poll every
   2 s, 30 tries = 60 s cap, comfortably above the observed 14 s p95
   warmup). Anything chained after `systemctl restart` is guaranteed a
   ready server.

2. **`tests/v2/run_functional_40.py` — runner self-wait.** The runner
   polls `${base_url}/health` for up to 60 s before iterating. Exits 2
   if `/health` never comes up — no more silent 100 %-fail reports.

## Functional retest — 40/40 = 100 % pass-like

After the second deploy attempt (with the chat server already warm):

| Verdict | Count |
|---|---|
| pass (tool-driven) | 31 |
| pass-no-tool (introduction / scope-less learning / vocab-no-author) | 3 |
| out_of_scope (translation, genre, dialogue, gender, movement-no-scope) | 6 |

Highlights from this round:

- **Q5** (`Bleak House` vs `Adventures of Huckleberry Finn`) — routes to
  `book_compare`, 8.5 s. v2.0.7 fix holds.
- **Q27** (Мелвилл/Конрад/Стивенсон) — 3× `affinity_by_author`
  parallel chain (Sprint 11.3 multi-author), 21.3 s.
- **Q34** (lexical wealth via `top_authors_by(metric=tokens)`) — 8.4 s.
  Was 60 s+ in v2.0.x before the Sprint 11.2 `author_tokens.json` cache.
- **Q40** — the whole reason Sprint 11.4 existed. Routes to
  `composite_compare`, runs the 4-step plan
  (`top_authors_by_country GB` → `top_authors_by_country US` →
  `affinity_by_author` for GB leader → `affinity_by_author` for US
  leader) in 22.3 s. LLM/renderer composes a real lexical contrast
  between British and American 1850-1920 prose with signature words for
  the leader of each country.

No timeouts, no `partial`, no failed tool calls.

## Tests

- Unit: **191/191** (held over the runner edit)
- Functional 40/40 end-to-end: **100 % pass-like**
- 10 historical caught bugs still PASS regression probes
- v2.2 changes (composite_compare, author_regex inject, NAME_AFTER_KEY,
  author_closest tightening) all behave as expected on real LLM + corpus

Full functional report:
`docs/v2/test_report_v2.2.1_2026-05-18.md`

Co-developed with Claude Opus 4.7 (1M context).
