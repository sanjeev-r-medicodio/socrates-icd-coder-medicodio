"""AI-assisted clarifying question for ambiguous search() results.

Its one model call goes through search_engine/llm.py (fast tier), so importing
this module needs no API key and a host can reroute the call with
llm.set_backend(). search_engine/core.py
stays pure, deterministic, and free to run -- this module sits strictly
downstream of it: it receives an already-narrowed candidate list from
core.search() and does exactly one narrow job -- phrase one clarifying
question whose options partition that candidate list. It never re-ranks,
never re-searches, and never picks the final code itself; the human does, by
answering the question(s).

Fires for any list of 2+ candidates, regardless of how many or how unrelated
they are -- there is no candidate-count cap and no same-parent requirement.
For a large, heterogeneous candidate set (e.g. 20 different hip conditions
spanning several unrelated ICD-10-CM chapters), an option may cover more than
one candidate code -- the model groups by whatever distinguishing question
narrows the set the most. The caller (cli.py) is expected to loop: if the
chosen option still covers more than one code, call clarify() again on just
that narrowed subset, repeating until a single code is resolved or no further
question is produced.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel

from search_engine import llm
from search_engine.core import CodeResult, DB_PATH

logger = logging.getLogger("search_engine.clarify")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_MODEL_ATTEMPTS = 2  # one try + one retry on validation failure

_SYSTEM_PROMPT = """\
You are helping a medical coder narrow down which ICD-10-CM code is correct \
for a diagnosis. You are given the full list of candidate codes with their \
descriptions -- these are already known to be the only codes under \
consideration; do not consider any code outside this list, and do not \
narrow the list yourself.

The candidates may all be closely related (e.g. differing only by laterality) \
or may span several unrelated conditions that merely matched the same search \
terms. Your only job: write exactly one clarifying question that most \
efficiently narrows down the correct code, and a set of multiple-choice \
options that partition the candidates.

Each option has a "label" (what a coder would recognize) and a "codes" list \
(one or more candidate codes it represents). Group codes into the same option \
when they share the same answer to your question -- you do not need one \
option per code. Prefer more, narrower options over cramming everything into \
one, but when the candidates are genuinely a grab-bag of unrelated \
conditions, a good first question often just separates them into their major \
categories (e.g. "which of these best describes the condition: dislocation, \
a structural/shape abnormality, contracture or stiffness, or a loose body in \
the joint?") -- a follow-up question can narrow further within a group later.

Every candidate code must appear in exactly one option's "codes" list -- \
never omitted, never duplicated across options, never invented outside the \
given list. Do not pick a final answer yourself; you are only phrasing the \
question and its options."""


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

class _ClarifyOptionModel(BaseModel):
    label: str
    codes: List[str]


class _ClarifyQuestionModel(BaseModel):
    question: str
    options: List[_ClarifyOptionModel]


@dataclass
class ClarifyOption:
    label: str
    codes: List[str]  # dotted/formatted codes, e.g. ["K57.31"] or ["Q65.30", "Q65.31", ...]


@dataclass
class ClarifyQuestion:
    question: str
    options: List[ClarifyOption]


# ---------------------------------------------------------------------------
# Enrich candidates with 7th-character meanings, for the model's context
# ---------------------------------------------------------------------------

def _seventh_char_meaning(conn: sqlite3.Connection, code: str) -> Optional[str]:
    row = conn.execute(
        "SELECT has_seventh_char, seventh_char_source_code FROM tabular_codes WHERE code = ?",
        (code,),
    ).fetchone()
    if not row or not row[0]:
        return None
    has_seventh_char, source_code = row
    last_char = code[-1]
    meaning_row = conn.execute(
        "SELECT meaning FROM seventh_char_defs WHERE code = ? AND char_value = ?",
        (source_code, last_char),
    ).fetchone()
    return meaning_row[0] if meaning_row else None


def index_main_terms(conn: sqlite3.Connection, code: str, limit: int = 3) -> List[str]:
    """Returns up to `limit` breadcrumb strings from the Alphabetical Index
    for this code (e.g. "Otitis, media, acute, suppurative") -- the
    coding-vocabulary phrasing a human coder would actually search under,
    which is often closer to how a note is written than the Tabular's formal
    title. Multiple Index entries can point to the same code (synonyms,
    cross-listed terms); each becomes its own breadcrumb, deduplicated."""
    rows = conn.execute("SELECT id FROM index_terms WHERE code = ?", (code,)).fetchall()
    breadcrumbs: List[str] = []
    seen = set()
    for (term_id,) in rows:
        chain = []
        current_id = term_id
        while current_id is not None:
            row = conn.execute(
                "SELECT term_text, parent_id FROM index_terms WHERE id = ?", (current_id,)
            ).fetchone()
            if row is None:
                break
            chain.append(row[0])
            current_id = row[1]
        breadcrumb = ", ".join(reversed(chain))
        if breadcrumb and breadcrumb not in seen:
            seen.add(breadcrumb)
            breadcrumbs.append(breadcrumb)
        if len(breadcrumbs) >= limit:
            break
    return breadcrumbs


def _build_candidate_payload(conn: sqlite3.Connection, candidates: List[CodeResult]) -> List[dict]:
    payload = []
    for c in candidates:
        entry = {
            "code": c.code_formatted,
            "description": c.description,
        }
        if c.short_title and c.short_title != c.description:
            entry["short_title"] = c.short_title
        meaning = _seventh_char_meaning(conn, c.code)
        if meaning:
            entry["seventh_character_meaning"] = meaning
        terms = index_main_terms(conn, c.code)
        if terms:
            entry["index_terms"] = terms
        payload.append(entry)
    return payload


# ---------------------------------------------------------------------------
# Model call (isolated so tests can substitute it without hitting the network)
# ---------------------------------------------------------------------------

def _call_model(candidate_payload: List[dict]) -> dict:
    # Fast tier (CLAUDE_MODEL_FAST by default); a host can reroute it with
    # llm.set_backend(). Nothing is read from the environment until here.
    return llm.parse(
        tier="fast",
        system=_SYSTEM_PROMPT,
        user=f"Candidate codes:\n{json.dumps(candidate_payload, indent=2)}",
        schema=_ClarifyQuestionModel,
    )


# ---------------------------------------------------------------------------
# Validation: never let an unvalidated model response reach the user
# ---------------------------------------------------------------------------

def _validate(raw: dict, candidate_payload: List[dict]) -> tuple[bool, str]:
    if not isinstance(raw, dict):
        return False, f"response is not a dict: {raw!r}"

    question = raw.get("question")
    options = raw.get("options")

    if not isinstance(question, str) or not question.strip():
        return False, "missing or empty 'question'"
    if not isinstance(options, list) or not options:
        return False, "missing or empty 'options'"

    candidate_codes = {c["code"] for c in candidate_payload}
    seen = set()
    for opt in options:
        if not isinstance(opt, dict) or "codes" not in opt or "label" not in opt:
            return False, f"malformed option: {opt!r}"
        codes = opt["codes"]
        if not isinstance(codes, list) or not codes:
            return False, f"option has no codes: {opt!r}"
        for code in codes:
            if code not in candidate_codes:
                return False, f"option code {code!r} is not one of the candidate codes"
            if code in seen:
                return False, f"duplicate option for code {code!r}"
            seen.add(code)

    missing = candidate_codes - seen
    if missing:
        return False, f"missing options for candidate codes: {sorted(missing)}"

    return True, "ok"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def clarify(
    candidates: List[CodeResult],
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
    call_model: Callable[[List[dict]], dict] = _call_model,
) -> Optional[ClarifyQuestion]:
    """Returns a ClarifyQuestion partitioning `candidates`, or None if there's
    nothing to clarify (fewer than 2 candidates) or the model's response never
    validates (caller should fall back to printing the plain list)."""
    if len(candidates) < 2:
        logger.info("clarify: skipped, only %d candidate(s)", len(candidates))
        return None

    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)

    try:
        candidate_payload = _build_candidate_payload(conn, candidates)
        logger.info("clarify: input candidates: %s", json.dumps(candidate_payload))

        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            try:
                raw = call_model(candidate_payload)
            except Exception:
                logger.exception("clarify: model call failed on attempt %d", attempt)
                return None

            logger.info("clarify: raw model response (attempt %d): %s", attempt, json.dumps(raw))

            ok, detail = _validate(raw, candidate_payload)
            logger.info("clarify: validation result (attempt %d): %s -- %s", attempt, ok, detail)

            if ok:
                question = ClarifyQuestion(
                    question=raw["question"],
                    options=[ClarifyOption(label=o["label"], codes=list(o["codes"])) for o in raw["options"]],
                )
                logger.info("clarify: resolved question with %d options", len(question.options))
                return question

        logger.warning("clarify: all %d attempt(s) failed validation, falling back to plain list", MAX_MODEL_ATTEMPTS)
        return None
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Pluggable cascade runner
#
# cli.py's interactive loop implements this same round-narrowing shape itself
# (human answers via stdin) and is deliberately left as its own
# implementation, unchanged. This is the shared version for any *other*
# answer source -- currently note_answerer.py's AI-answered cascade -- so
# that answer source doesn't need its own copy of the narrowing/termination
# logic. It only calls the public clarify() above; it does not touch
# question-generation or validation.
# ---------------------------------------------------------------------------

DEFAULT_MAX_CASCADE_ROUNDS = 5


@dataclass
class AnswerResult:
    """What an answer source returns for one clarify() question.

    resolved=False means "not specified" -- the answer source could not
    determine which option applies (e.g. note_answerer found the note simply
    doesn't address the distinguishing factor). option_index/supporting_quote
    are only meaningful when resolved=True.
    """
    resolved: bool
    option_index: Optional[int] = None
    supporting_quote: Optional[str] = None


@dataclass
class CascadeStep:
    question: ClarifyQuestion
    answer: AnswerResult
    narrowed_to: List[str]  # formatted codes remaining after this step


@dataclass
class CascadeResult:
    resolved_code: Optional[str]  # formatted code, set only if narrowed to exactly one
    remaining_candidates: List[CodeResult]  # the candidates left when the cascade stopped
    steps: List[CascadeStep]
    unanswered_question: Optional[ClarifyQuestion]  # set only if an answer source returned "not specified"


# answer_source(question, current_candidates) -> AnswerResult
AnswerSource = Callable[[ClarifyQuestion, List[CodeResult]], AnswerResult]


def run_cascade(
    candidates: List[CodeResult],
    answer_source: AnswerSource,
    db_path: Path = DB_PATH,
    conn: Optional[sqlite3.Connection] = None,
    max_rounds: int = DEFAULT_MAX_CASCADE_ROUNDS,
) -> CascadeResult:
    """Repeatedly calls clarify() and answer_source() to narrow `candidates`
    down to a single code. Stops when: narrowed to one code (resolved_code
    set); clarify() returns no question (nothing left to ask); answer_source
    returns resolved=False (unanswered_question set); the chosen option
    doesn't actually narrow anything (guards a degenerate question); or
    max_rounds is hit. Same termination shape as cli.py's own loop."""
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)

    try:
        current = candidates
        steps: List[CascadeStep] = []

        for round_num in range(1, max_rounds + 1):
            if len(current) <= 1:
                resolved_code = current[0].code_formatted if current else None
                return CascadeResult(resolved_code, current, steps, None)

            question = clarify(current, conn=conn)
            if question is None:
                return CascadeResult(None, current, steps, None)

            answer = answer_source(question, current)
            if not answer.resolved:
                return CascadeResult(None, current, steps, question)

            by_code = {r.code_formatted: r for r in current}
            chosen = question.options[answer.option_index]
            narrowed = [by_code[c] for c in chosen.codes if c in by_code]

            steps.append(CascadeStep(question, answer, [r.code_formatted for r in narrowed]))

            if len(narrowed) == 1:
                return CascadeResult(narrowed[0].code_formatted, narrowed, steps, None)
            if not narrowed or len(narrowed) >= len(current):
                # degenerate: the chosen option didn't narrow anything
                return CascadeResult(None, narrowed or current, steps, None)

            current = narrowed

        return CascadeResult(None, current, steps, None)
    finally:
        if owns_conn:
            conn.close()
