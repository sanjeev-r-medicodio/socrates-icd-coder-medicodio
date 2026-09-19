#!/usr/bin/env python3
"""Minimal single-diagnosis note -> code loop.

Usage:
    python cli_diagnosis.py path/to/note.txt   read the note from a file
    python cli_diagnosis.py                    paste the note, then Ctrl-D (EOF)

Pipeline: note_agent.resolve_diagnosis() -- extract_main_term(note) ->
core.search() -> clarify() generates a grouped MCQ for ambiguous results ->
note_agent.answer_question() picks the note-supported option -> narrow ->
repeat until one code remains. The loop itself lives in
search_engine/note_agent.py, shared with api.py's POST /api/code-from-note --
this file just drives it and prints live progress.

Explicit assumption (see the project plan): the note is a single, complete,
well-documented encounter with a clearly codeable diagnosis. There is no
"not specified" path in this version -- a validation failure that survives
one retry is a bug to surface loudly, not a case to route around.
"""
import os
import platform
import sys


def _ensure_expat_fix():
    """See cli.py -- same Homebrew/libexpat workaround, needed here too since
    this entry point also constructs Anthropic clients (note_agent, clarify)."""
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

import logging
from pathlib import Path

logging.basicConfig(
    filename=Path(__file__).resolve().parent / "clarify.log",
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from search_engine.note_agent import DiagnosisResolutionError, resolve_diagnosis


def read_note() -> str:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.exists():
            print(f"No such file: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text()

    print("Paste the visit note, then press Ctrl-D (EOF):\n", file=sys.stderr)
    return sys.stdin.read()


def on_status(message: str):
    """Ephemeral 'working on it' line -- stderr, so it doesn't clutter piped
    stdout, and flushed immediately rather than waiting for buffering."""
    print(message, file=sys.stderr, flush=True)


def on_question(round_num: int, question_text: str):
    print(f"Round {round_num}: {question_text}", flush=True)


def on_round(round_info: dict):
    print(f"  answered: {round_info['chosen_label']!r}  (code: {round_info['selected_code']})")
    print(f"  citation: \"{round_info['citation']}\"")
    print(f"  narrowed to: {', '.join(round_info['narrowed_to'])}\n", flush=True)


def main():
    note = read_note()
    if not note.strip():
        print("Empty note -- nothing to process.", file=sys.stderr)
        sys.exit(1)

    try:
        main_term, _rounds, final_result = resolve_diagnosis(
            note, on_status=on_status, on_question=on_question, on_round=on_round
        )
    except DiagnosisResolutionError as e:
        print(f"FAILED TO RESOLVE: {e}", file=sys.stderr)
        sys.exit(1)

    if not _rounds:
        # single result, no clarify rounds needed -- the "Main term: ..."
        # line would otherwise never print anything before the resolution
        print(f"Main term: {main_term}\n", flush=True)

    print(f"RESOLVED: {final_result.code_formatted}  {final_result.description}")
    if final_result.short_title and final_result.short_title != final_result.description:
        print(f"  short: {final_result.short_title}")


if __name__ == "__main__":
    main()
