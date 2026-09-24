"""Integrity of the built code set against the CMS order file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_db import ORDER_FILE, parse_order_file  # noqa: E402


def test_every_billable_order_file_code_is_billable_in_db(conn):
    order = parse_order_file(ORDER_FILE)
    expected = {c for c, v in order.items() if v["is_billable"]}
    actual = {r[0] for r in conn.execute("SELECT code FROM tabular_codes WHERE is_billable = 1")}
    assert expected == actual, f"missing {sorted(expected - actual)[:5]}, extra {sorted(actual - expected)[:5]}"


def test_generated_codes_hang_off_a_stem_with_valid_seventh_char(conn):
    bad = conn.execute("""
        SELECT g.code FROM tabular_codes g
        LEFT JOIN tabular_codes stem ON stem.code = g.parent_code
        LEFT JOIN seventh_char_defs d
               ON d.code = g.seventh_char_source_code AND d.char_value = g.seventh_char
        WHERE g.seventh_char IS NOT NULL
          AND (stem.code IS NULL OR stem.is_billable = 1 OR d.id IS NULL
               OR length(g.code) != 7 OR g.chapter_num IS NULL)
        LIMIT 5""").fetchall()
    assert not bad, f"malformed generated codes: {bad}"


def test_placeholder_padding_is_x(conn):
    rows = conn.execute("""
        SELECT code, parent_code FROM tabular_codes
        WHERE seventh_char IS NOT NULL AND length(parent_code) < 6""").fetchall()
    assert rows, "expected some placeholder-X codes (e.g. S01.00XA)"
    for code, stem in rows:
        assert set(code[len(stem):-1]) == {"X"}, f"{code}: padding after {stem} is not X"


def test_known_seventh_char_codes(conn):
    rows = dict(conn.execute("""
        SELECT code, code_formatted FROM tabular_codes
        WHERE code IN ('S72001A', 'T401X1A', 'S0100XA', 'O0901') AND is_billable = 1""").fetchall())
    assert rows == {"S72001A": "S72.001A", "T401X1A": "T40.1X1A", "S0100XA": "S01.00XA", "O0901": "O09.01"}


def test_generated_codes_not_in_fts(conn):
    n = conn.execute("""
        SELECT COUNT(*) FROM fts_tabular ft JOIN tabular_codes tc ON tc.code = ft.code
        WHERE tc.seventh_char IS NOT NULL""").fetchone()[0]
    assert n == 0, "generated 7-character codes must stay out of fts_tabular (bm25 stability)"
