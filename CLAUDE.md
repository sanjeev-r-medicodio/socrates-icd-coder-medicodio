# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For the product pitch and quick-start, see `README.md`. For full technical
architecture (setup, project structure, query pipeline, clarify/note-loop
design, the web app), see `docs/ARCHITECTURE.md`. This file stays short on
purpose -- it's loaded into every session, so it only holds what's worth
knowing before you run a single command or touch a single file.

## Environment gotcha (macOS / Homebrew, this dev machine)

Homebrew's Python has a `pyexpat` extension linked against a stale system
`libexpat`. This breaks `xml.etree` directly (`scripts/build_db.py`) and,
less obviously, `platform.mac_ver()` (via `plistlib`), which the `anthropic`
SDK's SSL/transport setup calls at client-construction time -- so *any* code
path that actually reaches the model needs
`DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib` set, or it fails silently
and falls back to whatever its no-op path is (looks like the feature just
isn't working). `cli.py`, `cli_diagnosis.py`, and `api.py` self-detect and
relaunch with the fix applied; other entry points need it set by hand.

## Quick command reference

```bash
.venv/bin/python cli.py                                     # human-answered search
.venv/bin/python cli_diagnosis.py sample_notes/<file>.txt    # AI-answered note loop
.venv/bin/uvicorn api:app --reload --port 8000                # web API
cd frontend && npm run dev                                    # web frontend (proxies /api -> :8000)
.venv/bin/python tests/run_smoke.py                          # network-free
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib .venv/bin/python tests/test_clarify.py
```

## Traps specific to this repo

- **Don't reach for `clarify.py`'s `run_cascade`/`AnswerSource` for a new
  answer source without checking whether its "not specified" semantics
  actually fit.** It's currently unused -- built for a removed pipeline with
  different requirements than `cli.py` or `cli_diagnosis.py` have. Both of
  those (and `api.py`, via `note_agent.resolve_diagnosis()`) implement their
  own simple round-narrowing loop directly instead.
- **`note_agent.py`'s narrow scope (single diagnosis, no "not specified"
  path, a second validation failure is a hard error) is deliberate, not
  unfinished.** Don't add missing-information handling or multi-diagnosis
  extraction to it unless explicitly asked -- that's real, intentionally
  deferred future work.
- **Check git history before rebuilding anything that sounds like it should
  already exist** (note reorganization, multi-diagnosis extraction with
  exclusion rules). A fuller version of this project had that; it was
  intentionally removed to reset to a minimal, provably-solid loop, and is
  fully recoverable from the snapshot commit rather than worth
  reimplementing from scratch.
- **`note_agent.py`'s two model calls need a generous `max_tokens` (4096),
  not a tight one.** `CLAUDE_MODEL_REASONING` does adaptive thinking by
  default, and thinking tokens count against `max_tokens` -- a clinically
  dense note can burn the entire budget on reasoning before any JSON output
  exists, leaving `parsed_output` as `None` (silent `AttributeError`
  otherwise). Both calls now raise a clear `RuntimeError` if that happens,
  but if you ever see it again, the budget is the first thing to check.
- **`api.py`'s server is fully stateless between calls** -- no session or
  conversation id. For `/api/search`/`/api/answer`, the client only ever
  sends back the codes from the option it picked; the server re-derives the
  next round from that alone. It does not remember a prior round's question
  or label, so don't add server-side round/trace bookkeeping to those two
  endpoints -- that's owned client-side in `frontend/src/components/SearchDiagnosisTab.jsx`.
