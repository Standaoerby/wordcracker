"""S-P1 — enrich_word / _maybe_translate ride the warm model.

Both utilities previously hardcoded ``"model": "qwen3:14b"``. In prod
``WC_LLM_MODEL=wordcracker:v2`` is the warm, resident model; the hardcode
cold-loaded a *second* qwen3:14b and contended for the 24 GB VRAM, blowing
the enrich (90 s) and translate (60 s) timeouts.

After S-P1 both calls resolve the model from ``WC_LLM_MODEL`` (same pattern
as critic/planner/rag_v2) and pin the Ollama ``system`` field so the baked
``wordcracker:v2`` operator persona cannot contaminate output:

  * enrich_word keeps ``format=json`` (structure already pinned) + a narrow
    "lexical annotation engine" system.
  * _maybe_translate has no json guard, so the persona is the real risk —
    a bare "translation engine" system keeps the output to the English
    string only.

Pure unit tests — no Ollama. We patch ``requests.post`` and read the payload
that *would* have been sent. These would fail on the pre-S-P1 code (model
hardcoded, no ``system`` key).
"""
import json
import os
import sys
from pathlib import Path
from unittest import mock

# learning_tools.py does a bare ``from rag_tools import ...``, so both the
# repo root (for the ``scripts`` package) and scripts/ itself (for the bare
# import) must be importable when this file runs in isolation. The full
# suite already arranges this; we replicate it so ``pytest <thisfile>`` works.
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.learning_tools as lt
import scripts.rag_tools as rt


def _fake_response(response_text):
    resp = mock.Mock()
    resp.json.return_value = {"response": response_text}
    resp.raise_for_status.return_value = None
    return resp


# --------------------------------------------------------------------------
# enrich_word
# --------------------------------------------------------------------------
_ENRICH_JSON = json.dumps({
    "translation": "удовольствие", "pos": "noun",
    "definition_en": "a feeling of happy satisfaction",
    "example_sentence": "It was a pleasure to read.",
    "etymology": "from Latin placere", "cefr_estimate": "B1",
    "archaic": False, "archaic_note": "",
})


def _capture_enrich_payload(env):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["payload"] = json
        return _fake_response(_ENRICH_JSON)

    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(lt, "_load_word_dict", return_value={}), \
         mock.patch.object(lt, "_save_word_dict"), \
         mock.patch("requests.post", side_effect=fake_post):
        lt.enrich_word("pleasure", force_refresh=True)
    return captured["payload"]


def test_enrich_model_defaults_to_qwen():
    payload = _capture_enrich_payload({})
    assert payload["model"] == "qwen3:14b"


def test_enrich_model_resolves_from_env():
    payload = _capture_enrich_payload({"WC_LLM_MODEL": "warm-model:99"})
    assert payload["model"] == "warm-model:99"


def test_enrich_has_persona_neutralizing_system_and_keeps_json():
    payload = _capture_enrich_payload({"WC_LLM_MODEL": "wordcracker:v2"})
    assert "system" in payload, "enrich must override the baked persona"
    assert "JSON" in payload["system"]
    # the json-structure guard must survive the change
    assert payload["format"] == "json"


# --------------------------------------------------------------------------
# _maybe_translate
# --------------------------------------------------------------------------
def _capture_translate_payload(env):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["payload"] = json
        return _fake_response("Jeeves and Bertie Wooster")

    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch("requests.post", side_effect=fake_post):
        out = rt._maybe_translate("Дживс и Берти")
    return captured["payload"], out


def test_translate_model_defaults_to_qwen():
    payload, _ = _capture_translate_payload({})
    assert payload["model"] == "qwen3:14b"


def test_translate_model_resolves_from_env():
    payload, out = _capture_translate_payload({"WC_LLM_MODEL": "warm-model:99"})
    assert payload["model"] == "warm-model:99"
    assert out == "Jeeves and Bertie Wooster"


def test_translate_has_translation_engine_system():
    payload, _ = _capture_translate_payload({"WC_LLM_MODEL": "wordcracker:v2"})
    assert "system" in payload, "translate must override the baked persona"
    assert "translation" in payload["system"].lower()
