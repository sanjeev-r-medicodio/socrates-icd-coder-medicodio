"""Engine-facing entry point: one extracted diagnosis in, one final_result out.

Shaped for nextgen-codio-engine's ICD pipeline, where it can stand in for the
S4.1 -> S4.2 -> S4.3 tree traversal of a single diagnosis:

    from search_engine import integration, llm
    llm.set_backend(engine_backend)        # optional: route model calls via the host
    final_result = integration.code_diagnosis(dx, chart_text, date_of_service)

`dx` is the engine's per-diagnosis dict (DXEX output): `phrase`, and
optionally `anatomical_location` (or `location`), `laterality`, and
`encounter` / `episode_of_care`. The returned dict carries the keys the
engine's aggregation step reads from an S4.3 final_result (`icd_code`,
`icd_code_desc`, `node_id`, `confidence`, `seventh_character_unresolved`),
plus `status`, `code_set` and a `trace` for audit. `icd_code` is always the
full billable code -- the 7th character is already included, so the host
must not merge a separate 7th character onto it. See docs/INTEGRATION.md.
"""
from dataclasses import asdict
from datetime import date, datetime
from typing import Callable, Optional, Union

from search_engine.resolver import Diagnosis, resolve

SELECTOR_NAME = "icd_coder"

_LATERALITY = {
    "right": "right", "rt": "right", "r": "right", "right side": "right",
    "left": "left", "lt": "left", "l": "left", "left side": "left",
    "bilateral": "bilateral", "both": "bilateral", "both sides": "bilateral", "b/l": "bilateral",
}
_ENCOUNTER = {
    "initial": "initial", "initial encounter": "initial", "a": "initial", "active treatment": "initial",
    "subsequent": "subsequent", "subsequent encounter": "subsequent", "d": "subsequent",
    "sequela": "sequela", "sequelae": "sequela", "s": "sequela",
}


def normalize_laterality(value) -> Optional[str]:
    """Maps engine/free-text laterality to 'left' | 'right' | 'bilateral';
    anything else (None, 'unspecified', 'N/A') means not documented."""
    return _LATERALITY.get(str(value).strip().lower()) if value else None


def normalize_encounter(value) -> Optional[str]:
    return _ENCOUNTER.get(str(value).strip().lower()) if value else None


def _to_date(value: Union[date, datetime, str, None]) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def code_diagnosis(
    dx: dict,
    chart_text: str,
    date_of_service: Union[date, datetime, str, None] = None,
    *,
    clarify_fn: Optional[Callable] = None,
    answer_fn: Optional[Callable] = None,
) -> dict:
    phrase = (dx.get("phrase") or "").strip()
    if not phrase:
        raise ValueError("dx['phrase'] is required")
    diagnosis = Diagnosis(
        phrase=phrase,
        laterality=normalize_laterality(dx.get("laterality")),
        encounter=normalize_encounter(dx.get("encounter") or dx.get("episode_of_care")),
        anatomical_location=dx.get("anatomical_location") or dx.get("location"),
    )
    res = resolve(diagnosis, chart_text, date_of_service=_to_date(date_of_service),
                  clarify_fn=clarify_fn, answer_fn=answer_fn)
    return {
        "icd_code": res.code,
        "icd_code_desc": res.description,
        "node_id": None,   # the engine's own tree id -- mapped by the host from icd_code
        "confidence": res.confidence,
        "seventh_character_unresolved": res.seventh_character_unresolved,
        "selector": SELECTOR_NAME,
        "status": res.status,
        "code_set": res.code_set,
        "trace": {
            "query": res.query,
            "candidates_considered": res.candidates_considered,
            "rounds": [asdict(r) for r in res.rounds],
            "remaining": res.remaining,
        },
    }
