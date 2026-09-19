"""Minimal single-diagnosis note agent: read a note, seed the search engine,
answer clarify()'s questions from the note.

Two methods, one shared grounding instruction. This is deliberately narrow --
one note, one diagnosis, no "not specified" path, no note reorganization. See
the project plan for the full scope and non-goals of this rebuild.

Uses CLAUDE_MODEL_REASONING for both methods -- interpreting what a note
actually states (vs. inventing something plausible) is real clinical-language
judgment, not pattern matching.
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel

from search_engine._env import load_dotenv, require_env
from search_engine.clarify import ClarifyOption, clarify
from search_engine.core import CodeResult, search

logger = logging.getLogger("search_engine.note_agent")

load_dotenv()

ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY", "search_engine.note_agent")
CLAUDE_MODEL_REASONING = require_env("CLAUDE_MODEL_REASONING", "search_engine.note_agent")

import anthropic  # noqa: E402

_client: Optional["anthropic.Anthropic"] = None


def _get_client() -> "anthropic.Anthropic":
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Shared grounding instruction -- written once, used by both methods below.
# ---------------------------------------------------------------------------

GROUNDING_INSTRUCTION = """\
Use real language understanding to interpret what the note actually states: \
recognize synonyms, abbreviations, and facts that are stated indirectly. \
Never invent or infer something the note doesn't say -- match what's \
genuinely written, not the statistically likely answer for a case like this."""


# ---------------------------------------------------------------------------
# extract_main_term
# ---------------------------------------------------------------------------

_MAIN_TERM_SYSTEM_PROMPT = f"""\
You read a clinical visit note and identify the single diagnosis it \
documents, phrased as a diagnostic term or phrase suitable for searching an \
ICD-10-CM index -- not the full sentence around it, not a code.

"Main term" does NOT mean the single shortest or most generic word for the \
condition -- it means the condition plus every qualifier the note states \
that distinguishes it from other forms of the same condition: laterality \
(left/right/bilateral), site, acuity (acute/chronic), and any stated \
with/without-complication status (e.g. "with bleeding", "without \
perforation"). Dropping a qualifier the note actually states makes the \
search far less useful, even though the bare word alone is technically also \
"a diagnosis". For example, from a note that documents "pain in right hip", \
return "pain in right hip" -- NOT "pain" or "hip pain" with the laterality \
dropped. From a note documenting "acute diverticulitis with bleeding", \
return "acute diverticulitis with bleeding" -- not just "diverticulitis".

{GROUNDING_INSTRUCTION}

The note documents exactly one diagnosis to code. Return that diagnosis's \
main term, including every qualifier above that the note actually states."""


class _MainTermModel(BaseModel):
    main_term: str


def _call_model_main_term(note: str) -> dict:
    # CLAUDE_MODEL_REASONING does adaptive thinking by default, and thinking
    # tokens count against max_tokens -- a clinically dense note can burn the
    # entire budget on reasoning before any output text exists, leaving
    # parsed_output None (stop_reason "max_tokens", a "thinking" block and no
    # text block). The actual JSON is tiny; the headroom below is for
    # thinking, not the output itself.
    response = _get_client().messages.parse(
        model=CLAUDE_MODEL_REASONING,
        max_tokens=4096,
        system=_MAIN_TERM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Visit note:\n\n{note}"}],
        output_format=_MainTermModel,
    )
    if response.parsed_output is None:
        raise RuntimeError(
            f"extract_main_term: model response had no parsed output "
            f"(stop_reason={response.stop_reason!r}) -- likely ran out of "
            f"max_tokens mid-thinking; see clarify.log."
        )
    return response.parsed_output.model_dump()


def extract_main_term(note: str, call_model: Callable[[str], dict] = _call_model_main_term) -> str:
    raw = call_model(note)
    main_term = raw["main_term"].strip()
    logger.info("note_agent: extract_main_term -> %r", main_term)
    return main_term


# ---------------------------------------------------------------------------
# answer_question
# ---------------------------------------------------------------------------

_ANSWER_SYSTEM_PROMPT = f"""\
You are answering one multiple-choice clarifying question on behalf of a \
medical coder, using the visit note below. You are given the question and \
its answer options -- each option has a label describing what it represents \
and one or more ICD-10-CM codes it covers.

Determine which option the note documents, and return exactly one code from \
that option's code list. If the matching option covers more than one code, \
any code from it is an acceptable answer -- a later, more specific question \
will distinguish between them; your only job right now is identifying the \
correct option.

The note fully documents this diagnosis: there is always a correct option to \
select. You must select one -- do not decline to answer.

{GROUNDING_INSTRUCTION}

Also give the exact verbatim quote from the note (character-for-character, \
not paraphrased) that supports your choice."""


class _AnswerModel(BaseModel):
    selected_code: str
    citation: str


@dataclass
class NoteAnswer:
    selected_code: str
    citation: str


def _build_answer_prompt(note: str, question: str, options: List[ClarifyOption]) -> str:
    lines = [f"Visit note:\n\n{note}\n", f"Question: {question}\n", "Options:"]
    for i, opt in enumerate(options, 1):
        lines.append(f"{i}. {opt.label}  (codes: {', '.join(opt.codes)})")
    return "\n".join(lines)


def _call_model_answer(note: str, question: str, options: List[ClarifyOption]) -> dict:
    # See _call_model_main_term: same reasoning model, same risk of thinking
    # tokens exhausting max_tokens before any output text exists.
    response = _get_client().messages.parse(
        model=CLAUDE_MODEL_REASONING,
        max_tokens=4096,
        system=_ANSWER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_answer_prompt(note, question, options)}],
        output_format=_AnswerModel,
    )
    if response.parsed_output is None:
        raise RuntimeError(
            f"answer_question: model response had no parsed output "
            f"(stop_reason={response.stop_reason!r}) -- likely ran out of "
            f"max_tokens mid-thinking; see clarify.log."
        )
    return response.parsed_output.model_dump()


def answer_question(
    note: str,
    question: str,
    options: List[ClarifyOption],
    call_model: Callable[[str, str, List[ClarifyOption]], dict] = _call_model_answer,
) -> NoteAnswer:
    raw = call_model(note, question, options)
    result = NoteAnswer(selected_code=raw["selected_code"].strip(), citation=raw["citation"].strip())
    logger.info("note_agent: answer_question -> code=%r citation=%r", result.selected_code, result.citation)
    return result


# ---------------------------------------------------------------------------
# resolve_diagnosis -- the full note -> code loop, shared by cli_diagnosis.py
# (live CLI progress via the optional callbacks) and api.py's
# POST /api/code-from-note (no callbacks, just the returned tuple, since the
# whole loop runs server-side in one request with no client interaction per
# round).
# ---------------------------------------------------------------------------

MAX_ROUNDS = 8  # safety cap against an infinite loop bug, not a "not specified" mechanism


class DiagnosisResolutionError(RuntimeError):
    """Raised when the loop cannot make progress -- a bug to surface loudly."""


def resolve_diagnosis(
    note: str,
    on_status: Optional[Callable[[str], None]] = None,
    on_question: Optional[Callable[[int, str], None]] = None,
    on_round: Optional[Callable[[dict], None]] = None,
):
    """Runs extract_main_term -> core.search() -> clarify()/answer_question()
    until a single code is reached. Three optional callbacks, all for a
    live-streaming caller like cli_diagnosis.py (api.py passes none of them,
    it only needs the returned tuple once the whole loop finishes):
    `on_status` gets brief ephemeral progress strings; `on_question` fires as
    soon as a round's question is generated, with (round_num, question_text);
    `on_round` fires once that round is fully answered, with its dict
    (question, chosen_label, selected_code, citation, narrowed_to). Returns
    (main_term, rounds, final_result). Raises DiagnosisResolutionError if the
    loop can't converge."""

    def status(message: str):
        if on_status:
            on_status(message)

    status("Extracting main diagnosis term...")
    main_term = extract_main_term(note)

    status(f"Searching for {main_term!r}...")
    result = search(main_term)
    if not result.results:
        raise DiagnosisResolutionError(
            f"core.search({main_term!r}) returned no candidates at all -- "
            "nothing for clarify() to work with."
        )

    candidates = result.results
    rounds = []

    if len(candidates) == 1:
        return main_term, rounds, candidates[0]

    for round_num in range(1, MAX_ROUNDS + 1):
        status(f"Round {round_num}: generating clarifying question...")
        question = clarify(candidates)
        if question is None:
            raise DiagnosisResolutionError(
                f"clarify() could not produce a valid question for {len(candidates)} "
                f"candidates on round {round_num} (see clarify.log)."
            )

        if on_question:
            on_question(round_num, question.question)

        valid_codes = {code for opt in question.options for code in opt.codes}

        status(f"Round {round_num}: answering from the note...")
        answer = None
        for attempt in (1, 2):
            candidate_answer = answer_question(note, question.question, question.options)
            if candidate_answer.selected_code in valid_codes:
                answer = candidate_answer
                break
            logger.warning(
                "round %d attempt %d: note_agent returned %r, not one of this round's "
                "candidate codes %s",
                round_num, attempt, candidate_answer.selected_code, sorted(valid_codes),
            )
            if attempt == 1:
                status(f"Round {round_num}: invalid answer, retrying...")
        if answer is None:
            raise DiagnosisResolutionError(
                f"note_agent.answer_question() returned a code that wasn't one of this "
                f"round's candidates, twice in a row, for question: {question.question!r}"
            )

        chosen_option = next(opt for opt in question.options if answer.selected_code in opt.codes)
        narrowed_code_set = set(chosen_option.codes)
        narrowed = [c for c in candidates if c.code_formatted in narrowed_code_set]

        round_info = {
            "question": question.question,
            "chosen_label": chosen_option.label,
            "selected_code": answer.selected_code,
            "citation": answer.citation,
            "narrowed_to": [c.code_formatted for c in narrowed],
        }
        rounds.append(round_info)
        if on_round:
            on_round(round_info)

        if len(narrowed) == 1:
            return main_term, rounds, narrowed[0]

        if not narrowed or len(narrowed) >= len(candidates):
            raise DiagnosisResolutionError(
                f"round {round_num}: chosen option {chosen_option.label!r} did not "
                f"narrow the candidate set ({len(candidates)} -> {len(narrowed)}) -- "
                "this is a bug, not a 'not specified' case."
            )

        candidates = narrowed

    raise DiagnosisResolutionError(
        f"did not converge to a single code within {MAX_ROUNDS} rounds -- "
        f"{len(candidates)} candidate(s) remain: {[c.code_formatted for c in candidates]}"
    )
