#!/usr/bin/env python3
"""Terminal interface for the ICD-10-CM search engine.

Usage:
    python cli.py                  interactive loop
    python cli.py "left cataract"  single-shot: search once, print, exit
"""
import os
import platform
import sys


def _ensure_expat_fix():
    """On this dev machine, Homebrew's system libexpat is stale, which breaks
    plistlib (via pyexpat) and, transitively, platform.mac_ver() -- which the
    anthropic SDK's SSL/transport setup (truststore) calls the moment a
    clarify() question actually needs to reach the model. Left unfixed, that
    call silently fails and clarify() falls back to the plain list with no
    visible explanation (by design -- same as the no-question path), which
    looks like the feature just isn't working. Detect the broken state here
    and self-relaunch with the fix applied, so no invocation of this file
    needs to remember an env var prefix."""
    if sys.platform != "darwin" or platform.mac_ver()[0]:
        return
    expat_lib = "/opt/homebrew/opt/expat/lib"
    if not os.path.isdir(expat_lib):
        return
    current = os.environ.get("DYLD_LIBRARY_PATH", "")
    if expat_lib in current.split(":"):
        return  # already set but still broken -- don't relaunch forever
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

from search_engine.clarify import clarify
from search_engine.core import search

MAX_CLARIFY_ROUNDS = 5  # safety net against a question that fails to narrow anything


def print_candidate(prefix: str, r):
    print(f"{prefix}{r.code_formatted:10s}  {r.description}")
    if r.short_title and r.short_title != r.description:
        print(f"     short: {r.short_title}")
    print(f"     score: {r.score:.2f}")
    for reason in r.reasons:
        print(f"       - {reason}")


def print_candidates(candidates, heading=None):
    if heading is None:
        heading = "Single best match:" if len(candidates) == 1 else f"{len(candidates)} candidate(s):"
    print(heading + "\n")
    for i, r in enumerate(candidates, 1):
        print_candidate(f"{i:2d}. ", r)
        print()


def print_filters(result):
    filters = []
    if result.laterality_filter:
        filters.append(f"laterality={result.laterality_filter}")
    if result.encounter_filter:
        filters.append(f"encounter={result.encounter_filter}")
    if filters:
        print(f"(filters applied: {', '.join(filters)})")


def prompt_for_option(question) -> int:
    """Returns the chosen index into question.options, or -1 if the user backed out."""
    while True:
        try:
            raw = input("Your choice (number, or Enter to see the full list instead): ").strip()
        except EOFError:
            return -1
        if not raw:
            return -1
        if raw.isdigit() and 1 <= int(raw) <= len(question.options):
            return int(raw) - 1
        print(f"  enter a number from 1 to {len(question.options)}")


def handle_result(result):
    print_filters(result)

    if not result.results:
        print("No matches found.")
        return

    if result.is_single:
        print_candidates(result.results)
        return

    candidates = result.results

    for round_num in range(1, MAX_CLARIFY_ROUNDS + 1):
        question = clarify(candidates)
        if question is None:
            print_candidates(candidates)
            return

        by_code = {r.code_formatted: r for r in candidates}

        print(question.question + "\n")
        for i, opt in enumerate(question.options, 1):
            codes_str = ", ".join(opt.codes) if len(opt.codes) <= 4 else f"{len(opt.codes)} codes"
            print(f"{i:2d}. {opt.label}  ({codes_str})")
        print()

        choice = prompt_for_option(question)
        if choice == -1:
            print()
            print_candidates(candidates)
            return

        chosen_codes = question.options[choice].codes
        narrowed = [by_code[c] for c in chosen_codes if c in by_code]
        print()

        if len(narrowed) == 1:
            print("Resolved:\n")
            print_candidate("-> ", narrowed[0])
            print()
            return

        if len(narrowed) >= len(candidates):
            # the question didn't actually narrow anything -- stop rather than loop forever
            print_candidates(narrowed)
            return

        candidates = narrowed
        print(f"Narrowed to {len(candidates)} candidate(s) -- next question:\n")

    print(f"Reached the clarification round limit ({MAX_CLARIFY_ROUNDS}) -- remaining candidates:\n")
    print_candidates(candidates)


def interactive_loop():
    print("ICD-10-CM search (Ctrl-D or empty line to quit)")
    while True:
        try:
            query = input("\n> ").strip()
        except EOFError:
            print()
            break
        if not query:
            break
        handle_result(search(query))


def single_shot(query: str):
    handle_result(search(query))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        single_shot(" ".join(sys.argv[1:]))
    else:
        interactive_loop()
