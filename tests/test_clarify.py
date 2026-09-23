"""Tests for search_engine/clarify.py: live question quality for a genuine
sibling case and for a large heterogeneous fan-out (grouped options), and
validation/retry/fallback behavior under a stubbed (mocked) model response
that returns a code outside the candidate list.

The two ``live`` tests call the model (ICD_CODER_LIVE_TESTS=1 to run them);
the stubbed test needs no network access.

Run with: python -m pytest tests/test_clarify.py
"""
import pytest

from search_engine.clarify import clarify
from search_engine.core import CodeResult, search


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


def _assert_valid_partition(question, candidates):
    expected_codes = {c.code_formatted for c in candidates}
    seen = set()
    for opt in question.options:
        for code in opt.codes:
            assert code in expected_codes, f"option code {code!r} not in candidate list"
            assert code not in seen, f"code {code!r} appears in more than one option"
            seen.add(code)
    assert seen == expected_codes, f"partition doesn't cover all candidates: missing {expected_codes - seen}"


@pytest.mark.live
def test_genuine_sibling_case_fires_a_question(conn):
    """K57.30-33 (all children of K57.3) -- clarify() should fire and produce a
    valid partition."""
    candidates = _candidates_for(conn, ["K5730", "K5731", "K5732", "K5733"])
    assert len(candidates) == 4, "fixture setup broken"

    question = clarify(candidates, conn=conn)
    assert question is not None, "expected a question, got None"
    _assert_valid_partition(question, candidates)


@pytest.mark.live
def test_large_heterogeneous_case_groups_options(conn, db_path):
    """Bare 'hip' fans out across several unrelated conditions. clarify() must
    produce a valid partition and group at least one option to more than one
    code rather than emitting one option per code."""
    result = search("hip", db_path=db_path)
    assert not result.is_single
    assert len(result.results) >= 10, f"expected a large result set, got {len(result.results)}"

    question = clarify(result.results, conn=conn)
    assert question is not None, "expected a question, got None"
    _assert_valid_partition(question, result.results)
    assert len(question.options) < len(result.results), "no grouping happened"


def test_forced_validation_failure_falls_back(conn):
    """Stub the model to return a code that was never in the candidate list --
    validation must reject it, retry once, reject again, and clarify() must
    return None rather than ever surfacing the bad option."""
    candidates = _candidates_for(conn, ["M25551", "M25552", "M25559"])
    assert len(candidates) == 3, "fixture setup broken"

    call_count = {"n": 0}

    def bad_stub(payload):
        call_count["n"] += 1
        return {
            "question": "Which matches?",
            "options": [{"label": "not a real candidate", "codes": ["Z99.99"]}],
        }

    assert clarify(candidates, conn=conn, call_model=bad_stub) is None
    assert call_count["n"] == 2, "expected exactly 2 model call attempts (1 try + 1 retry)"
