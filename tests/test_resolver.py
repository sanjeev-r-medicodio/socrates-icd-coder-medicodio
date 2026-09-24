"""Offline tests for search_engine.resolver, with stubbed model calls.

The stub clarifier gives one option per item; the stub answerer knows the
target code and picks the option that leads to it (or answers 0 = not
documented when told to), always quoting a sentence that is in the note.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List

import pytest

from search_engine.resolver import (
    CONFIDENCE_SINGLE_SEARCH_RESULT, CONFIDENCE_UNSPECIFIED_DEFAULT, CONFIDENCE_UNVERIFIED_CITATION,
    MAX_CLARIFY_CANDIDATES, Diagnosis, citation_in_note, resolve,
)


@dataclass
class _Opt:
    label: str
    codes: List[str]


@dataclass
class _Q:
    question: str
    options: List[_Opt]


def one_option_per_item(items, conn):
    assert len(items) <= MAX_CLARIFY_CANDIDATES, "resolver must stage before calling clarify"
    return _Q("Which one?", [_Opt(f"{c.code_formatted} {c.description}", [c.code_formatted]) for c in items])


def group_by_prefix(n):
    """Stub clarifier that groups items sharing their first n code characters."""
    def fn(items, conn):
        groups = {}
        for c in items:
            groups.setdefault(c.code[:n], []).append(c.code_formatted)
        if len(groups) == 1:
            return one_option_per_item(items, conn)
        return _Q("Which kind?", [_Opt(k, v) for k, v in groups.items()])
    return fn


def answer_toward(target: str, quote: str, undocumented=lambda options: False):
    """Picks the option leading to the target: an option code that is a prefix
    of the target, or that lies under it (dots ignored)."""
    t = target.replace(".", "")
    calls = []

    def fn(note, question, options):
        calls.append(options)
        if undocumented(options):
            return {"option": 0, "citation": ""}
        for o in options:
            if any(t.startswith(c.replace(".", "")) or c.replace(".", "").startswith(t) for c in o["codes"]):
                return {"option": o["number"], "citation": quote}
        return {"option": 0, "citation": ""}
    fn.calls = calls
    return fn


NOTE = "Patient reports pain in the right hip for two weeks. No trauma. Exam: tenderness over the hip."


def never(*a):
    raise AssertionError("no model call expected")


def test_single_search_result_resolves_without_model_calls(db_path):
    r = resolve(Diagnosis("NASH"), NOTE, db_path=db_path, clarify_fn=never, answer_fn=never)
    assert (r.status, r.code, r.confidence) == ("resolved", "K75.81", CONFIDENCE_SINGLE_SEARCH_RESULT)


def test_structured_laterality_and_location_build_the_query(db_path):
    answer = answer_toward("M25.551", "pain in the right hip")
    r = resolve(Diagnosis("pain", laterality="right", anatomical_location="hip"), NOTE,
                db_path=db_path, clarify_fn=one_option_per_item, answer_fn=answer)
    assert r.query == "pain hip" and r.code == "M25.551"
    # laterality came from the structured field, so no left/unspecified-side hip codes were offered
    offered = {c for opts in answer.calls for o in opts for c in o["codes"]}
    assert not {"M25.552", "M25.559"} & offered


def test_low_scoring_tail_is_not_considered(db_path):
    answer = answer_toward("M25.551", "pain in the right hip")
    r = resolve(Diagnosis("pain in right hip"), NOTE, db_path=db_path,
                clarify_fn=one_option_per_item, answer_fn=answer)
    assert r.code == "M25.551"
    assert r.candidates_considered < 100          # search ranks 400+; the tail is noise


def test_large_candidate_set_is_staged_by_category_not_truncated(db_path):
    answer = answer_toward("M24.651", "tenderness over the hip")
    r = resolve(Diagnosis("hip"), NOTE, db_path=db_path, clarify_fn=one_option_per_item, answer_fn=answer)
    assert r.candidates_considered > MAX_CLARIFY_CANDIDATES
    assert r.rounds[0].staged
    assert (r.status, r.code) == ("resolved", "M24.651")
    assert r.confidence == 1.0


def test_not_documented_picks_unspecified_code_in_group(db_path):
    # Round 1 picks the hip-pain group (M25.55x); "which hip?" is not documented.
    in_hip_group = lambda opts: all(c.startswith("M25.55") for o in opts for c in o["codes"])
    answer = answer_toward("M25.55", "tenderness over the hip", undocumented=in_hip_group)
    r = resolve(Diagnosis("hip pain"), NOTE, db_path=db_path, clarify_fn=group_by_prefix(5), answer_fn=answer)
    assert (r.status, r.code) == ("unspecified_default", "M25.559")
    assert r.confidence == CONFIDENCE_UNSPECIFIED_DEFAULT
    assert r.rounds[-1].chosen == 0


def test_not_documented_across_families_is_unresolved(db_path):
    r = resolve(Diagnosis("hip pain"), NOTE, db_path=db_path, clarify_fn=one_option_per_item,
                answer_fn=lambda *a: {"option": 0, "citation": ""})
    assert r.status == "unresolved" and r.code is None and r.remaining


def test_undocumented_seventh_character_is_reported_not_guessed(db_path):
    only_7th = lambda opts: len({c.replace(".", "")[:6] for o in opts for c in o["codes"]}) == 1
    answer = answer_toward("S72.001", "fracture of neck of right femur", undocumented=only_7th)
    note = "X-ray confirms fracture of neck of right femur."
    r = resolve(Diagnosis("fracture of neck of right femur"), note, db_path=db_path,
                clarify_fn=one_option_per_item, answer_fn=answer)
    assert r.status == "seventh_char_unresolved" and r.seventh_character_unresolved
    assert r.code is None
    assert r.remaining and all(c.startswith("S72.001") for c in r.remaining)


def test_encounter_resolves_the_seventh_character(db_path):
    answer = answer_toward("S72.001A", "closed fracture")
    note = "Initial visit: closed fracture of neck of right femur."
    r = resolve(Diagnosis("fracture of neck of right femur", encounter="initial"), note,
                db_path=db_path, clarify_fn=one_option_per_item, answer_fn=answer)
    assert (r.status, r.code) == ("resolved", "S72.001A")


def test_unverified_citation_is_retried_then_penalized(db_path):
    answer = answer_toward("M25.551", "the patient has right hip pain")   # not verbatim in NOTE
    r = resolve(Diagnosis("hip pain"), NOTE, db_path=db_path, clarify_fn=one_option_per_item, answer_fn=answer)
    assert r.code == "M25.551"
    assert len(answer.calls) == 2 * len(r.rounds)     # every round retried once
    assert not any(rd.citation_verified for rd in r.rounds)
    assert r.confidence == pytest.approx(max(0.0, 1.0 - CONFIDENCE_UNVERIFIED_CITATION * len(r.rounds)))


def test_invalid_option_twice_is_treated_as_not_documented(db_path):
    calls = []

    def bad(*a):
        calls.append(1)
        return {"option": 99, "citation": "x"}
    r = resolve(Diagnosis("hip pain"), NOTE, db_path=db_path, clarify_fn=one_option_per_item, answer_fn=bad)
    assert len(calls) == 2
    assert r.rounds[0].chosen == 0 and r.code is None


def test_no_question_leaves_it_unresolved_with_remaining(db_path):
    r = resolve(Diagnosis("hip pain"), NOTE, db_path=db_path, clarify_fn=lambda items, conn: None,
                answer_fn=lambda *a: {"option": 1, "citation": ""})
    assert r.status == "unresolved" and "M25.551" in r.remaining


def test_no_candidates(db_path):
    r = resolve(Diagnosis("qqqzzz"), NOTE, db_path=db_path)
    assert r.status == "no_candidates" and r.code is None


def test_concurrent_calls_are_independent(db_path):
    cases = [("pain in right hip", "M25.551"), ("NASH", "K75.81"),
             ("poisoning by heroin, accidental, initial encounter", "T40.1X1A")] * 4
    def run(case):
        return resolve(Diagnosis(case[0]), NOTE, db_path=db_path, clarify_fn=one_option_per_item,
                       answer_fn=answer_toward(case[1], "pain in the right hip")).code
    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(run, cases))
    assert codes == [c[1] for c in cases]


@pytest.mark.parametrize("citation, ok", [
    ("pain in the right hip", True),
    ("PAIN IN THE   RIGHT hip", True),
    ('"No trauma."', True),
    ("pain in the left hip", False),
    ("", False),
])
def test_citation_in_note(citation, ok):
    assert citation_in_note(citation, NOTE) is ok
