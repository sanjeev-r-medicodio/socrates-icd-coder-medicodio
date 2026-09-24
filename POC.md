# Proof of Concept: Index-grounded ICD-10-CM coding for nextgen-codio-engine

**Author:** Sanjeev Ragunathan · [LinkedIn](https://www.linkedin.com/in/sanjeev-ragunathan) · [GitHub](https://www.github.com/sanjeev-ragunathan)
**Status:** POC complete and integration-ready (`icd-coder`, branch `making-it-integration-ready`, PRs #1–#9).
**Proposal:** replace the engine's ICD tree traversal (S4.1 → S4.2 → S4.3) with this approach, behind a config switch, after a head-to-head evaluation on coder-final charts.
**Date:** 2026-09-24

---

## 1. Summary

- **The idea.** Let the official CMS code set pick the candidates and let the model only answer questions about the chart. A deterministic search over the CMS Alphabetical Index and Tabular List finds the candidate codes; a language model then asks a multiple-choice question that splits those candidates, and a second model answers it from the chart, quoting the sentence that supports its answer. This repeats until one billable code is left.
- **Why it matters.**
  - The model never proposes a code, so it cannot return one that doesn't exist or that wasn't shortlisted. That is the engine's open C3/T2 defect.
  - Each code choice comes with a trail a coder can audit: the question asked, the answer, and the quote from the chart.
- **Results so far.**
  - 88 automated tests pass, including the live-model tests.
  - 9 of 9 end-to-end test notes come back with the expected code.
  - A 600-query search benchmark improved from 41.7% → 69.0% top-1 on Alphabetical Index paths and from 90.0% → 94.0% on Tabular titles.
- **Cost.** $0.014 per diagnosis on average. When questions are needed it's $0.007–$0.047; 4 of 9 cases needed no model call at all.
- **Not yet proven.** Accuracy against real coder-final charts, and head-to-head against the current engine. Section 9.3 is the plan to measure both before any switch.

---

## 2. The problem with the current ICD step

These findings come from reading the engine code (`Codio_backend_engine/code_prediction/icd_prediction/`) and its own audit register (`Docs/code_audit/ISSUE_REGISTER_VERIFICATION_2026-07-23.md`).

| # | Issue | Where | Effect |
|---|---|---|---|
| P1 | **No fence on the final code (C3/T2, confirmed, open).** S4.3 looks the model's `chosen_node_id` up in the full `node_map` first, not in the shortlist it was given. | `s4_3_final_selection/router.py:653`, `:780` | A code outside the candidate set can reach the claim. |
| P2 | **Errors compound down the tree.** Chapter (S4.1), subchapter/section (S4.2) and final code (S4.3) are three separate LLM decisions (`claude-sonnet-4-6`); a wrong chapter can't be recovered later. | `icd_main.py`, `s4_1/`, `s4_2/`, `s4_3/` routers | A wrong chapter or section pick makes every later step wrong. |
| P3 | **The 7th character is chosen separately and merged afterwards.** | `icd_main.py:600–621` | Unbillable stems have to be caught and dropped after the fact (C1/C23). |
| P4 | **The confidence ceiling isn't applied (A3, confirmed, open).** It is computed in pre-processing but not enforced in S4.3 or the LLM wrapper. | `llm/wrapper.py`, S4.3 | Confidence doesn't reflect chart completeness. |
| P5 | **No accuracy measurement.** There are no gold ICD labels and no precision/recall scripts, only regression-parity and single-encounter diff tools. | `qa/` | Nobody can say how accurate ICD prediction is, or whether a change helped. |
| P6 | **The pipeline is hardcoded.** There's no predictor interface; alternatives are config flags inside fixed stages. | `run_icd_pipeline` | Hard to trial a different selection method. |

---

## 3. The idea

```
DXEX diagnosis phrase (+ laterality, location, encounter)       <- engine, unchanged
        │
        ▼
Deterministic search over the CMS code set for the date of service   (no LLM)
  - Alphabetical Index terms, matched with their full path ("Pain, joint, hip")
  - Tabular titles, Excludes/Code-first/Use-additional notes
  - laterality / encounter filters; every candidate is a real billable code
        │
        ├── one clear winner ─────────────────────────────► code (no model call)
        │
        ▼
Clarifying loop (repeat until one code remains)
  1. Question model (Haiku 4.5): write ONE multiple-choice question whose options
     partition the candidates. Validated: no invented, missing or duplicated codes.
  2. Answer model (Opus 5): pick the option the chart documents, with a verbatim
     quote, or answer "not documented".
  3. Quote is checked against the chart text; narrow to the chosen option.
        │
        ▼
final_result: full billable code (7th character included), confidence, trace
        │
        ▼
Engine: duplicate filter, injury pipeline, coding guidelines,                <- engine, unchanged
ICD↔CPT linking, sequencing
```

**Design principles**

1. **The code set is the only source of codes.** The models phrase and answer questions; they never propose a code.
2. **The answer model can say "the chart doesn't say."** Missing detail isn't guessed. Within one category the loop takes the category's "unspecified" code with lower confidence; if only the 7th character is missing, the result is flagged instead.
3. **Every decision is evidenced.** Each answer carries a quote from the chart, and the quote is verified against the chart text.
4. **The code set follows the date of service.** FY2026 (to 2026-09-30) and FY2027 (from 2026-10-01) are both loaded.

---

## 4. How it is better

| | Current engine (S4.1–S4.3) | icd-coder |
|---|---|---|
| Where candidates come from | LLM routing down a tree, plus optional RAG (Pinecone) | Deterministic search over the CMS Index and Tabular List |
| Can the output be a code that wasn't a candidate? | **Yes** (P1, open) | **No, by construction.** Options are validated against the candidate set. |
| Can it return a code that doesn't exist? | Only codes in the engine's node map (`icd_kb_preview.xlsx`) | Only billable codes in the CMS order file for that fiscal year |
| Error propagation | A wrong chapter can't be corrected downstream | No chapter/section commitment; narrowing happens by answering questions about the chart |
| 7th character | Chosen separately and merged afterwards | Part of the code; asked about like any other distinction |
| Missing documentation | The model must pick something | Explicit "not documented" answer; unspecified code or flagged 7th character |
| Evidence | Model rationale | Verbatim chart quote per decision, checked against the text |
| Code-set version | Single workbook | Chosen by date of service (FY2026 / FY2027) |
| Confidence | Model-reported | Derived from how the answer was reached (quotes verified, defaults used) |
| Measurement | None (P5) | Regression suite, search benchmark, live evaluation and cost harness |

---

## 5. What it solves

| Engine problem | How icd-coder addresses it |
|---|---|
| P1: off-shortlist codes | Candidates come only from the code set; each option list is validated as an exact partition of the candidates. |
| P2: compounding tree errors | There's no routing tree. Every candidate stays reachable until the chart rules it out. |
| P3: separate 7th-character merge | Full 7-character codes (51,305 in FY2026, 51,378 in FY2027) are in the code set; the returned code is always billable, or flagged. |
| P4: confidence not grounded | Confidence reflects verified quotes and "not documented" defaults. The S2 ceiling can be applied as `min(confidence, ceiling)`. |
| P5: no measurement | Comes with tests, a benchmark and live cost/accuracy scripts; section 9.3 extends them to coder-final charts. |
| P6: hardcoded pipeline | One function, `code_diagnosis(dx, chart_text, date_of_service)`, returning the engine's S4.3 `final_result` shape. |

---

## 6. What was built

| PR | Change | Measured effect |
|---|---|---|
| #1 | Regression test harness; known bugs tracked as strict expected failures | Baseline recorded |
| #2 | Index-backed code lookup, cached token statistics | Search went from ~12s to ~0.7s per query |
| #3 | All 7th-character codes generated from the CMS order file | Billable codes 23,501 → 74,879 (exact match with CMS); `S72.001A`, `T40.1X1A`, `S01.00XA` reachable |
| #4 | Excludes notes no longer boost the code that excludes them; Index terms matched with their parent path | "pain in right hip" → M25.551 (was R52) |
| #5 | Laterality/encounter filter based on conflicts; structured inputs from the engine | "bright" ≠ "right"; a documented side removes the unspecified-side code |
| #6 | An Index hit must match the term itself, not only its path | Heroin poisoning no longer ranked as shellfish poisoning |
| #7 | Resolution loop: category staging, "not documented", quote checks, confidence | No top-20 cut-off; unsupported guesses become flagged results |
| #8 | FY2026 + FY2027 code sets, chosen by date of service | Correct code on each side of 2026-10-01 (e.g. I42.0) |
| #9 | `code_diagnosis()` engine interface; model calls routable through the host's LLM wrapper | Drop-in `final_result` shape; runs in the engine's thread pool |

---

## 7. Results

### 7.1 Automated tests

**88 passed, 0 failed, 2 expected failures** (known vocabulary gaps, below), with live model tests enabled.

The suite covers:
- search regression cases;
- code-set integrity against the CMS order file for both fiscal years;
- the fiscal-year boundary;
- Excludes and laterality rules;
- the resolution loop with stubbed models, including concurrency;
- the engine interface contract;
- live question-generation quality.

### 7.2 Search benchmark

`scripts/benchmark_search.py`: 300 Alphabetical Index paths and 300 Tabular titles, fixed seed. For each query, the expected result is the code that path or title leads to.

| Query set | Top-1: before → after | Found in top 20: before → after |
|---|---|---|
| Index paths | 41.7% → 69.0% | 73.0% → 89.0% |
| Tabular titles | 90.0% → 94.0% | 98.7% → 98.7% |

*Caveat:* this measures consistency with the code set, not clinical accuracy. The Index-path set shares its source with the Index-path matching change (#4), so its gain is an upper bound; the Tabular set is independent of that change.

### 7.3 Running charts end to end (live models)

Each case passes an engine-style diagnosis dict and the note text to `code_diagnosis()`, using Haiku 4.5 for questions and Opus 5 for answers.

| # | Note | Diagnosis passed in | Expected | Result | Questions | Time |
|---|---|---|---|---|---|---|
| 1 | Sample 01: acute diverticulitis with GI bleeding | "acute diverticulitis of large intestine … with bleeding" | K57.33 | ✅ K57.33 | 4 | 21.5s |
| 2 | Sample 02: diverticulosis, no bleeding | "diverticulosis of large intestine … without bleeding" | K57.30 | ✅ K57.30 | 0 | 0.3s |
| 3 | Sample 03: right hip pain | `pain` + location `hip` + laterality `Right` | M25.551 | ✅ M25.551 | 1 | 6.0s |
| 4 | Sample: biopsy-confirmed NASH | "nonalcoholic steatohepatitis" | K75.81 | ✅ K75.81 | 0 | 0.0s |
| 5 | Fracture follow-up, healing (encounter **not** passed in) | "fracture of neck of right femur" | S72.001D | ✅ S72.001D (7th character read from the note) | 6 | 27.2s |
| 6 | Knee pain, side not documented | "knee pain" | M25.569 | ✅ M25.569 | 2 | 18.2s |
| 7 | T2DM with diabetic CKD | "type 2 diabetes mellitus with diabetic CKD" | E11.22 | ✅ E11.22 | 0 | 0.4s |
| 8 | Accidental heroin overdose | "poisoning by heroin, accidental", encounter initial | T40.1X1A | ✅ T40.1X1A | 0 | 0.5s |
| 9 | Note 3 dated 2026-10-05 | "pain in right hip" | M25.551 (FY2027) | ✅ M25.551, FY2027 code set | 1 | 6.7s |

**9/9 correct.** Every quote the answer model gave was found word for word in its note.

*What this does and doesn't show.* It shows the pipeline works end to end with real models, across:
- 7th characters;
- laterality given as a structured field;
- undocumented detail;
- combination codes;
- poisoning;
- the fiscal-year switch.

It is **not** an accuracy measurement. Four notes are the repository samples and five were written for this test.

### 7.4 Known limitations

- **Vocabulary gaps** (tracked as expected test failures): "ingrown" does not reach "Ingrowing nail" (L60.0), and "femoral neck" does not reach femur codes.
- **Tables not loaded:** the Table of Drugs and Chemicals, the Neoplasm Table and the External Cause Index. Poisoning and neoplasm codes are reached through Tabular titles only.
- **One diagnosis per call.** Code-first / use-additional pairing and sequencing remain the engine's coding-guidelines stage, which is where they belong in the integrated design.
- **Latency:** about 5–6 seconds per question, so a diagnosis that needs 4–6 questions takes 20–30 seconds.

---

## 8. Cost

Token counts come from the API's own usage report for every call, priced at Anthropic list prices: Haiku 4.5 $1 / $5 and Opus 5 $5 / $25 per million input / output tokens. Thinking tokens are billed as output. There's no batch discount and no prompt caching.

| # | Case | Questions | Haiku (question) | Opus (answer) | **Total** |
|---|---|---|---|---|---|
| 1 | Diverticulitis with bleeding | 4 | 4 calls, 6,000 in / 494 out: $0.0085 | 4 calls, 2,909 / 361: $0.0236 | **$0.0320** |
| 2 | Diverticulosis | 0 | none | none | **$0.0000** |
| 3 | Right hip pain | 1 | 1,222 / 230: $0.0024 | 821 / 27: $0.0048 | **$0.0072** |
| 4 | NASH | 0 | none | none | **$0.0000** |
| 5 | Fracture follow-up (7th character) | 6 | 7,228 / 1,253: $0.0135 | 4,150 / 515: $0.0336 | **$0.0471** |
| 6 | Knee pain, side not documented | 2 | 2,134 / 296: $0.0036 | 1,187 / 846: $0.0271 | **$0.0307** |
| 7 | T2DM with CKD | 0 | none | none | **$0.0000** |
| 8 | Heroin poisoning | 0 | none | none | **$0.0000** |
| 9 | Hip pain, FY2027 | 1 | 1,222 / 240: $0.0024 | 824 / 102: $0.0067 | **$0.0091** |
| | **All 9** | | $0.0304 | $0.0958 | **$0.1261** |

- **Average per diagnosis:** $0.014 overall; $0.025 when questions are needed.
- **Estimated per chart:** around $0.07–$0.13, assuming about 5 diagnoses, 40% of which resolve with no questions.
- **Cost driver:** the Opus answer step is 76% of spend. Each extra question adds roughly $0.006–$0.013.
- **Levers, largest first:**
  1. Fewer questions per diagnosis (e.g. don't ask about laterality already stated in the phrase).
  2. Lower thinking effort on the answer step.
  3. A cheaper answer model, but only after an accuracy comparison.
- **Engine comparison:** the engine's current per-diagnosis ICD cost (three Sonnet 4.6 stages, plus RAG) has not been measured yet. It is part of the Phase 1 evaluation (section 9.3).

---

## 9. Integration plan with nextgen-codio-engine

**Scope.** Replace only the per-diagnosis code selection (S4.1 → S4.2 → S4.3).

- **Unchanged:** pre-processing (S0–S4 routing), diagnosis extraction (DXEX), and everything after selection: low-confidence and duplicate filters, injury pipeline, general coding guidelines, ICD↔CPT linking, sequencing and push.
- **Full technical contract:** `docs/INTEGRATION.md`.

### 9.1 Interface

```python
from search_engine import integration, llm

llm.set_backend(engine_llm_backend)      # route calls through call_llm_wrapper
final_result = integration.code_diagnosis(dx, chart_text, date_of_service)
```

- **Input:** the engine's DXEX per-diagnosis dict: `phrase`, plus optional `anatomical_location` / `location`, `laterality`, `encounter` / `episode_of_care`.
- **Output:** the S4.3 `final_result` keys (`icd_code`, `icd_code_desc`, `node_id`, `confidence`, `seventh_character_unresolved`) plus `status`, `code_set` and an audit `trace`.

### 9.2 Engine changes

| Step | Change | Engine location |
|---|---|---|
| 1 | Config switch `icd_selection.mode = "tree" \| "icd_coder"`, default `"tree"` | bundle `MODULE_CONFIG` (`client_configs/<bundle>/config.py`) |
| 2 | With `"icd_coder"`: skip batched S4.1/S4.2; in `_process_diagnosis` call `code_diagnosis()` and store the result as `cr["final_result"]` (already runs in the thread pool) | `icd_main.py` (`run_icd_pipeline`, `_process_diagnosis`) |
| 3 | Skip the 7th-character merge for these results (the code is already complete); keep the C23 drop when `seventh_character_unresolved` | `icd_main.py:600–621` |
| 4 | Map `icd_code` → `node_id` via `node_map`; a missing code is a code-set mismatch, so log it as an error | `icd_main.py` aggregation |
| 5 | LLM backend adapter over `call_llm_wrapper` (fallback, record/replay, cost tracking); register both prompts in the prompt engine | `general_modules/llm/wrapper.py`, `prompts/registry.py` |
| 6 | Apply the S2 confidence ceiling as `min(confidence, ceiling)` | aggregation, before the low-confidence filter |
| 7 | Store the trace in `tree_traversal.chapter_routing[i]` for `/code-audit`, `block_sim`, icd-detective | `icd_main.py` |
| 8 | Package icd-coder (library + two SQLite code-set files, built from `data/raw/`); no service to run | deployment |

### 9.3 Phased rollout

| Phase | What | Exit criterion |
|---|---|---|
| **0. Evaluation harness** | Pull finished encounters with coder-final codes from the PE API (`qa/scripts/coding_accuracy_diff.py` already reads them). Store DXEX phrases and coder codes as a gold set of at least 200 gastro encounters plus general ones. | Gold set frozen |
| **1. Offline head-to-head** | Run the same DXEX phrases through the tree and icd-coder. Compare exact code, 3-character category, no-code rate, off-shortlist rate, rounds, latency and cost. Report injury/7th-character and gastro-shortlist cases separately. | icd-coder ≥ tree on exact match, with no regression on any reported slice |
| **2. Engine wiring** | Engine changes 1–8 behind the flag, default `"tree"`. Parity tests with record/replay. | Flag off = byte-identical output; flag on = passes the Phase 1 set |
| **3. Shadow mode** | Both selectors run on live charts, and only the tree result is pushed. Diffs go to `/code-audit`. | 2 weeks with no unexplained regressions; coder review of the diffs |
| **4. Switch per bundle** | Turn on `"icd_coder"` for one bundle (e.g. gastro_op), monitor coder edit rate, then expand. | Coder edit rate ≤ baseline |

### 9.4 Prerequisites and open items

- **Code-set alignment:** confirm which fiscal year `icd_kb_preview.xlsx` covers, so `node_id` mapping and comparisons are like-for-like.
- **Gastro shortlist:** for a fair Phase 1, either disable `icd_Kb_gastro_54.csv` for the tree run or filter both outputs against it.
- **Before Phase 3:**
  - load the Drug, Neoplasm and External Cause tables;
  - cut the number of questions per diagnosis (latency and cost);
  - close the vocabulary gaps.
- **Ownership:** decide whether icd-coder is vendored into the engine repository or imported as a pinned package.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Accuracy on real charts is unknown | Phases 0–1 run before any switch; default stays `"tree"` |
| Latency with 4–6 questions | Question-reduction work before shadow mode; diagnoses already run in parallel |
| Cost at volume | Measured per call; levers in section 8; compare with the tree's cost in Phase 1 |
| Vocabulary misses on colloquial phrases | DXEX phrases are usually clinical; known-issue cases tracked; synonym and Index improvements |
| Code-set drift between the engine KB and CMS | `node_id` mapping errors are logged; one fiscal-year build per release |
| Model/provider change | All calls go through one replaceable backend (`llm.set_backend`) |

---

## 11. Reproducing these results

```bash
cd icd-coder
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # or .venv/bin on macOS/Linux
python scripts/build_db.py                          # builds data/icd10_fy2026.db and data/icd10_fy2027.db
python -m pytest                                    # offline suite
ICD_CODER_LIVE_TESTS=1 python -m pytest             # + live model tests (needs .env key)
python scripts/benchmark_search.py --n 300          # search benchmark
```

`.env` needs `ANTHROPIC_API_KEY` (a workspace-scoped key), `CLAUDE_MODEL_FAST=claude-haiku-4-5-20251001` and `CLAUDE_MODEL_REASONING=claude-opus-5`.

Architecture: `docs/ARCHITECTURE.md`. Engine contract: `docs/INTEGRATION.md`.
