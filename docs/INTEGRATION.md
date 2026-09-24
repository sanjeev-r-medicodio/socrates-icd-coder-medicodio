# Integrating icd-coder into nextgen-codio-engine

For engineers wiring icd-coder into the engine's ICD pipeline as an alternative
to the S4.1 → S4.2 → S4.3 tree traversal. Everything upstream (DXEX diagnosis
extraction) and downstream (low-confidence and duplicate filters, injury
pipeline, general coding guidelines, ICD↔CPT linking, sequencing) stays as it
is.

## What it replaces

Per diagnosis, the engine currently runs chapter routing (S4.1), subchapter /
section routing (S4.2) and final code selection (S4.3), then merges a separate
7th character. icd-coder does that whole step for one diagnosis:

1. Deterministic search over the CMS Tabular List and Alphabetical Index for
   the fiscal year in effect on the date of service. Candidates always come
   from the official code set, so no code can be invented.
2. If more than one candidate remains, a clarifying question whose options
   partition the candidates, answered from the chart text with a verbatim
   citation, repeated until one code is left.

## Contract

```python
from search_engine import integration, llm

llm.set_backend(engine_backend)   # once at startup; see "Model calls" below

final_result = integration.code_diagnosis(
    dx,                 # the engine's per-diagnosis dict from DXEX
    chart_text,         # the same CDI-normalized text S4.3 receives
    date_of_service,    # date | datetime | "YYYY-MM-DD"
)
```

**Input `dx`.** Only `phrase` is required. These are also used when present:

| key | used as |
|---|---|
| `phrase` | the search query |
| `anatomical_location` or `location` | appended to the query when the phrase doesn't already contain it |
| `laterality` | filter; "Right", "RT", "left side", "both", ... are normalized, anything else is treated as not documented |
| `encounter` or `episode_of_care` | 7th-character filter: initial / subsequent / sequela |

**Output.**

| key | meaning |
|---|---|
| `icd_code` | full billable code with the 7th character already included (e.g. `S72.001A`), or `None` |
| `icd_code_desc` | CMS long description |
| `node_id` | always `None`: the host maps `icd_code` to its own tree node |
| `confidence` | heuristic in [0, 1], see below |
| `seventh_character_unresolved` | `True` when the code is known apart from an undocumented 7th character |
| `status` | `resolved`, `unspecified_default`, `seventh_char_unresolved`, `unresolved` or `no_candidates` |
| `code_set` | `FY2026` or `FY2027`, whichever was used |
| `trace` | query, candidate count, each round's question, options, answer and citation, and remaining codes; JSON-serializable |

## Wiring it into `run_icd_pipeline`

`Codio_backend_engine/code_prediction/icd_prediction/icd_main.py`

1. **Put it behind a config switch**, e.g. `icd_selection.mode = "tree" | "icd_coder"`
   in the bundle's `MODULE_CONFIG`, defaulting to `"tree"`. With
   `"icd_coder"`, skip the batched S4.1 / S4.2 calls and call
   `code_diagnosis()` inside `_process_diagnosis`, storing the result as
   `cr["final_result"]`. The engine already runs `_process_diagnosis` in a
   thread pool, and `code_diagnosis` is safe there: each call opens its own
   read-only SQLite connection.
2. **Skip the 7th-character merge** for these results. `icd_main.py`
   currently appends `selected_seventh_character` to `icd_code`
   (`build_full_icd_seventh_char`). icd-coder's code is already complete, so
   branch on `final_result["selector"] == "icd_coder"`.
3. **Keep the C23 drop.** `seventh_character_unresolved=True` means the same
   thing it does today: drop the unbillable stem and log it for review.
4. **Map `node_id`.** Look `icd_code` up in the engine's `node_map` (built
   from `icd_kb_preview.xlsx`). A code missing from the engine KB is a
   code-set mismatch between the two systems. Log it as an error rather
   than shipping a null `node_id`.
5. **Record the trace** in `tree_traversal.chapter_routing[i]` so
   `/code-audit`, `block_sim` and the icd-detective agent can show why a
   code was chosen.

## Model calls

`llm.set_backend(fn)` routes every model call through the host. `fn` is
called as `fn(tier, system, user, schema)` and must return
`schema(...).model_dump()`:

- `tier="fast"`: phrasing the clarifying question (small, cheap model)
- `tier="reasoning"`: answering it from the chart (stronger model)

Implement `fn` on top of `call_llm_wrapper` so provider fallback,
record/replay, logging and cost tracking all apply. The two prompts are the
module constants `clarify._SYSTEM_PROMPT` and `resolver._ANSWER_SYSTEM_PROMPT`.
To manage them in the engine's prompt engine, seed them as workflows there.

## Confidence and the engine's filters

Confidence is a transparent heuristic, not a calibrated probability:

| how the code was reached | confidence |
|---|---|
| search alone was unambiguous | 0.9 |
| every answer cited verbatim from the chart | 1.0 |
| each answer whose citation isn't in the chart | −0.3 |
| note didn't document the distinction, unspecified code used | at most 0.5 |

The engine's default low-confidence floor is 0.30, so a code with three
unverified citations will be dropped. That is intended. The confidence
ceiling from S2 completeness can be applied on top as `min(confidence, ceiling)`.

## Code sets

icd-coder ships FY2026 (effective 2025-10-01 to 2026-09-30) and FY2027
(effective from 2026-10-01) and picks one by `date_of_service`. A date outside
those years raises `code_sets.UnsupportedDateOfService`: surface it, don't
guess. Check that the engine's `icd_kb_preview.xlsx` covers the same fiscal
year before comparing the two systems. Otherwise differences will look like
accuracy problems.

## Specialty shortlists

The engine restricts gastro S4.3 candidates to `icd_Kb_gastro_54.csv`.
icd-coder doesn't take a shortlist yet. For a like-for-like comparison, either
turn the shortlist off for the tree run or filter icd-coder's final code
against it and count out-of-list codes separately.

## Not covered yet

- The Table of Drugs and Chemicals, the Neoplasm Table and the External Cause
  Index are not loaded. Poisoning, neoplasm and external-cause codes are
  reached only through their Tabular titles. The FY2026 CMS zip includes all
  three as XML.
- Colloquial vocabulary gaps ("ingrown", "femoral" → femur) are tracked as
  known issues in `tests/cases.json`.
- icd-coder codes one diagnosis per call. Code-first / use-additional pairing,
  combination codes across diagnoses and sequencing remain the engine's
  general-coding-guidelines stage.

## Before switching the default

1. Pull finished encounters with coder-final codes (the engine's
   `qa/scripts/coding_accuracy_diff.py` already reads them from the PE API).
2. Replay the same DXEX phrases through both selectors and compare, per
   diagnosis:
   - exact-code match
   - 3-character category match
   - how often each selector produces no code
   - rounds, latency and cost
3. Report injury / 7th-character cases and gastro-shortlist cases separately.
