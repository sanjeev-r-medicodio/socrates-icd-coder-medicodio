#!/usr/bin/env python3
"""Run tests/cases.json against search_engine.core.search() and report pass/fail.

Run this after each meaningful ranking change.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search_engine.core import search

CASES_PATH = Path(__file__).resolve().parent / "cases.json"


def normalize(code: str) -> str:
    return code.replace(".", "").upper()


def run_case(case: dict) -> tuple[bool, str]:
    result = search(case["query"])
    expected = normalize(case["expected_code"])
    result_codes = [normalize(r.code) for r in result.results]

    mode = case.get("mode", "member")
    if mode == "single":
        if not result.is_single:
            return False, f"expected a single result, got {len(result.results)}"
        if not result_codes or result_codes[0] != expected:
            got = result_codes[0] if result_codes else "(none)"
            return False, f"expected top result {case['expected_code']}, got {got}"
    elif mode == "member":
        if expected not in result_codes:
            return False, f"expected {case['expected_code']} somewhere in results, got {result_codes[:5]}..."
    else:
        return False, f"unknown mode {mode!r}"

    if case.get("expect_list") and result.is_single:
        return False, "expected a list of candidates, got a single result"

    if "expect_laterality" in case and result.laterality_filter != case["expect_laterality"]:
        return False, f"expected laterality filter {case['expect_laterality']!r}, got {result.laterality_filter!r}"

    return True, "ok"


def main():
    cases = json.loads(CASES_PATH.read_text())
    passed = 0
    for case in cases:
        ok, detail = run_case(case)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case['description']}")
        print(f"         query: {case['query']!r} -> {detail}")
        if ok:
            passed += 1
    print(f"\n{passed}/{len(cases)} passed")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
