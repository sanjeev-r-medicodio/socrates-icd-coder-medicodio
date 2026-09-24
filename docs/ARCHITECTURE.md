# Architecture

Full technical reference for this project: setup, project layout, and how
each piece works internally. For the product pitch and quick-start, see
`README.md`. For AI-agent-specific gotchas and traps, see `CLAUDE.md`.

## Setup

1. Place these CMS source files in `data/raw/` (already done for FY2027):
   - `icd10cm_tabular_2027.xml`
   - `icd10cm_index_2027.xml`
   - `icd10cm_order_2027.txt`
   - `icd10cm_tabular.xsd`, `icd10cm_index.xsd`, `icd10OrderFiles.pdf` (reference only)

2. Build the database (idempotent -- safe to re-run):

   ```bash
   DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib python3.12 scripts/build_db.py
   ```

   This produces `data/icd10.db` and prints sanity-check spot-checks.

   The Tabular XML lists 7th-character codes only as a stem plus a
   `sevenChrDef` table (e.g. `S72.001` + `A`/`D`/`S`/...); the full billable
   codes (`S72.001A`, `T40.1X1A`, `S01.00XA`) exist only in the order file.
   The build adds every one of them under its stem, with `seventh_char` set,
   and fails if the DB's billable set doesn't match the order file exactly.
   Generated codes are kept out of `fts_tabular` so they don't shift bm25
   scores; search reaches them by expanding a hit on their stem.

3. Set up the virtual environment (Homebrew's system Python is
   externally-managed and refuses a bare `pip install`):

   ```bash
   DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib python3.12 -m venv .venv
   DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib .venv/bin/pip install anthropic pydantic fastapi uvicorn
   ```

4. Add your API key to `.env` at the project root (already gitignored):

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   CLAUDE_MODEL_FAST=claude-haiku-4-5-20251001
   CLAUDE_MODEL_REASONING=claude-opus-5
   ```

   Every module that needs a key (`clarify.py`, `note_agent.py`) fails fast
   at import time with a clear error if the key it needs is missing.
   `CLAUDE_MODEL_FAST` is used by `clarify()` (phrasing a question over an
   already-narrowed candidate list). `CLAUDE_MODEL_REASONING` is used by both
   `note_agent.py` methods -- interpreting what a note actually states (vs.
   inventing something plausible) needs real clinical-language judgment, not
   the fast tier.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest                           # offline suite (needs data/icd10.db)
ICD_CODER_LIVE_TESTS=1 python -m pytest    # also runs tests that call the model
python tests/run_smoke.py                  # quick, dependency-free view of cases.json
```

For ranking changes, also compare `python scripts/benchmark_search.py`
before and after. It samples 300 Alphabetical Index paths and 300 Tabular
titles (fixed seed) and reports top-1 and recall@20 against the code each
one leads to -- a consistency check with the code set, not clinical accuracy.

`tests/cases.json` is the search regression set. A case with a `known_issue`
is a strict xfail: it documents a bug that isn't fixed yet, and the suite
fails if it starts passing until the field is removed.

## Project structure

- `data/raw/` -- CMS source XML/order file/XSDs.
- `data/icd10.db` -- built SQLite database (Tabular + Index + FTS5), gitignored.
- `scripts/build_db.py` -- ETL: parses the XML/order file into `data/icd10.db`.
- `search_engine/core.py` -- `search(diagnosis: str) -> SearchResult`, the only
  place the query pipeline logic lives. Pure and deterministic.
- `search_engine/clarify.py` -- `clarify(candidates) -> Optional[ClarifyQuestion]`.
  Imports the Anthropic SDK. See "The clarify addition" below. Also has a
  `run_cascade`/`AnswerSource` cascade runner that's currently unused --
  built for the now-removed multi-diagnosis pipeline's "not specified"
  handling.
- `search_engine/note_agent.py` -- `extract_main_term(note)`,
  `answer_question(note, question, options)`, and the shared
  `resolve_diagnosis(note, on_status=, on_question=, on_round=)` loop used by
  both `cli_diagnosis.py` and `api.py`. See "The note-to-code loop" below.
- `search_engine/_env.py` -- shared `.env` loading, used by `note_agent.py`
  (`clarify.py` predates it and keeps its own copy, deliberately untouched).
- `cli.py` -- terminal interface for single-diagnosis search (interactive +
  single-shot), with a human answering `clarify()`'s questions via stdin.
- `cli_diagnosis.py` -- terminal interface for the note-to-code loop, with
  live streaming progress to the terminal via `resolve_diagnosis()`'s callbacks.
- `api.py` -- FastAPI layer wrapping the above for the web frontend. See
  "The web app" below.
- `frontend/` -- Vite + React app (two tabs: search-diagnosis, encounter-note).
- `sample_notes/` -- unambiguous, fully-documented note paragraphs
  (diverticular disease and hip pain scenarios) used to test the loop above.
- `tests/cases.json`, `tests/test_search_cases.py`, `tests/run_smoke.py` --
  `core.search()` regression cases (see "Running the tests").
- `tests/test_code_set.py` -- built DB vs. the CMS order file (every billable
  code present, 7th-character codes well-formed).
- `tests/test_clarify.py` -- `clarify()` tests (live question quality on a
  sibling case and a large heterogeneous fan-out, plus a network-free
  forced-validation-failure case).

## How the query pipeline works (`search_engine/core.py`)

1. Strip laterality (`left`/`right`/`bilateral`) and encounter-type
   (`initial`/`subsequent`/`sequela`) trigger words out of the query text;
   remember them as post-filters.
2. Expand remaining tokens against the `synonyms` table (e.g. `MI` ->
   `myocardial infarction`) -- query expansion only, the FTS index itself is
   untouched.
3. Drop tokens that match an unusually large fraction of all diag rows (e.g.
   "with", "right", "lower" -- see `GENERIC_TOKEN_DOC_FREQ_RATIO`) before
   building the FTS query. Without this, a handful of common words can
   co-occur in an unrelated title and out-score a row matching one truly
   specific term -- e.g. "community-acquired pneumonia" was matching an
   unrelated orthopedic "acquired deformities" category purely because
   "acquired" happened to be common ground, before this filter existed.
4. Run an FTS5 (Porter-stemmed) `OR` query across three sources: Tabular
   titles/descriptions, Alphabetical Index terms, and the reverse
   `note_references` index (Code First / Use Additional / Code Also /
   Excludes1 / Excludes2 conditions).
   - Each Index term is indexed together with its parent path (the subterm
     "hip" under "Pain, joint" is searchable as "Pain joint hip"), so a
     multi-word query matches the whole path a coder would follow rather
     than every bare subterm named "pain" or "hip".
   - Excludes text lists what a code does *not* cover, so it never boosts
     its owner. When the query fully describes the excluded condition (e.g.
     "joint pain" against R52's "joint pain (M25.5-)"), the owner is
     penalized and the code the note points to gets the boost; partial
     overlaps are ignored.
5. Resolve every hit to billable leaf code(s):
   - a header/category code (e.g. `E08.32`, which requires a 6th character,
     or the stem `S72.001`, which requires a 7th) expands to all of its
     billable descendants;
   - an Index entry whose code ends in `-` (e.g. `M25.55-`) expands the same
     way -- this is what turns a bare "hip pain" query into the
     `M25.551/552/559` list rather than a dead end;
   - a `see`/`seeAlso` Index cross-reference triggers one more hop of Tabular
     search against the reference target -- but only when the matched Index
     term is a genuine root main term or a multi-word phrase. A single bare
     word matched at a non-root level (e.g. "acquired" appearing as a
     qualifier subterm under dozens of unrelated main terms like "clubfoot,
     acquired") only means something combined with its parent term, which
     isn't captured in that row's own text, so following its cross-reference
     on the word alone is untrustworthy.
6. Apply the laterality/encounter-type post-filter to the resolved candidate
   set. The filter is only trusted if at least one candidate *at the top
   score* survives it (not just some low-relevance candidate elsewhere in the
   list) -- otherwise it falls back to the unfiltered set. This matters
   because ties are common (e.g. right/left/bilateral/unspecified variants of
   the same match often score identically), so checking only a single
   arbitrarily-chosen "top" candidate would let which side happens to sort
   first silently decide whether the whole filter applies.
7. Re-rank: candidates whose title/description matches the query near-verbatim
   get a large boost (only when the query is multi-word, or the title itself
   is short -- a single-word query being a substring of nearly any title
   that merely mentions it isn't "near-exact"). Each of the four *sources*
   above (Tabular, Index, cross-reference, note-reference) contributes at
   most its single best hit per code -- this matters because a category
   header, its subcategory, and a leaf's own title can all independently
   match a common word like "pain," and without capping, that would stack
   additively across every descendant and drown out a code that matches
   multiple distinct query words in one place. Distinct sources still add
   together, since a code matching in both the Tabular title *and* the
   Index *and* a note reference is genuinely a stronger match than one from
   a single source. A cross-reference hop's score is also capped to never
   exceed the strength of the match that triggered it, so a coincidental
   two-word Index overlap can't unlock an unrelated, much stronger
   destination match.
8. Decide single-vs-list: if the top score exceeds the runner-up by
   `SINGLE_RESULT_MARGIN_RATIO` (a named constant in `core.py`, currently 1.4),
   return that one result; otherwise return the ranked list capped at
   `MAX_RESULTS` (20).

Every result carries a `reasons` list explaining why it matched (index-term
hit, tabular-title hit, synonym expansion, note-reference hit, cross-reference
hop, laterality filter applied, etc.) -- this is what `cli.py` prints under
each code, and it's the main tool for debugging/tuning ranking.

### Known ranking rough edge

Because ranking is driven by BM25 term-frequency plus how much Index/note
cross-referencing a code family happens to have, a code family with *less*
cross-referencing in the underlying CMS data can be under-surfaced relative to
a competing family with more, even when its raw title match is stronger (e.g.
"acute otitis media" under-surfacing the suppurative branch relative to the
nonsuppurative branch). There's no clinical-frequency data feeding the ranker
in v1, so this is a known limitation to watch for while tuning the weights in
`search_engine/core.py`, not a bug to "fix" outright -- the constants at the
top of `core.py` (`TABULAR_TITLE_WEIGHT`, `INDEX_TERM_WEIGHT`,
`CROSS_REFERENCE_WEIGHT_MULTIPLIER`, `EXACT_MATCH_BOOST`,
`SINGLE_RESULT_MARGIN_RATIO`, `GENERIC_TOKEN_DOC_FREQ_RATIO`) are the levers
to adjust -- re-run `tests/run_smoke.py` after any change.

A downstream answer source (human or AI) can still recover the correct code
even when it's not the top-ranked result, as long as it survives into the
candidate list handed to `clarify()` (capped at `MAX_RESULTS`) -- but it
cannot recover a code that never made that cut.

## The clarify addition

When `core.search()` returns a list (not a confident single match),
`search_engine/clarify.py` optionally turns it into one AI-generated
clarifying question with structured multiple-choice options. `core.py` itself
is untouched by this; `clarify()` only ever receives an already-narrowed
candidate list `core.py` produced on its own.

**When it fires:** for any list of 2+ candidates, regardless of how many or
how unrelated they are -- there's no candidate-count cap and no same-parent
requirement. For a large, heterogeneous set (e.g. 20 different hip
conditions spanning several unrelated ICD-10-CM chapters), one option can
cover more than one candidate code -- the model groups by whatever
distinguishing question narrows the set the most (e.g. "which of these best
describes the condition: dislocation, a structural abnormality, contracture,
or a loose body?"), rather than forcing one option per code. If the chosen
option still covers more than one code, the caller asks a follow-up question
over just that narrowed subset, repeating until it resolves to one code.

**Grounding:** the model receives the actual candidate codes with their full
descriptions, 7th-character meanings (when applicable), and up to a few
Alphabetical Index breadcrumbs per code (e.g. "Otitis, media, acute,
suppurative" -- the coding-vocabulary phrasing a human coder would search
under, which is often closer to how a note is written than the Tabular's
formal title) and does exactly one job: phrase one question whose options
partition the candidates. It never invents a distinguishing question from
nothing and never picks the final code itself.

**Validation is non-negotiable:** every option's codes in the model's
response are checked against the candidate list it was given -- every
candidate must appear in exactly one option, no duplicates, none missing, no
invented codes. On failure, `clarify()` retries once with the same input; on
a second failure, it logs the failure and returns `None`, and the caller
falls back to printing the plain list. An unvalidated model response never
reaches the user.

**Logging:** every `clarify()` call logs its input candidates, the model's raw
response, the validation result, and the final resolved question to
`clarify.log` -- same debuggability model as `core.py`'s `reasons` list.

## The note-to-code loop

`search_engine/note_agent.py`'s `resolve_diagnosis()` runs a deliberately
minimal loop, built directly on `core.search()`/`clarify()` without any
wrapper machinery, shared by `cli_diagnosis.py` (live terminal streaming via
callbacks) and `api.py` (no callbacks, just the returned tuple):

1. `note_agent.extract_main_term(note)` reads the raw note and returns the
   diagnosis's main term, including every qualifier the note actually states
   that distinguishes it (laterality, site, acuity, with/without-complication
   status) -- e.g. "pain in right hip", not just "pain" or "hip pain" with
   the laterality dropped. Dropping a stated qualifier makes the search far
   less useful even though the bare word is technically still "a diagnosis".
2. That term seeds `core.search()`, unchanged.
3. A single result resolves immediately. A list goes to `clarify()`
   (unchanged) for a grouped multiple-choice question.
4. `note_agent.answer_question(note, question, options)` reads the note and
   picks the option it documents, returning one code from that option's list
   plus the exact verbatim quote that supports it. It must always select an
   option -- there is no "not specified" path in this version, per the
   explicit assumption below.
5. The returned code is checked against this round's full candidate set
   (every code across every option) -- the same membership check `clarify()`
   already applies to its own output. On failure, retry once; a second
   failure raises `DiagnosisResolutionError` and stops. That's a bug to fix,
   not a case to route around.
6. Candidates narrow to the *entire* chosen option's code list (not just the
   one code returned -- if that option covers more than one code, the loop
   continues with a fresh `clarify()` round on the narrowed set). Repeat
   until exactly one code remains.

**Explicit assumption, relied upon throughout:** the note is a single,
complete, well-documented encounter with a clearly codeable diagnosis. There
is no missing-information handling and no multi-diagnosis extraction in this
version -- both are real, deliberately deferred future work. Both
`note_agent.py` methods share one grounding instruction: use real language
understanding (recognizing synonyms, abbreviations, indirectly-stated facts)
to interpret what the note actually says, never invent or infer something it
doesn't state. That still matters even with no "not specified" branch -- it's
the difference between picking the option the note actually supports versus
the statistically likely one.

## The web app

`api.py` is a thin FastAPI layer over `core.py`/`clarify.py`/`note_agent.py`
with zero internal logic changes -- it only shapes requests/responses.

- `POST /api/search {query}` and `POST /api/answer {selected_codes}` --
  **fully stateless.** No session or conversation id: the client only ever
  sends back the codes from the option it picked, and the server re-derives
  the next round from that alone (re-fetching those codes and calling
  `clarify()` again). Response is `{type: "single", code, description,
  short_title}` or `{type: "clarify", question, options: [{label, codes}]}`
  -- no round count or trace field, since the server has no memory of prior
  rounds. That bookkeeping is owned client-side, in
  `frontend/src/components/SearchDiagnosisTab.jsx`.
- `POST /api/code-from-note {note}` -- runs `note_agent.resolve_diagnosis()`
  end to end in one call, returning `{code, description, short_title, trace:
  [{summary, citation}, ...]}` with the full trace, since the client never
  sees intermediate state here.
- `POST /api/code-from-note/stream {note}` -- the same loop, but forwards
  `resolve_diagnosis()`'s `on_status`/`on_question`/`on_round` callbacks as
  Server-Sent Events (one JSON object per `data:` line: `status`, `question`,
  `round`, `resolved`, or `error`), so `frontend/src/components/EncounterNoteTab.jsx`
  can show the same live progress the CLI has instead of sitting blank until
  the whole loop finishes. `resolve_diagnosis()` is synchronous (blocking
  Anthropic SDK calls), so this runs it on a background thread that pushes
  events onto a `queue.Queue` while the generator yields them out.

`frontend/` is a Vite + React app with two tabs (`SearchDiagnosisTab.jsx`,
`EncounterNoteTab.jsx`) mirroring the two CLI flows. Design tokens are CSS
custom properties in `frontend/src/index.css` (so a future dark-mode toggle
is cheap to add, not built yet). Dev server proxies `/api` to
`http://127.0.0.1:8000` (`frontend/vite.config.js`).
