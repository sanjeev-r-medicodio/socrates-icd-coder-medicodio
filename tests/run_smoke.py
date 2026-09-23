#!/usr/bin/env python3
"""Run tests/cases.json against search_engine.core.search() and report pass/fail.

Quick, dependency-free view of the same cases tests/test_search_cases.py runs
under pytest. Cases with a ``known_issue`` are reported as KNOWN (expected to
fail) and don't affect the exit code -- unless they unexpectedly pass, which
means the ``known_issue`` field should be removed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from search_engine.core import search
from test_search_cases import check_case

CASES_PATH = Path(__file__).resolve().parent / "cases.json"


def main():
    cases = json.loads(CASES_PATH.read_text())
    bad = 0
    for case in cases:
        ok, detail = check_case(case, search(case["query"]))
        known = case.get("known_issue")
        if known:
            status = "XPASS" if ok else "KNOWN"
            if ok:
                bad += 1
                detail = f"passes now -- remove known_issue ({known})"
            else:
                detail = f"{detail} [{known}]"
        else:
            status = "PASS" if ok else "FAIL"
            bad += 0 if ok else 1
        print(f"[{status}] {case['description']}")
        print(f"         query: {case['query']!r} -> {detail}")
    print(f"\n{len(cases) - bad}/{len(cases)} as expected")
    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
