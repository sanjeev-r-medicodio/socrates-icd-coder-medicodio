#!/usr/bin/env python3
"""Thin FastAPI layer over search_engine/{core,clarify,note_agent}.py -- zero
internal logic here, just request/response shaping. See README for the full
endpoint contracts.

The server is fully stateless between calls: no session or conversation id.
For the search-diagnosis flow, the client only ever sends along the codes
from the option it picked (`selected_codes`); the server re-derives the next
round from that alone (re-fetching those codes' CodeResult rows and calling
clarify() again) -- it never remembers a prior round's question, labels, or
round count. That bookkeeping (the on-screen trace / round number) is owned
client-side, using data the client already rendered from earlier responses.

POST /api/code-from-note is different: note_agent.resolve_diagnosis() runs
its entire note -> code loop in one call, so its response carries the full
trace (with citations) since the client never sees intermediate state.
POST /api/code-from-note/stream runs the same loop but streams progress as
it happens (status/question/round/resolved events, one JSON object per SSE
"data:" line) -- resolve_diagnosis() already exposes on_status/on_question/
on_round callbacks for exactly this, built for cli_diagnosis.py's live
terminal output; this endpoint just forwards them to the browser instead of
stdout. resolve_diagnosis() itself is synchronous (blocking Anthropic SDK
calls), so it runs on a background thread that pushes callback events onto a
queue.Queue while the generator yields them out to the response.

Run: uvicorn api:app --reload --port 8000
"""
import os
import platform
import sys


def _ensure_expat_fix():
    """See cli.py -- same Homebrew/libexpat workaround, needed here too since
    this process also constructs Anthropic clients (clarify, note_agent)."""
    if sys.platform != "darwin" or platform.mac_ver()[0]:
        return
    expat_lib = "/opt/homebrew/opt/expat/lib"
    if not os.path.isdir(expat_lib):
        return
    current = os.environ.get("DYLD_LIBRARY_PATH", "")
    if expat_lib in current.split(":"):
        return
    os.environ["DYLD_LIBRARY_PATH"] = f"{expat_lib}:{current}" if current else expat_lib
    os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_expat_fix()

import json
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    filename=Path(__file__).resolve().parent / "clarify.log",
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from search_engine.clarify import DB_PATH, clarify
from search_engine.core import CodeResult, _fetch_code_display, search
from search_engine.note_agent import DiagnosisResolutionError, resolve_diagnosis

app = FastAPI(title="ICD-10-CM diagnosis coding agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response shapes
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str


class AnswerRequest(BaseModel):
    selected_codes: List[str]


class CodeFromNoteRequest(BaseModel):
    note: str


class ClarifyOptionResponse(BaseModel):
    label: str
    codes: List[str]


class SingleResponse(BaseModel):
    type: str = "single"
    code: str
    description: str
    short_title: Optional[str]


class ClarifyResponse(BaseModel):
    type: str = "clarify"
    question: str
    options: List[ClarifyOptionResponse]


class TraceStepResponse(BaseModel):
    summary: str
    citation: str


class CodeFromNoteResponse(BaseModel):
    code: str
    description: str
    short_title: Optional[str]
    trace: List[TraceStepResponse]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _single_or_clarify(candidates: List[CodeResult]):
    """Shared shaping for /api/search and /api/answer: one candidate -> a
    SingleResponse; 2+ -> run clarify() and return a ClarifyResponse."""
    if len(candidates) == 1:
        c = candidates[0]
        return SingleResponse(code=c.code_formatted, description=c.description, short_title=c.short_title)

    question = clarify(candidates)
    if question is None:
        raise HTTPException(
            status_code=500,
            detail=f"clarify() could not produce a valid question for {len(candidates)} candidates (see clarify.log).",
        )
    return ClarifyResponse(
        question=question.question,
        options=[ClarifyOptionResponse(label=o.label, codes=o.codes) for o in question.options],
    )


def _code_results_from_codes(conn: sqlite3.Connection, codes: List[str]) -> List[CodeResult]:
    """Reconstructs CodeResult rows for a set of formatted codes the client
    sent back (e.g. the codes from the clarify option it picked). score/reasons
    are placeholders -- clarify() doesn't use them, only code/description."""
    results = []
    for code_formatted in codes:
        row = conn.execute(
            "SELECT code FROM tabular_codes WHERE code_formatted = ?", (code_formatted,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown code: {code_formatted!r}")
        display = _fetch_code_display(conn, row[0])
        results.append(CodeResult(
            code=row[0],
            code_formatted=display["code_formatted"],
            description=display["long_desc"],
            short_title=display["short_title"],
            score=0.0,
            reasons=[],
        ))
    return results


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/search")
def api_search(req: SearchRequest):
    result = search(req.query)
    if not result.results:
        raise HTTPException(status_code=404, detail=f"No candidates found for {req.query!r}.")
    return _single_or_clarify(result.results)


@app.post("/api/answer")
def api_answer(req: AnswerRequest):
    if not req.selected_codes:
        raise HTTPException(status_code=400, detail="selected_codes must not be empty.")
    conn = sqlite3.connect(DB_PATH)
    try:
        candidates = _code_results_from_codes(conn, req.selected_codes)
    finally:
        conn.close()
    return _single_or_clarify(candidates)


@app.post("/api/code-from-note", response_model=CodeFromNoteResponse)
def api_code_from_note(req: CodeFromNoteRequest):
    if not req.note.strip():
        raise HTTPException(status_code=400, detail="note must not be empty.")
    try:
        _main_term, rounds, final_result = resolve_diagnosis(req.note)
    except DiagnosisResolutionError as e:
        raise HTTPException(status_code=500, detail=str(e))

    trace = [
        TraceStepResponse(summary=r["chosen_label"], citation=r["citation"])
        for r in rounds
    ]
    return CodeFromNoteResponse(
        code=final_result.code_formatted,
        description=final_result.description,
        short_title=final_result.short_title,
        trace=trace,
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.post("/api/code-from-note/stream")
def api_code_from_note_stream(req: CodeFromNoteRequest):
    if not req.note.strip():
        raise HTTPException(status_code=400, detail="note must not be empty.")

    def generate():
        events: "queue.Queue" = queue.Queue()
        done = object()

        def on_status(message: str):
            events.put({"type": "status", "message": message})

        def on_question(round_num: int, question_text: str):
            events.put({"type": "question", "round": round_num, "question": question_text})

        def on_round(round_info: dict):
            events.put({
                "type": "round",
                "chosen_label": round_info["chosen_label"],
                "citation": round_info["citation"],
            })

        def worker():
            try:
                _main_term, _rounds, final_result = resolve_diagnosis(
                    req.note, on_status=on_status, on_question=on_question, on_round=on_round
                )
                events.put({
                    "type": "resolved",
                    "code": final_result.code_formatted,
                    "description": final_result.description,
                    "short_title": final_result.short_title,
                })
            except DiagnosisResolutionError as e:
                events.put({"type": "error", "detail": str(e)})
            except Exception:
                # Any other failure (model/network error, etc.) must still
                # reach the client as an event -- otherwise the thread dies
                # silently, the stream just ends, and the UI has nothing to
                # show but a re-enabled button. See clarify.log / server logs
                # for the actual traceback.
                logging.getLogger("api").exception("code-from-note/stream: worker failed")
                events.put({"type": "error", "detail": "Internal error resolving diagnosis -- see server logs."})
            finally:
                events.put(done)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            event = events.get()
            if event is done:
                break
            yield _sse(event)

    return StreamingResponse(generate(), media_type="text/event-stream")
