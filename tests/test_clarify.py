#!/usr/bin/env python3
"""Tests for search_engine/clarify.py: live question quality for a genuine
sibling case and for a large heterogeneous fan-out (grouped options), and
validation/retry/fallback behavior under a stubbed (mocked) model response
that returns a code outside the candidate list.

Requires ANTHROPIC_API_KEY / CLAUDE_MODEL_FAST in .env for the two live cases
(tests 1 and 2); test 3 stubs the model call and needs no network access.

Run with: DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib .venv/bin/python tests/test_clarify.py
(see README for why DYLD_LIBRARY_PATH is needed on this dev machine)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search_engine.core import CodeResult, DB_PATH, search
from search_engine.clarify import clarify


def _candidates_for(conn, codes):
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, code_formatted, long_desc, short_title FROM tabular_codes WHERE code IN ({placeholders})",
        codes,
    ).fetchall()
    return [
        CodeResult(code=r[0], code_formatted=r[1], description=r[2], short_title=r[3], score=1.0, reasons=[])
        for r in rows
    ]


def _assert_valid_partition(question, candidates) -> tuple[bool, str]:
    expected_codes = {c.code_formatted for c in candidates}
    seen = set()
    for opt in question.options:
        for code in opt.codes:
            if code not in expected_codes:
                return False, f"option code {code!r} not in candidate list"
            if code in seen:
                return False, f"code {code!r} appears in more than one option"
            seen.add(code)
    if seen != expected_codes:
        return False, f"partition doesn't cover all candidates: missing {expected_codes - seen}"
    return True, "ok"


def test_genuine_sibling_case_fires_a_question(conn) -> tuple[bool, str]:
    """K57.30-33 (all children of K57.3) -- clarify() should fire and produce a
    valid partition; since these are 4 genuinely distinct leaf conditions, one
    option per code is the natural (though not required) shape."""
    codes = ["K5730", "K5731", "K5732", "K5733"]
    candidates = _candidates_for(conn, codes)
    if len(candidates) != 4:
        return False, f"fixture setup broken: expected 4 candidates, got {len(candidates)}"

    question = clarify(candidates, conn=conn)
    if question is None:
        return False, "expected a question, got None"

    ok, detail = _assert_valid_partition(question, candidates)
    if not ok:
        return False, detail

    return True, f"question={question.question!r}, {len(question.options)} option(s)"


def test_large_heterogeneous_case_groups_options(conn) -> tuple[bool, str]:
    """Bare 'hip' fans out across several unrelated conditions (congenital
    dislocation, Coxa plana, contracture, ankylosis, loose body, ...).
    clarify() must still fire (no count/sibling cap), produce a valid
    partition of all candidates, and -- since the set is large and
    heterogeneous -- group at least one option to more than one code rather
    than emitting a literal one-option-per-code list."""
    result = search("hip")
    if result.is_single:
        return False, "expected 'hip' to be a list result, got single"
    if len(result.results) < 10:
        return False, f"expected a large heterogeneous result set, got {len(result.results)}"

    question = clarify(result.results, conn=conn)
    if question is None:
        return False, "expected a question, got None"

    ok, detail = _assert_valid_partition(question, result.results)
    if not ok:
        return False, detail

    if len(question.options) >= len(result.results):
        return False, (
            f"expected grouping (fewer options than candidates) for {len(result.results)} "
            f"heterogeneous candidates, got {len(question.options)} options -- no grouping happened"
        )

    return True, f"{len(result.results)} candidates grouped into {len(question.options)} options"


def test_forced_validation_failure_falls_back(conn) -> tuple[bool, str]:
    """Stub the model to return a code that was never in the candidate list --
    validation must reject it, retry once, reject again, and clarify() must
    return None (the caller's cue to fall back to the plain list) rather than
    ever surfacing the bad option."""
    codes = ["M25551", "M25552", "M25559"]
    candidates = _candidates_for(conn, codes)
    if len(candidates) != 3:
        return False, f"fixture setup broken: expected 3 candidates, got {len(candidates)}"

    call_count = {"n": 0}

    def bad_stub(payload):
        call_count["n"] += 1
        return {
            "question": "Which matches?",
            "options": [{"label": "not a real candidate", "codes": ["Z99.99"]}],
        }

    question = clarify(candidates, conn=conn, call_model=bad_stub)
    if question is not None:
        return False, f"expected None after failed validation, got: {question}"
    if call_count["n"] != 2:
        return False, f"expected exactly 2 model call attempts (1 try + 1 retry), got {call_count['n']}"

    return True, "rejected bad code and fell back to None after 2 attempts"


def main():
    conn = sqlite3.connect(DB_PATH)
    tests = [
        ("Genuine sibling case fires a valid question", test_genuine_sibling_case_fires_a_question),
        ("Large heterogeneous fan-out groups into fewer options", test_large_heterogeneous_case_groups_options),
        ("Forced validation failure falls back to plain list", test_forced_validation_failure_falls_back),
    ]
    passed = 0
    for name, fn in tests:
        try:
            ok, detail = fn(conn)
        except Exception as e:
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        print(f"         {detail}")
        if ok:
            passed += 1
    conn.close()
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
