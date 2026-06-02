"""S-P2 regression guard — static audit: EVERY wordcracker:v2 Ollama
caller must pass an explicit `num_ctx` in its request options.

Why a source-level test (not just the behavioural one in
test_sp2_numctx_align.py): the wedge that S-P2 fixed is re-introduced the
moment ANYONE adds a new `requests.post(.../api/(generate|chat))` to the
v2 stack and forgets `num_ctx`. A single missing num_ctx lets Ollama fall
back to the Modelfile default and a flip rebuilds the single KvSize runner
(~7s reload) → pseudo-wedge. The behavioural test only covers the three
sites that were broken in this sprint; this one fails loudly on ANY future
v2 caller that ships without num_ctx.

Mechanism: for each known v2 module that talks to Ollama, scan the source
for every `/api/generate` or `/api/chat` POST and assert the surrounding
payload block contains a `num_ctx` key. Pure source inspection — no
imports, no network, no heavy deps (chromadb etc. not required).

If you ADD a v2 Ollama caller, add its file to V2_OLLAMA_CALLER_FILES and
make sure the payload carries num_ctx via get_model_ctx/TokenBudget.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Every module in the v2 stack that POSTs to Ollama. Kept explicit (not
# globbed) so adding a caller is a deliberate, reviewed edit to this list.
V2_OLLAMA_CALLER_FILES = [
    "scripts/learning_tools.py",        # enrich_word            -> /api/generate
    "scripts/rag_tools.py",             # _maybe_translate       -> /api/generate
    "scripts/v2/rag_v2.py",             # renderer _llm_render   -> /api/chat
    "scripts/v2/critic.py",             # critic                 -> /api/chat
    "scripts/v2/planner/llm_intent.py", # classify_and_extract + classify_with_llm
    "scripts/v2/planner/llm_planner.py",# planner                -> /api/chat
]

# Matches the POST to either Ollama inference endpoint, but NOT the
# server-side route strings (/api/chat/stream) which are not outbound calls.
_POST_RE = re.compile(r"requests\.post\(\s*f?\"[^\"]*?/api/(?:generate|chat)\b")


class EveryV2OllamaCallerSendsNumCtx(unittest.TestCase):
    def test_each_callsite_carries_num_ctx(self):
        checked = 0
        for rel in V2_OLLAMA_CALLER_FILES:
            path = _REPO / rel
            self.assertTrue(path.exists(), f"missing v2 caller file: {rel}")
            lines = path.read_text(encoding="utf-8").splitlines()
            post_lines = [
                i for i, ln in enumerate(lines) if _POST_RE.search(ln)
            ]
            self.assertTrue(
                post_lines,
                f"{rel}: expected at least one Ollama POST; the audit list is "
                f"stale (call-site moved/renamed?)",
            )
            for i in post_lines:
                # The payload dict is either built inline in the post() call
                # or assigned just above it. A generous window catches both
                # styles without coupling to exact line offsets.
                window = "\n".join(lines[max(0, i - 35): i + 6])
                self.assertIn(
                    "num_ctx", window,
                    f"{rel}: Ollama POST near line {i + 1} has no num_ctx in "
                    f"its payload — S-P2 wedge regression (a v2 caller without "
                    f"num_ctx lets Ollama rebuild the shared runner on a ctx "
                    f"flip).",
                )
                checked += 1
        # Guard against the regex silently matching nothing across the board.
        self.assertGreaterEqual(
            checked, len(V2_OLLAMA_CALLER_FILES),
            "audit found fewer POSTs than caller files — scan likely broken",
        )


if __name__ == "__main__":
    unittest.main()
