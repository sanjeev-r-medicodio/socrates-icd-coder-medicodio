"""ICD-10-CM deterministic search: search(diagnosis) -> SearchResult.

Pipeline: strip laterality/encounter-type filter keywords -> expand synonyms ->
FTS5 search (Tabular titles, Index terms, note_references) -> resolve each hit to
billable leaf code(s) (expanding non-billable headers and dash-suffixed Index
entries to their billable descendants) -> apply laterality/encounter post-filter ->
re-rank (exact-match boost) -> single-vs-list decision.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "icd10.db"

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

EXACT_MATCH_BOOST = 100.0          # added when the query text matches a title/term near-verbatim
TABULAR_TITLE_WEIGHT = 3.0         # weight for Tabular short_title/long_desc FTS hits
INDEX_TERM_WEIGHT = 2.5            # weight for Alphabetical Index term FTS hits
NOTE_REFERENCE_WEIGHT = 1.0        # weight for reverse note-reference FTS hits (lowest priority)
CROSS_REFERENCE_WEIGHT_MULTIPLIER = 0.6   # discount applied to one-hop see/seeAlso follow-ups
SYNONYM_ONLY_MULTIPLIER = 0.8      # discount when a hit only matched via synonym expansion, not raw query text
SINGLE_RESULT_MARGIN_RATIO = 1.4   # top score must exceed 2nd-best by this ratio to auto-return a single result
MAX_RESULTS = 20
FTS_CANDIDATE_LIMIT = 200          # how many raw FTS rows to pull per source table before aggregation
GENERIC_TOKEN_DOC_FREQ_RATIO = 0.03  # tokens matching >3% of all diag rows are dropped from the OR query as noise

LATERALITY_PATTERNS = {
    "bilateral": r"\bbilateral\b",
    "left": r"\bleft\b",
    "right": r"\bright\b",
}
ENCOUNTER_PATTERNS = {
    "initial": r"\binitial(?:\s+encounter)?\b",
    "subsequent": r"\bsubsequent(?:\s+encounter)?\b",
    "sequela": r"\bsequela[e]?\b",
}

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
FTS_SPECIAL_RE = re.compile(r'["*^]')


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CodeResult:
    code: str
    code_formatted: str
    description: str
    short_title: Optional[str]
    score: float
    reasons: list = field(default_factory=list)


@dataclass
class SearchResult:
    query: str
    laterality_filter: Optional[str]
    encounter_filter: Optional[str]
    is_single: bool
    results: list  # List[CodeResult]


# internal accumulator: not exposed outside search()
#
# Each *source* (tabular / index / cross-reference / notes / exact-match-boost)
# contributes at most its single best hit per code -- a category header, its
# subcategory, and a leaf's own title can all independently match the same
# generic word (e.g. "pain") and would otherwise stack additively across every
# billable descendant, drowning out a leaf that matches multiple distinct query
# terms in one place. Distinct *sources* still add together: that convergence
# (title hit + index hit + note reference, say) is the real ranking signal.
@dataclass
class _Candidate:
    source_scores: dict = field(default_factory=dict)  # source_key -> best score seen
    reasons: list = field(default_factory=list)

    def add(self, source_key: str, weight: float, reason: str):
        if weight > self.source_scores.get(source_key, float("-inf")):
            self.source_scores[source_key] = weight
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def score(self) -> float:
        return sum(self.source_scores.values())


# ---------------------------------------------------------------------------
# Step 1-2: tokenize, extract filters
# ---------------------------------------------------------------------------

def _extract_filters(query: str):
    laterality = None
    for name, pattern in LATERALITY_PATTERNS.items():
        m = re.search(pattern, query, re.IGNORECASE)
        if m:
            laterality = name
            query = query[: m.start()] + query[m.end():]
            break

    encounter = None
    for name, pattern in ENCOUNTER_PATTERNS.items():
        m = re.search(pattern, query, re.IGNORECASE)
        if m:
            encounter = name
            query = query[: m.start()] + query[m.end():]
            break

    stripped = re.sub(r"\s+", " ", query).strip()
    return laterality, encounter, stripped


def _tokenize(text: str):
    return [t.lower() for t in TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# Step 3: synonym expansion
# ---------------------------------------------------------------------------

def _expand_synonyms(conn, tokens):
    """Returns (expansion_tokens, expansion_reasons). Query expansion only -- FTS index is untouched."""
    expansion_tokens = []
    reasons = []
    seen_canonical = set()
    for tok in tokens:
        row = conn.execute(
            "SELECT shorthand, canonical_term FROM synonyms WHERE lower(shorthand) = ?", (tok,)
        ).fetchone()
        if row and row[1].lower() not in seen_canonical:
            seen_canonical.add(row[1].lower())
            expansion_tokens.extend(_tokenize(row[1]))
            reasons.append(f"Synonym expansion: '{row[0]}' -> '{row[1]}'")
    return expansion_tokens, reasons


# ---------------------------------------------------------------------------
# Step 4: FTS5 query construction + execution
# ---------------------------------------------------------------------------

def _fts_escape(token: str) -> str:
    return FTS_SPECIAL_RE.sub("", token)


def _build_match_query(tokens):
    terms = [f'"{_fts_escape(t)}"' for t in tokens if t]
    return " OR ".join(terms)


def _drop_generic_tokens(conn, tokens):
    """Drops tokens matching an unusually large fraction of all diag rows
    (e.g. 'with', 'right', 'lower') before they go into an OR query.

    These add topic-drift noise far more than search signal: in an OR query,
    a row matching several such generic tokens together (e.g. an unrelated
    "acquired ... right ... lower ..." title) can out-score a row matching one
    truly specific term (e.g. "pneumonia") if the generic terms are left in,
    since bm25 rewards matching more distinct terms. Never drops down to an
    empty token list -- if everything looks generic, keep the original set
    rather than search on nothing."""
    if len(tokens) <= 1:
        return tokens
    total = conn.execute("SELECT COUNT(*) FROM tabular_codes WHERE node_type = 'diag'").fetchone()[0]
    if not total:
        return tokens
    specific = []
    for t in tokens:
        df = conn.execute(
            "SELECT COUNT(*) FROM fts_tabular WHERE fts_tabular MATCH ?", (f'"{_fts_escape(t)}"',)
        ).fetchone()[0]
        if df / total <= GENERIC_TOKEN_DOC_FREQ_RATIO:
            specific.append(t)
    return specific or tokens


def _search_tabular(conn, match_query, limit=FTS_CANDIDATE_LIMIT):
    if not match_query:
        return []
    return conn.execute(
        """SELECT tc.code, tc.node_type, tc.is_billable, tc.long_desc, tc.short_title,
                  tc.code_formatted, bm25(fts_tabular) AS rank
           FROM fts_tabular ft JOIN tabular_codes tc ON tc.code = ft.code
           WHERE fts_tabular MATCH ? AND tc.node_type = 'diag'
           ORDER BY rank LIMIT ?""",
        (match_query, limit),
    ).fetchall()


def _search_index_terms(conn, match_query, limit=FTS_CANDIDATE_LIMIT):
    if not match_query:
        return []
    return conn.execute(
        """SELECT it.id, it.code, it.code_raw, it.term_text, it.xref_type, it.xref_target,
                  it.manif_code, it.level, bm25(fts_index_terms) AS rank
           FROM fts_index_terms fit JOIN index_terms it ON it.id = fit.term_id
           WHERE fts_index_terms MATCH ?
           ORDER BY rank LIMIT ?""",
        (match_query, limit),
    ).fetchall()


def _search_note_references(conn, match_query, limit=FTS_CANDIDATE_LIMIT):
    if not match_query:
        return []
    return conn.execute(
        """SELECT nr.code, nr.note_type, nr.condition_text, bm25(fts_note_references) AS rank
           FROM fts_note_references fnr JOIN note_references nr ON nr.id = fnr.ref_id
           WHERE fts_note_references MATCH ?
           ORDER BY rank LIMIT ?""",
        (match_query, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Resolve any tabular_codes.code into its billable leaf code(s)
# ---------------------------------------------------------------------------

def _resolve_to_billable(conn, code_or_prefix: str):
    row = conn.execute(
        "SELECT code, is_billable FROM tabular_codes WHERE code = ?", (code_or_prefix,)
    ).fetchone()
    if row and row[1] == 1:
        return [code_or_prefix]
    cur = conn.execute(
        "SELECT code FROM tabular_codes WHERE code LIKE ? AND is_billable = 1",
        (code_or_prefix + "%",),
    )
    return [r[0] for r in cur.fetchall()]


def _fetch_code_display(conn, code: str):
    row = conn.execute(
        "SELECT code_formatted, long_desc, short_title FROM tabular_codes WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        return None
    return {"code_formatted": row[0], "long_desc": row[1] or "", "short_title": row[2]}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def search(diagnosis: str, db_path: Path = DB_PATH, conn: sqlite3.Connection = None) -> SearchResult:
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)

    try:
        return _search_impl(diagnosis, conn)
    finally:
        if owns_conn:
            conn.close()


def _search_impl(diagnosis: str, conn: sqlite3.Connection) -> SearchResult:
    original_query = diagnosis
    laterality, encounter, stripped_query = _extract_filters(diagnosis)

    raw_tokens = _tokenize(stripped_query)
    expansion_tokens, synonym_reasons = _expand_synonyms(conn, raw_tokens)
    all_tokens = raw_tokens + [t for t in expansion_tokens if t not in raw_tokens]
    match_tokens = _drop_generic_tokens(conn, all_tokens)

    match_query = _build_match_query(match_tokens)

    candidates: dict[str, _Candidate] = {}

    def add_hit(billable_code, source_key, weight, reason):
        cand = candidates.setdefault(billable_code, _Candidate())
        cand.add(source_key, weight, reason)

    # --- Tabular title/description hits ---
    for code, node_type, is_billable, long_desc, short_title, code_formatted, rank in _search_tabular(conn, match_query):
        score = -rank * TABULAR_TITLE_WEIGHT
        for billable_code in _resolve_to_billable(conn, code):
            reason = f"Tabular title match: '{short_title or long_desc}'"
            if is_billable != 1:
                reason += f" (expanded from category {code_formatted})"
            add_hit(billable_code, "tabular", score, reason)

    # --- Index term hits (with dash-expansion / cross-reference follow) ---
    for term_id, code, code_raw, term_text, xref_type, xref_target, manif_code, level, rank in _search_index_terms(conn, match_query):
        base_score = -rank * INDEX_TERM_WEIGHT
        # A single bare word matched at a non-root level (e.g. "acquired" as a
        # qualifier subterm appearing under dozens of unrelated main terms
        # like "clubfoot, acquired" or "myopia, acquired") only means
        # something combined with its parent term -- the Index doesn't store
        # that parent context in this row's own term_text, so following its
        # cross-reference on the bare word alone is untrustworthy: it treats
        # a coincidental one-word collision as if it meant the same thing as
        # the query. A true root main term (level 0) or a genuine multi-word
        # phrase match is real signal either way.
        is_meaningful_standalone = level == 0 or len(_tokenize(term_text)) > 1

        if code:
            for billable_code in _resolve_to_billable(conn, code):
                add_hit(billable_code, "index", base_score, f"Index term match: '{term_text}'")
        elif code_raw and code_raw.endswith("-"):
            prefix = code_raw.rstrip("-").replace(".", "")
            for billable_code in _resolve_to_billable(conn, prefix):
                add_hit(
                    billable_code, "index", base_score,
                    f"Index term match: '{term_text}' (expanded, requires additional character)",
                )
        elif xref_type in ("see", "seeAlso") and xref_target and is_meaningful_standalone:
            xref_query = _build_match_query(_drop_generic_tokens(conn, _tokenize(xref_target)))
            for xcode, _nt, _bill, xlong, xshort, xfmt, xrank in _search_tabular(conn, xref_query, limit=20):
                # The destination re-search can score arbitrarily high on its
                # own terms (e.g. a long xref_target phrase overlapping
                # heavily with its own destination title) -- completely
                # independent of how weak the original trigger match was. A
                # cross-reference hop must never be allowed to outweigh the
                # match that triggered it, or a two-word coincidental Index
                # overlap can unlock a score stronger than any direct match.
                xscore = min(-xrank * TABULAR_TITLE_WEIGHT * CROSS_REFERENCE_WEIGHT_MULTIPLIER, base_score)
                for billable_code in _resolve_to_billable(conn, xcode):
                    add_hit(
                        billable_code, "xref", xscore,
                        f"Cross-reference: '{term_text}' see '{xref_target}' -> '{xshort or xlong}'",
                    )

        if manif_code:
            for billable_code in _resolve_to_billable(conn, manif_code):
                add_hit(billable_code, "index", base_score, f"Index term manifestation code for '{term_text}'")

    # --- Reverse note-reference hits ---
    for code, note_type, condition_text, rank in _search_note_references(conn, match_query):
        score = -rank * NOTE_REFERENCE_WEIGHT
        for billable_code in _resolve_to_billable(conn, code):
            add_hit(billable_code, "notes", score, f"Note reference ({note_type}): '{condition_text}'")

    if not candidates:
        return SearchResult(original_query, laterality, encounter, is_single=False, results=[])

    # --- attach synonym-expansion provenance globally (applies to whole query, not per-code) ---
    for cand in candidates.values():
        for r in synonym_reasons:
            if r not in cand.reasons:
                cand.reasons.append(r)

    # --- exact / near-exact match re-rank boost ---
    normalized_query = stripped_query.strip().lower()
    if normalized_query:
        for code, cand in candidates.items():
            display = _fetch_code_display(conn, code)
            if display is None:
                continue
            title_texts = [
                (display["short_title"] or "").lower(),
                display["long_desc"].lower(),
            ]
            if any(normalized_query == t for t in title_texts):
                cand.add("exact", EXACT_MATCH_BOOST, "Exact title match")
            elif any(
                normalized_query in t
                # A single-word query is a substring of nearly any title that
                # merely mentions it (e.g. "hypertension" inside "Pre-existing
                # hypertension with pre-eclampsia, first trimester") -- that's
                # not remotely "near-exact" and boosting it flatly rewards
                # every candidate sharing that one word, which in practice
                # favors whichever code family happens to have the most
                # verbose/elaborate titles, not the closest match. Only
                # trust the substring check when the query itself is a real
                # multi-word phrase, or the matched title is short enough
                # that containing the query IS close to the whole title.
                and (len(normalized_query.split()) >= 2 or len(t.split()) <= 2)
                for t in title_texts if t
            ):
                cand.add("exact", EXACT_MATCH_BOOST * 0.3, "Near-exact title match")

    # --- laterality / encounter-type post-filter ---
    def passes_filter(code):
        display = _fetch_code_display(conn, code)
        if display is None:
            return False
        text = f"{display['short_title'] or ''} {display['long_desc']}".lower()
        if laterality and laterality not in text:
            return False
        if encounter and encounter not in text and (encounter != "initial" or "initial encounter" not in text):
            return False
        return True

    filtered_codes = [c for c in candidates if passes_filter(c)]
    # Only trust the filter if at least one candidate AT THE TOP SCORE
    # survives it -- not just an arbitrarily-chosen single "top" code, since
    # ties are common (e.g. right/left/bilateral/unspecified variants of the
    # same match often score identically) and max() picking one of several
    # tied codes at random must not decide whether the whole filter applies.
    # If NONE of the top-tied candidates survive, that's a sign the
    # laterality/encounter distinction doesn't actually apply to this
    # diagnosis family (e.g. plain pneumonia codes have no laterality
    # variant at all) and filtered_codes is non-empty only because of
    # unrelated lower-relevance noise that happens to mention the filter
    # word -- fall back to the unfiltered set instead.
    top_score = max((c.score for c in candidates.values()), default=None)
    top_tier_survives = any(candidates[c].score == top_score for c in filtered_codes)
    if (laterality or encounter) and filtered_codes and top_tier_survives:
        active_codes = filtered_codes
        for code in active_codes:
            if laterality:
                candidates[code].add("filter_laterality", 0.0, f"Laterality filter applied: {laterality}")
            if encounter:
                candidates[code].add("filter_encounter", 0.0, f"Encounter-type filter applied: {encounter}")
    else:
        active_codes = list(candidates.keys())

    # --- assemble, sort, decide single vs list ---
    scored = []
    for code in active_codes:
        display = _fetch_code_display(conn, code)
        if display is None:
            continue
        cand = candidates[code]
        scored.append(CodeResult(
            code=code,
            code_formatted=display["code_formatted"],
            description=display["long_desc"],
            short_title=display["short_title"],
            score=round(cand.score, 3),
            reasons=cand.reasons,
        ))

    scored.sort(key=lambda r: r.score, reverse=True)

    is_single = False
    if len(scored) == 1:
        is_single = True
    elif len(scored) > 1 and scored[1].score > 0 and scored[0].score >= scored[1].score * SINGLE_RESULT_MARGIN_RATIO:
        is_single = True
    elif len(scored) > 1 and scored[1].score <= 0 < scored[0].score:
        is_single = True

    results = scored[:1] if is_single else scored[:MAX_RESULTS]
    return SearchResult(original_query, laterality, encounter, is_single, results)
