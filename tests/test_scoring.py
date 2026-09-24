"""Unit tests for search_engine.core scoring helpers."""
from search_engine.core import _Candidate, _query_covers, _ref_prefixes, search


def test_query_covers_requires_every_identifying_word():
    q = {"pain", "hip"}
    assert not _query_covers(q, "joint pain")                 # 'joint' missing
    assert not _query_covers(q, "pain in hand")
    assert _query_covers({"joint", "pain"}, "joint pain")
    assert _query_covers({"chest", "pain"}, "chest pain, unspecified type")  # stopwords ignored
    assert not _query_covers(q, "unspecified")                # nothing identifying


def test_ref_prefixes_shapes():
    assert _ref_prefixes("M25.5-") == ["M255"]
    assert _ref_prefixes("R10.-") == ["R10"]
    assert _ref_prefixes("B01.-, B02.-") == ["B01", "B02"]
    assert _ref_prefixes("J09.X3, J10.2, J11.2") == ["J09X3", "J102", "J112"]
    assert _ref_prefixes("C81-C86") == []                     # block ranges skipped
    assert _ref_prefixes(None) == []


def test_penalty_lowers_score_and_keeps_largest():
    c = _Candidate()
    c.add("tabular", 10.0, "t")
    c.penalize("excluded", 2.0, "x")
    c.penalize("excluded", 1.0, "x")
    assert c.score == 8.0


def test_excludes_note_never_boosts_its_owner(db_path):
    # R52 carries 18 Excludes1 notes mentioning "... pain"; none may appear
    # as a positive reason for R52 on a pain query.
    for r in search("pain in right hip", db_path=db_path).results + search("hip pain", db_path=db_path).results:
        assert not any(reason.startswith("Note reference (excludes") for reason in r.reasons), r.code


def test_fully_described_excluded_condition_is_redirected(db_path):
    # R52 excludes "joint pain (M25.5-)": the query is exactly that condition,
    # so R52 is penalized and M25.5- codes get the redirect.
    results = search("joint pain", db_path=db_path).results
    codes = [r.code for r in results]
    assert codes[0].startswith("M255")
    assert "R52" not in codes[:5]
