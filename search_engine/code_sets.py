"""Which ICD-10-CM code set applies to a date of service.

ICD-10-CM is released per federal fiscal year: FY N is in effect from
October 1 of year N-1 through September 30 of year N. A claim must use the
codes in effect on the date of service, so each fiscal year is built into its
own database (data/icd10_fy{N}.db) and callers pick one by date.

Only the October 1 releases are modelled. A mid-year (April 1) update would
be a new CodeSet entry with its own effective dates and source files.
"""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"


@dataclass(frozen=True)
class CodeSet:
    fiscal_year: int
    effective_from: date
    effective_to: date

    @property
    def db_path(self) -> Path:
        return DATA_DIR / f"icd10_fy{self.fiscal_year}.db"

    @property
    def tabular_xml(self) -> Path:
        return RAW_DIR / f"icd10cm_tabular_{self.fiscal_year}.xml"

    @property
    def index_xml(self) -> Path:
        return RAW_DIR / f"icd10cm_index_{self.fiscal_year}.xml"

    @property
    def order_file(self) -> Path:
        return RAW_DIR / f"icd10cm_order_{self.fiscal_year}.txt"

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to


def _fiscal_year(fy: int) -> CodeSet:
    return CodeSet(fy, date(fy - 1, 10, 1), date(fy, 9, 30))


CODE_SETS = [_fiscal_year(2026), _fiscal_year(2027)]
LATEST = CODE_SETS[-1]


class UnsupportedDateOfService(ValueError):
    pass


def for_date(day: date) -> CodeSet:
    for cs in CODE_SETS:
        if cs.covers(day):
            return cs
    supported = f"{CODE_SETS[0].effective_from} to {CODE_SETS[-1].effective_to}"
    raise UnsupportedDateOfService(f"no ICD-10-CM code set loaded for {day} (supported: {supported})")


def db_path_for(day: Optional[date] = None) -> Path:
    """Database for the code set in effect on `day` (default: today).

    Raises UnsupportedDateOfService outside the loaded years, and
    FileNotFoundError if that year's database hasn't been built."""
    cs = for_date(day or date.today())
    if not cs.db_path.exists():
        raise FileNotFoundError(f"{cs.db_path} not built -- run `python scripts/build_db.py --fy {cs.fiscal_year}`")
    return cs.db_path


def default_db_path() -> Path:
    """Today's code set if it's built, else the latest one that is.

    Used as the default for callers that don't pass a date of service
    (the CLI, the demo web app)."""
    try:
        return db_path_for()
    except (UnsupportedDateOfService, FileNotFoundError):
        for cs in reversed(CODE_SETS):
            if cs.db_path.exists():
                return cs.db_path
        return LATEST.db_path
