"""Resolve one extracted diagnosis to one billable ICD-10-CM code, from a note.

This is the loop an upstream pipeline calls once per diagnosis it has already
extracted (phrase + optional laterality / encounter / anatomical location):

    search() -> [stage by category when > MAX_CLARIFY_CANDIDATES]
             -> clarify() -> answer from the note -> narrow -> repeat

It differs from note_agent.resolve_diagnosis() (the demo's single-diagnosis
loop, left unchanged) in four ways:

- No count cut-off: every candidate scoring within CANDIDATE_SCORE_FLOOR of
  the top is kept, and a large set is narrowed a category at a time instead
  of being cut to the top 20 before any question is asked.
- The answerer may say the note does not document the distinction. The loop
  then falls back to the group's unspecified code, or, when only the 7th
  character is undecided, reports it as unresolved rather than guessing.
- Every citation is checked against the note text.
- The result carries a confidence score derived from how it was reached.

Model calls are injectable (``clarify_fn`` / ``answer_fn``) so tests and host
pipelines can supply their own. Each call opens its own read-only SQLite
connection, so concurrent calls from worker threads are safe.
"""
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel

from search_engine import code_sets
from search_engine.core import DB_PATH, CodeResult, search

logger = logging.getLogger("search_engine.resolver")

MAX_CLARIFY_CANDIDATES = 20   # above this, ask at category level first
CANDIDATE_SCORE_FLOOR = 0.5   # keep candidates scoring >= this fraction of the top score
MAX_ROUNDS = 8
MAX_ANSWER_ATTEMPTS = 2       # one try + one retry on an invalid/unverified answer
NOT_DOCUMENTED = 0            # option number the answerer returns for "note doesn't say"

# Confidence is a transparent heuristic, not a calibrated probability.
CONFIDENCE_SINGLE_SEARCH_RESULT = 0.9   # search alone was unambiguous
CONFIDENCE_UNVERIFIED_CITATION = 0.3    # subtracted per answer whose quote isn't in the note
CONFIDENCE_UNSPECIFIED_DEFAULT = 0.5    # cap when a "not documented" answer picked the unspecified code

UNSPECIFIED_RE = re.compile(r"\bunspecified\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    phrase: str
    laterality: Optional[str] = None           # 'left' | 'right' | 'bilateral'
    encounter: Optional[str] = None            # 'initial' | 'subsequent' | 'sequela'
    anatomical_location: Optional[str] = None


@dataclass
class Round:
    question: str
    options: List[dict]                        # [{"number", "label", "codes"}]
    chosen: int                                # option number, 0 = not documented
    citation: str
    citation_verified: bool
    staged: bool                               # True if options were categories, not codes
    narrowed_to: int                           # candidates left after this round


@dataclass
class Resolution:
    status: str                                # resolved | unspecified_default | seventh_char_unresolved | unresolved | no_candidates
    code: Optional[str] = None                 # dotted billable code, e.g. "S72.001A"
    description: Optional[str] = None
    confidence: float = 0.0
    query: str = ""
    rounds: List[Round] = field(default_factory=list)
    remaining: List[str] = field(default_factory=list)   # dotted codes left when not resolved
    candidates_considered: int = 0
    code_set: Optional[str] = None             # e.g. "FY2027"

    @property
    def seventh_character_unresolved(self) -> bool:
        return self.status == "seventh_char_unresolved"


# ---------------------------------------------------------------------------
# Answering from the note
# ---------------------------------------------------------------------------

_ANSWER_SYSTEM_PROMPT = """\
You are answering one multiple-choice question for a medical coder, using \
only the clinical note provided. Each numbered option describes one or more \
ICD-10-CM codes.

Pick the option the note documents. If the note does not state the fact the \
question asks about, answer 0 (not documented) -- do not choose the \
statistically likely option, and do not infer from what is typical for a \
case like this. Recognize synonyms, abbreviations and facts stated \
indirectly, but never invent a fact the note doesn't contain.

Give the exact quote from the note (copied character for character) that \
supports your answer. For 0, quote the part of the note that describes the \
condition, or leave the citation empty."""


class _AnswerModel(BaseModel):
    option: int
    citation: str


def build_answer_prompt(note: str, question: str, options: List[dict]) -> str:
    lines = [f"Clinical note:\n\n{note}\n", f"Question: {question}\n", "Options:",
             "0. Not documented in the note"]
    for opt in options:
        lines.append(f"{opt['number']}. {opt['label']}")
    return "\n".join(lines)


def default_answer_fn(note: str, question: str, options: List[dict]) -> dict:
    """Anthropic-backed answerer. Returns {"option": int, "citation": str}."""
    from search_engine import llm
    return llm.parse(
        tier="reasoning",
        system=_ANSWER_SYSTEM_PROMPT,
        user=build_answer_prompt(note, question, options),
        schema=_AnswerModel,
    )


def default_clarify_fn(candidates: List[CodeResult], conn: sqlite3.Connection):
    from search_engine.clarify import clarify
    return clarify(candidates, conn=conn)


def citation_in_note(citation: str, note: str) -> bool:
    """Whitespace- and case-insensitive containment. An empty citation is
    never verified."""
    def norm(t):
        return re.sub(r"\s+", " ", t).strip().lower()
    c = norm(citation).strip('"').strip("'")
    return bool(c) and c in norm(note)


# ---------------------------------------------------------------------------
# Staging: collapse a large candidate set into category-level options
# ---------------------------------------------------------------------------

def _group_prefix_len(codes: List[str]) -> int:
    """Shortest code-prefix length (from 3) that splits the set into >= 2 groups."""
    for n in range(3, 8):
        if len({c[:n] for c in codes}) >= 2:
            return n
    return 7


def stage_candidates(conn, candidates: List[CodeResult]) -> tuple:
    """Returns (group_items, members) where group_items are CodeResults for
    category headers (e.g. K57 'Diverticular disease of intestine') and
    members maps each group's formatted code to its candidates. Groups are
    ranked by their best member's score; at most MAX_CLARIFY_CANDIDATES are
    kept, and the rest (lowest-scoring) are dropped with a log line."""
    n = _group_prefix_len([c.code for c in candidates])
    groups: dict = {}
    for c in candidates:
        groups.setdefault(c.code[:n], []).append(c)
    ranked = sorted(groups.items(), key=lambda kv: -max(m.score for m in kv[1]))
    if len(ranked) > MAX_CLARIFY_CANDIDATES:
        dropped = ranked[MAX_CLARIFY_CANDIDATES:]
        logger.info("resolver: staging kept %d of %d groups (dropped %s)", MAX_CLARIFY_CANDIDATES,
                    len(ranked), [k for k, _ in dropped][:10])
        ranked = ranked[:MAX_CLARIFY_CANDIDATES]
    items, members = [], {}
    for prefix, group in ranked:
        row = conn.execute(
            "SELECT code_formatted, long_desc, short_title FROM tabular_codes WHERE code = ?", (prefix,)
        ).fetchone()
        fmt = row[0] if row and row[0] else group[0].code_formatted[: n + (1 if n > 3 else 0)]
        items.append(CodeResult(
            code=prefix, code_formatted=fmt,
            description=(row[1] if row else group[0].description) or "",
            short_title=row[2] if row else None,
            score=max(m.score for m in group),
        ))
        members[fmt] = group
    return items, members


# ---------------------------------------------------------------------------
# Resolution loop
# ---------------------------------------------------------------------------

def _build_query(dx: Diagnosis) -> str:
    phrase = dx.phrase.strip()
    loc = (dx.anatomical_location or "").strip()
    if loc and loc.lower() not in phrase.lower():
        return f"{phrase} {loc}"
    return phrase


def _only_seventh_char_differs(candidates: List[CodeResult]) -> bool:
    return (
        len(candidates) > 1
        and all(len(c.code) == 7 for c in candidates)
        and len({c.code[:6] for c in candidates}) == 1
    )


def _unspecified_default(candidates: List[CodeResult]) -> Optional[CodeResult]:
    """The highest-ranked candidate whose title says 'unspecified', but only
    when every candidate is in the same 3-character category -- defaulting
    across unrelated families (M25.559 vs R52) would be a guess."""
    if len({c.code[:3] for c in candidates}) != 1:
        return None
    for c in candidates:   # already sorted by score
        if UNSPECIFIED_RE.search(c.description or ""):
            return c
    return None


def resolve(
    dx: Diagnosis,
    note: str,
    *,
    date_of_service: Optional[date] = None,
    db_path: Optional[Path] = None,
    clarify_fn: Callable = None,
    answer_fn: Callable = None,
) -> Resolution:
    """`date_of_service` picks the fiscal-year code set in effect that day
    (raises code_sets.UnsupportedDateOfService outside the loaded years).
    `db_path` overrides it; with neither, today's code set is used."""
    clarify_fn = clarify_fn or default_clarify_fn
    answer_fn = answer_fn or default_answer_fn
    query = _build_query(dx)
    if db_path is None:
        db_path = code_sets.db_path_for(date_of_service) if date_of_service else DB_PATH

    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        result = search(query, conn=conn, laterality=dx.laterality, encounter=dx.encounter,
                        max_results=None)
        candidates = result.results
        if candidates:
            floor = candidates[0].score * CANDIDATE_SCORE_FLOOR
            candidates = [c for c in candidates if c.score >= floor]
        res = Resolution(status="unresolved", query=query, candidates_considered=len(candidates),
                         code_set=_code_set_label(conn))
        if not candidates:
            res.status = "no_candidates"
            return res
        if result.is_single or len(candidates) == 1:
            return _finish(res, candidates[0], CONFIDENCE_SINGLE_SEARCH_RESULT, "resolved")

        penalty = 0.0
        for _ in range(MAX_ROUNDS):
            staged = len(candidates) > MAX_CLARIFY_CANDIDATES
            if staged:
                items, members = stage_candidates(conn, candidates)
            else:
                items, members = candidates, {c.code_formatted: [c] for c in candidates}

            question = clarify_fn(items, conn)
            if question is None:
                logger.warning("resolver: clarify produced no valid question for %d items", len(items))
                break
            options = [{"number": i, "label": o.label, "codes": list(o.codes)}
                       for i, o in enumerate(question.options, 1)]

            chosen, citation, verified = _ask(answer_fn, note, question.question, options)
            if not verified:
                penalty += CONFIDENCE_UNVERIFIED_CITATION

            if chosen == NOT_DOCUMENTED:
                res.rounds.append(Round(question.question, options, chosen, citation, verified, staged,
                                        len(candidates)))
                if _only_seventh_char_differs(candidates):
                    res.status = "seventh_char_unresolved"
                    res.remaining = [c.code_formatted for c in candidates]
                    return res
                # At category level there's no single group to default within.
                default = None if staged else _unspecified_default(candidates)
                if default is None:
                    res.remaining = [c.code_formatted for c in candidates]
                    return res
                return _finish(res, default, min(1.0 - penalty, CONFIDENCE_UNSPECIFIED_DEFAULT),
                               "unspecified_default")

            opt = options[chosen - 1]
            narrowed = [m for fmt in opt["codes"] for m in members.get(fmt, [])]
            narrowed.sort(key=lambda c: -c.score)
            res.rounds.append(Round(question.question, options, chosen, citation, verified, staged,
                                    len(narrowed)))
            if len(narrowed) == 1:
                return _finish(res, narrowed[0], 1.0 - penalty, "resolved")
            if not narrowed or len(narrowed) >= len(candidates):
                logger.warning("resolver: option %r did not narrow %d candidates", opt["label"], len(candidates))
                break
            candidates = narrowed

        res.remaining = [c.code_formatted for c in candidates]
        return res
    finally:
        conn.close()


def _code_set_label(conn) -> Optional[str]:
    row = conn.execute("SELECT fiscal_year FROM code_set_info").fetchone()
    return f"FY{row[0]}" if row else None


def _ask(answer_fn, note, question, options):
    """Returns (option_number, citation, citation_verified). Retries once on
    an out-of-range option or an unverifiable citation; a second unverified
    citation is accepted but flagged. A second out-of-range option is
    treated as not documented."""
    valid = {NOT_DOCUMENTED} | {o["number"] for o in options}
    last = None
    for attempt in range(1, MAX_ANSWER_ATTEMPTS + 1):
        raw = answer_fn(note, question, options)
        chosen, citation = int(raw.get("option", -1)), (raw.get("citation") or "").strip()
        if chosen not in valid:
            logger.warning("resolver: answer %r not a valid option (attempt %d)", chosen, attempt)
            continue
        verified = chosen == NOT_DOCUMENTED or citation_in_note(citation, note)
        last = (chosen, citation, verified)
        if verified:
            return last
        logger.warning("resolver: citation not found in note (attempt %d): %r", attempt, citation)
    return last or (NOT_DOCUMENTED, "", True)


def _finish(res: Resolution, code: CodeResult, confidence: float, status: str) -> Resolution:
    res.status = status
    res.code = code.code_formatted
    res.description = code.description
    res.confidence = round(max(0.0, min(1.0, confidence)), 2)
    return res
