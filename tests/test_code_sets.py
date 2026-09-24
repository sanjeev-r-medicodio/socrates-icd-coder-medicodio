"""Date-of-service -> fiscal-year code set selection."""
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_db import parse_order_file  # noqa: E402

from search_engine import code_sets  # noqa: E402
from search_engine.code_sets import CODE_SETS, UnsupportedDateOfService, for_date  # noqa: E402
from search_engine.resolver import Diagnosis, resolve  # noqa: E402

BUILT = [cs for cs in CODE_SETS if cs.db_path.exists()]


@pytest.mark.parametrize("day, fy", [
    (date(2025, 10, 1), 2026),
    (date(2026, 9, 30), 2026),     # last day of FY2026
    (date(2026, 10, 1), 2027),     # first day of FY2027
    (date(2027, 9, 30), 2027),
])
def test_fiscal_year_boundaries(day, fy):
    assert for_date(day).fiscal_year == fy


@pytest.mark.parametrize("day", [date(2025, 9, 30), date(2027, 10, 1)])
def test_dates_outside_loaded_years_are_rejected(day):
    with pytest.raises(UnsupportedDateOfService):
        for_date(day)


@pytest.mark.parametrize("cs", BUILT, ids=lambda cs: f"FY{cs.fiscal_year}")
def test_each_build_matches_its_own_order_file(cs):
    order = parse_order_file(cs.order_file)
    expected = {c for c, v in order.items() if v["is_billable"]}
    with sqlite3.connect(cs.db_path) as conn:
        actual = {r[0] for r in conn.execute("SELECT code FROM tabular_codes WHERE is_billable = 1")}
        info = conn.execute("SELECT fiscal_year, effective_from, effective_to FROM code_set_info").fetchone()
    assert expected == actual
    assert info == (cs.fiscal_year, cs.effective_from.isoformat(), cs.effective_to.isoformat())


needs_both = pytest.mark.skipif(len(BUILT) < 2, reason="needs FY2026 and FY2027 builds")


@needs_both
def test_same_diagnosis_codes_differently_across_the_october_boundary():
    # I42.0 Dilated cardiomyopathy is billable in FY2026 and split into
    # subcodes in FY2027, so a claim on 2026-09-30 and one on 2026-10-01
    # must not get the same code.
    def one_per_item(items, conn):
        from dataclasses import dataclass

        @dataclass
        class O:
            label: str
            codes: list

        @dataclass
        class Q:
            question: str
            options: list
        return Q("?", [O(c.description, [c.code_formatted]) for c in items])

    def first_i42(note, question, options):
        for o in options:
            if o["codes"][0].startswith("I42.0"):
                return {"option": o["number"], "citation": "dilated cardiomyopathy"}
        return {"option": 0, "citation": ""}

    note = "Echo shows dilated cardiomyopathy."
    before = resolve(Diagnosis("dilated cardiomyopathy"), note, date_of_service=date(2026, 9, 30),
                     clarify_fn=one_per_item, answer_fn=first_i42)
    after = resolve(Diagnosis("dilated cardiomyopathy"), note, date_of_service=date(2026, 10, 1),
                    clarify_fn=one_per_item, answer_fn=first_i42)
    assert (before.code_set, before.code) == ("FY2026", "I42.0")
    assert after.code_set == "FY2027"
    assert after.code.startswith("I42.0") and after.code != "I42.0"


def test_db_path_for_unbuilt_year_explains_how_to_build(monkeypatch, tmp_path):
    monkeypatch.setattr(code_sets, "DATA_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="build_db.py --fy 2027"):
        code_sets.db_path_for(date(2026, 12, 1))
