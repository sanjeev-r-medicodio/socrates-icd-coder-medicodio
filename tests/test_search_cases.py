"""Regression suite: every case in tests/cases.json against core.search().

A case with a ``known_issue`` is expected to fail today (strict xfail). When a
fix makes it pass, pytest reports XPASS as a failure -- remove the
``known_issue`` field in the same change so the case becomes a normal guard.
"""
import json
from pathlib import Path

import pytest

from search_engine.core import search

CASES = json.loads((Path(__file__).resolve().parent / "cases.json").read_text())


def normalize(code: str) -> str:
    return code.replace(".", "").upper()


def check_case(case: dict, result) -> tuple[bool, str]:
    expected = normalize(case["expected_code"])
    codes = [normalize(r.code) for r in result.results]
    mode = case.get("mode", "member")

    if mode == "single":
        if not result.is_single:
            return False, f"expected a single result, got {len(result.results)}"
        if not codes or codes[0] != expected:
            return False, f"expected single {case['expected_code']}, got {codes[:1] or '(none)'}"
    elif mode == "top":
        if not codes or codes[0] != expected:
            return False, f"expected top result {case['expected_code']}, got {codes[:5]}"
    elif mode == "member":
        if expected not in codes:
            return False, f"expected {case['expected_code']} in results, got {codes[:5]}..."
    else:
        return False, f"unknown mode {mode!r}"

    if case.get("expect_list") and result.is_single:
        return False, "expected a list of candidates, got a single result"
    if "expect_laterality" in case and result.laterality_filter != case["expect_laterality"]:
        return False, f"expected laterality {case['expect_laterality']!r}, got {result.laterality_filter!r}"
    return True, "ok"


def _param(case):
    marks = []
    if case.get("known_issue"):
        marks.append(pytest.mark.xfail(reason=case["known_issue"], strict=True))
    return pytest.param(case, id=case["query"][:60], marks=marks)


@pytest.mark.parametrize("case", [_param(c) for c in CASES])
def test_search_case(case, db_path):
    result = search(case["query"], db_path=db_path,
                    laterality=case.get("laterality"), encounter=case.get("encounter"))
    ok, detail = check_case(case, result)
    assert ok, f"{case['description']}: {detail}"
