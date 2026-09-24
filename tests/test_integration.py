"""Engine-facing contract: search_engine.integration and llm backend injection."""
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from search_engine import integration, llm
from search_engine.integration import code_diagnosis, normalize_encounter, normalize_laterality
from tests.test_resolver import answer_toward, group_by_prefix, one_option_per_item

ROOT = Path(__file__).resolve().parent.parent
FINAL_RESULT_KEYS = {"icd_code", "icd_code_desc", "node_id", "confidence", "seventh_character_unresolved"}


@pytest.mark.parametrize("raw, norm", [
    ("Right", "right"), ("RT", "right"), ("left side", "left"), ("bilateral", "bilateral"),
    ("both", "bilateral"), ("unspecified", None), ("", None), (None, None),
])
def test_normalize_laterality(raw, norm):
    assert normalize_laterality(raw) == norm


@pytest.mark.parametrize("raw, norm", [
    ("initial encounter", "initial"), ("A", "initial"), ("Subsequent", "subsequent"),
    ("sequelae", "sequela"), ("unknown", None), (None, None),
])
def test_normalize_encounter(raw, norm):
    assert normalize_encounter(raw) == norm


def test_engine_dx_dict_to_final_result_shape():
    dx = {"phrase": "pain", "anatomical_location": "hip", "laterality": "Right",
          "is_symptom": True, "is_confirmed": True}
    out = code_diagnosis(dx, "Pain in the right hip.", "2026-10-15",
                         clarify_fn=one_option_per_item,
                         answer_fn=answer_toward("M25.551", "Pain in the right hip."))
    assert FINAL_RESULT_KEYS <= out.keys()
    assert (out["icd_code"], out["status"], out["code_set"]) == ("M25.551", "resolved", "FY2027")
    assert out["node_id"] is None and out["seventh_character_unresolved"] is False
    assert out["trace"]["rounds"] and out["trace"]["query"] == "pain hip"
    json.dumps(out)   # trace must be serializable for the engine's run_store


def test_full_seventh_character_code_is_returned():
    dx = {"phrase": "fracture of neck of right femur", "episode_of_care": "initial encounter"}
    out = code_diagnosis(dx, "Closed fracture of neck of right femur.", date(2026, 10, 2),
                         clarify_fn=one_option_per_item,
                         answer_fn=answer_toward("S72.001A", "Closed fracture"))
    assert out["icd_code"] == "S72.001A"


def test_undocumented_seventh_character_flags_for_the_engine_to_drop():
    only_7th = lambda opts: len({c.replace(".", "")[:6] for o in opts for c in o["codes"]}) == 1
    out = code_diagnosis({"phrase": "fracture of neck of right femur"}, "Fracture of neck of right femur.",
                         datetime(2026, 10, 2, 9, 30), clarify_fn=group_by_prefix(6),
                         answer_fn=answer_toward("S72.001", "Fracture of neck of right femur.", undocumented=only_7th))
    assert out["icd_code"] is None and out["seventh_character_unresolved"] is True


def test_phrase_is_required():
    with pytest.raises(ValueError):
        code_diagnosis({"phrase": "  "}, "note")


def test_host_backend_receives_every_model_call():
    calls = []

    def backend(tier, system, user, schema):
        calls.append(tier)
        if tier == "fast":   # clarify: one option per candidate code in the prompt
            payload = json.loads(user.split("\n", 1)[1])
            return {"question": "Which?", "options": [{"label": c["description"], "codes": [c["code"]]}
                                                      for c in payload]}
        return {"option": 1, "citation": "hip pain"}

    llm.set_backend(backend)
    try:
        out = code_diagnosis({"phrase": "hip pain"}, "Patient has hip pain.", "2026-10-15")
    finally:
        llm.set_backend(None)
    assert out["status"] == "resolved"
    assert "fast" in calls and "reasoning" in calls


def test_modules_import_without_api_key():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "CLAUDE_MODEL"))}
    env.update(ANTHROPIC_API_KEY="", CLAUDE_MODEL_FAST="", CLAUDE_MODEL_REASONING="")
    code = "import search_engine.clarify, search_engine.resolver, search_engine.integration; print('ok')"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
