#!/usr/bin/env python3
"""Idempotent ETL: CMS ICD-10-CM XML/order-file sources -> data/icd10.db"""
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "icd10.db"

TABULAR_XML = RAW / "icd10cm_tabular_2027.xml"
INDEX_XML = RAW / "icd10cm_index_2027.xml"
ORDER_FILE = RAW / "icd10cm_order_2027.txt"

NOTE_TAGS = [
    "includes", "excludes1", "excludes2", "codeFirst",
    "useAdditionalCode", "codeAlso", "inclusionTerm",
]
REVERSE_INDEX_NOTE_TYPES = {"codeFirst", "useAdditionalCode", "codeAlso", "excludes1", "excludes2"}

# matches a trailing parenthetical, e.g. "(T81.44-)", "(A00-A09)" -- linear, no nested quantifiers
TRAILING_PAREN_RE = re.compile(r"\(([^()]*)\)\s*$")
CODE_LOOKS_LIKE_RE = re.compile(r"^[A-Z][0-9A-Z.,\-\s]+$")
NEC_RE = re.compile(r"\bNEC\b")


def extract_trailing_code_ref(text: str):
    """Best-effort: trailing parenthetical that looks like a code/range, else None."""
    m = TRAILING_PAREN_RE.search(text)
    if not m:
        return None
    inner = m.group(1).strip()
    if inner and CODE_LOOKS_LIKE_RE.match(inner):
        return inner
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE tabular_codes (
    code                    TEXT PRIMARY KEY,
    code_formatted          TEXT,
    node_type               TEXT NOT NULL,   -- 'chapter' | 'section' | 'diag'
    parent_code             TEXT REFERENCES tabular_codes(code),
    chapter_num             TEXT,
    chapter_desc            TEXT,
    section_id              TEXT,
    section_desc            TEXT,
    short_title              TEXT,
    long_desc                TEXT,
    is_placeholder           INTEGER DEFAULT 0,
    is_header                INTEGER,
    is_billable              INTEGER,
    has_seventh_char          INTEGER DEFAULT 0,
    seventh_char_source_code  TEXT,
    seventh_char              TEXT,     -- set only on generated 7-character codes
    order_number              INTEGER
);
CREATE INDEX idx_tabular_parent ON tabular_codes(parent_code);

CREATE TABLE seventh_char_defs (
    id            INTEGER PRIMARY KEY,
    code          TEXT REFERENCES tabular_codes(code),
    char_value    TEXT,
    meaning       TEXT
);
CREATE INDEX idx_7ch_code ON seventh_char_defs(code);

CREATE TABLE tabular_notes (
    id            INTEGER PRIMARY KEY,
    code          TEXT REFERENCES tabular_codes(code),
    note_type     TEXT,
    note_text     TEXT,
    ref_code      TEXT
);
CREATE INDEX idx_notes_code ON tabular_notes(code);

CREATE TABLE index_terms (
    id              INTEGER PRIMARY KEY,
    parent_id       INTEGER REFERENCES index_terms(id),
    letter          TEXT,
    level           INTEGER,
    term_text       TEXT,
    nemod_text      TEXT,
    code_raw        TEXT,
    code            TEXT,
    manif_code      TEXT,
    xref_type       TEXT,
    xref_target     TEXT,
    is_nec          INTEGER DEFAULT 0
);
CREATE INDEX idx_index_terms_parent ON index_terms(parent_id);
CREATE INDEX idx_index_terms_code ON index_terms(code);

CREATE TABLE note_references (
    id             INTEGER PRIMARY KEY,
    code           TEXT REFERENCES tabular_codes(code),
    note_type      TEXT,
    condition_text TEXT
);
CREATE INDEX idx_note_refs_code ON note_references(code);

CREATE TABLE synonyms (
    id              INTEGER PRIMARY KEY,
    shorthand       TEXT,
    canonical_term  TEXT
);

CREATE VIRTUAL TABLE fts_tabular USING fts5(
    code UNINDEXED, short_title, long_desc, tokenize='porter'
);
CREATE VIRTUAL TABLE fts_index_terms USING fts5(
    term_id UNINDEXED, code UNINDEXED, term_text, tokenize='porter'
);
CREATE VIRTUAL TABLE fts_note_references USING fts5(
    ref_id UNINDEXED, code UNINDEXED, condition_text, tokenize='porter'
);
"""


# ---------------------------------------------------------------------------
# Order file
# ---------------------------------------------------------------------------

def parse_order_file(path: Path) -> dict:
    """Fixed-width per icd10OrderFiles.pdf (1-indexed positions -> 0-indexed slices)."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            order_number = int(line[0:5])
            code = line[6:13].strip()
            billable = line[14] == "1"
            short_desc = line[16:76].strip()
            long_desc = line[77:].strip()
            out[code] = {
                "order_number": order_number,
                "is_billable": 1 if billable else 0,
                "is_header": 0 if billable else 1,
                "short_title": short_desc,
                "long_desc": long_desc,
            }
    return out


# ---------------------------------------------------------------------------
# Tabular XML
# ---------------------------------------------------------------------------

def elem_text(elem, tag):
    child = elem.find(tag)
    if child is None:
        return None
    return "".join(child.itertext()).strip()


def extract_notes(elem, code, notes_out):
    for tag in NOTE_TAGS:
        for note_container in elem.findall(tag):
            for note in note_container.findall("note"):
                text = "".join(note.itertext()).strip()
                if not text:
                    continue
                ref_code = extract_trailing_code_ref(text)
                notes_out.append((code, tag, text, ref_code))


def extract_seventh_char(elem, code, sevenchar_out):
    sevenchr_def = elem.find("sevenChrDef")
    if sevenchr_def is None:
        return False
    for ext in sevenchr_def.findall("extension"):
        char_value = ext.get("char")
        meaning = "".join(ext.itertext()).strip()
        sevenchar_out.append((code, char_value, meaning))
    return True


def walk_diag(diag_elem, parent_code, chapter_num, chapter_desc, section_id, section_desc,
              codes_out, notes_out, sevenchar_out, order_data):
    name = elem_text(diag_elem, "name")
    if not name:
        return
    code = name.replace(".", "")
    desc = elem_text(diag_elem, "desc") or ""
    is_placeholder = 1 if diag_elem.get("placeholder") == "true" else 0

    od = order_data.get(code, {})
    row = {
        "code": code,
        "code_formatted": name,
        "node_type": "diag",
        "parent_code": parent_code,
        "chapter_num": chapter_num,
        "chapter_desc": chapter_desc,
        "section_id": section_id,
        "section_desc": section_desc,
        "short_title": od.get("short_title"),
        "long_desc": desc,
        "is_placeholder": is_placeholder,
        "is_header": od.get("is_header"),
        "is_billable": od.get("is_billable"),
        "has_seventh_char": 0,
        "seventh_char_source_code": None,
        "seventh_char": None,
        "order_number": od.get("order_number"),
    }
    codes_out[code] = row

    extract_notes(diag_elem, code, notes_out)
    has_own_7ch = extract_seventh_char(diag_elem, code, sevenchar_out)
    if has_own_7ch:
        row["has_seventh_char"] = 1
        row["seventh_char_source_code"] = code

    for child in diag_elem.findall("diag"):
        walk_diag(child, code, chapter_num, chapter_desc, section_id, section_desc,
                  codes_out, notes_out, sevenchar_out, order_data)


def parse_tabular(path: Path, order_data: dict):
    tree = ET.parse(path)
    root = tree.getroot()

    codes_out = {}
    notes_out = []
    sevenchar_out = []

    for chapter in root.findall("chapter"):
        chapter_num = elem_text(chapter, "name")
        chapter_desc = elem_text(chapter, "desc")
        chapter_code = f"CH:{chapter_num}"
        codes_out[chapter_code] = {
            "code": chapter_code,
            "code_formatted": None,
            "node_type": "chapter",
            "parent_code": None,
            "chapter_num": chapter_num,
            "chapter_desc": chapter_desc,
            "section_id": None,
            "section_desc": None,
            "short_title": None,
            "long_desc": chapter_desc,
            "is_placeholder": 0,
            "is_header": None,
            "is_billable": None,
            "has_seventh_char": 0,
            "seventh_char_source_code": None,
            "seventh_char": None,
            "order_number": None,
        }
        extract_notes(chapter, chapter_code, notes_out)

        for section in chapter.findall("section"):
            section_id = section.get("id")
            section_desc = elem_text(section, "desc")
            # Prefixed key: some sections span a single category and share that
            # category's exact code (e.g. section id "B10" == diag code "B10"),
            # which would otherwise collide with the diag row in this dict.
            section_code = f"SEC:{section_id}"
            codes_out[section_code] = {
                "code": section_code,
                "code_formatted": None,
                "node_type": "section",
                "parent_code": chapter_code,
                "chapter_num": chapter_num,
                "chapter_desc": chapter_desc,
                "section_id": section_id,
                "section_desc": section_desc,
                "short_title": None,
                "long_desc": section_desc,
                "is_placeholder": 0,
                "is_header": None,
                "is_billable": None,
                "has_seventh_char": 0,
                "seventh_char_source_code": None,
                "seventh_char": None,
                "order_number": None,
            }
            extract_notes(section, section_code, notes_out)

            for diag in section.findall("diag"):
                walk_diag(diag, section_code, chapter_num, chapter_desc, section_id, section_desc,
                          codes_out, notes_out, sevenchar_out, order_data)

    # propagate 7th-char applicability down to descendants that don't define their own
    for code, row in codes_out.items():
        if row["node_type"] != "diag" or row["has_seventh_char"]:
            continue
        cur = row
        hops = 0
        while cur.get("parent_code"):
            hops += 1
            if hops > 20:
                raise RuntimeError(f"parent_code cycle detected walking up from {code!r}")
            parent = codes_out.get(cur["parent_code"])
            if parent is None:
                break
            if parent["node_type"] == "diag" and parent["seventh_char_source_code"]:
                row["has_seventh_char"] = 1
                row["seventh_char_source_code"] = parent["seventh_char_source_code"]
                break
            cur = parent

    return codes_out, notes_out, sevenchar_out


def add_seventh_char_codes(codes_out: dict, sevenchar_out: list, order_data: dict) -> int:
    """Adds the full 7-character codes (e.g. S72.001A, T40.1X1A, S01.00XA).

    The Tabular XML only lists the stem (S72.001) plus a sevenChrDef table; the
    billable codes themselves exist only in the order file. Each missing
    billable order-file code is attached under its longest existing prefix
    (the stem) and inherits that stem's chapter/section and 7th-char source.
    Any characters between the stem and the 7th character must be the
    placeholder 'X'. Fails the build on anything that doesn't fit that shape,
    so a code-set change can't silently drop codes again."""
    valid_chars = {}
    for src, char_value, _meaning in sevenchar_out:
        valid_chars.setdefault(src, set()).add(char_value)

    added = 0
    for code, od in order_data.items():
        if code in codes_out or not od["is_billable"]:
            continue
        stem = code[:-1]
        while stem and stem not in codes_out:
            stem = stem[:-1]
        parent = codes_out.get(stem)
        if parent is None or parent["node_type"] != "diag" or len(code) != 7:
            raise RuntimeError(f"order-file code {code!r} has no Tabular stem")
        source = parent["seventh_char_source_code"]
        padding = code[len(stem):-1]
        if not source or code[-1] not in valid_chars.get(source, ()) or set(padding) - {"X"}:
            raise RuntimeError(
                f"order-file code {code!r} doesn't fit stem {stem!r} "
                f"(7th-char source {source!r}, padding {padding!r})"
            )
        codes_out[code] = {
            **parent,
            "code": code,
            "code_formatted": f"{code[:3]}.{code[3:]}",
            "parent_code": stem,
            "short_title": od["short_title"],
            "long_desc": od["long_desc"],
            "is_placeholder": 1 if padding else 0,
            "is_header": 0,
            "is_billable": 1,
            "seventh_char": code[-1],
            "order_number": od["order_number"],
        }
        added += 1
    return added


# ---------------------------------------------------------------------------
# Index XML
# ---------------------------------------------------------------------------

XREF_TAGS = ["see", "seeAlso", "seecat", "subcat"]


def clean_index_code(raw: str):
    """Returns (code_raw, code) where code is None if unresolvable (dash-suffixed)."""
    raw = raw.strip()
    if raw.endswith("-"):
        return raw, None
    return raw, raw.replace(".", "")


def walk_term(term_elem, parent_id, letter, level, terms_out):
    title_elem = term_elem.find("title")
    if title_elem is None:
        return
    term_text = (title_elem.text or "").strip()
    nemod_elem = title_elem.find("nemod")
    nemod_text = "".join(nemod_elem.itertext()).strip() if nemod_elem is not None else None

    code_elem = term_elem.find("code")
    code_raw = code_elem.text.strip() if code_elem is not None and code_elem.text else None
    code = None
    if code_raw:
        code_raw, code = clean_index_code(code_raw)

    manif_elem = term_elem.find("manif")
    manif_code = manif_elem.text.strip() if manif_elem is not None and manif_elem.text else None

    xref_type = None
    xref_target = None
    for tag in XREF_TAGS:
        el = term_elem.find(tag)
        if el is not None and el.text:
            xref_type = tag
            xref_target = el.text.strip()
            break

    is_nec = 1 if NEC_RE.search(term_text) else 0

    this_id = len(terms_out) + 1
    terms_out.append({
        "id": this_id,
        "parent_id": parent_id,
        "letter": letter,
        "level": level,
        "term_text": term_text,
        "nemod_text": nemod_text,
        "code_raw": code_raw,
        "code": code,
        "manif_code": manif_code,
        "xref_type": xref_type,
        "xref_target": xref_target,
        "is_nec": is_nec,
    })

    for child in term_elem.findall("term"):
        child_level = int(child.get("level", level + 1))
        walk_term(child, this_id, letter, child_level, terms_out)


def parse_index(path: Path):
    tree = ET.parse(path)
    root = tree.getroot()
    terms_out = []

    for letter_elem in root.findall("letter"):
        letter_title_elem = letter_elem.find("title")
        letter = (letter_title_elem.text or "").strip() if letter_title_elem is not None else None
        for main_term in letter_elem.findall("mainTerm"):
            walk_term(main_term, None, letter, 0, terms_out)

    return terms_out


# ---------------------------------------------------------------------------
# Synonyms seed data
# ---------------------------------------------------------------------------

SYNONYM_SEED = [
    ("MI", "myocardial infarction"), ("CHF", "congestive heart failure"),
    ("COPD", "chronic obstructive pulmonary disease"), ("HTN", "hypertension"),
    ("T2DM", "type 2 diabetes mellitus"), ("T1DM", "type 1 diabetes mellitus"),
    ("URI", "upper respiratory infection"), ("UTI", "urinary tract infection"),
    ("GERD", "gastroesophageal reflux disease"), ("CKD", "chronic kidney disease"),
    ("CVA", "cerebrovascular accident"), ("AFib", "atrial fibrillation"),
    ("AF", "atrial fibrillation"), ("DVT", "deep vein thrombosis"),
    ("PE", "pulmonary embolism"), ("COPD", "chronic obstructive pulmonary disease"),
    ("ESRD", "end stage renal disease"), ("CAD", "coronary artery disease"),
    ("CABG", "coronary artery bypass graft"), ("PVD", "peripheral vascular disease"),
    ("PAD", "peripheral artery disease"), ("OSA", "obstructive sleep apnea"),
    ("OA", "osteoarthritis"), ("RA", "rheumatoid arthritis"),
    ("IBS", "irritable bowel syndrome"), ("IBD", "inflammatory bowel disease"),
    ("GI", "gastrointestinal"), ("MS", "multiple sclerosis"),
    ("PD", "Parkinson's disease"), ("AD", "Alzheimer's disease"),
    ("TIA", "transient ischemic attack"), ("ARDS", "acute respiratory distress syndrome"),
    ("ARF", "acute renal failure"), ("AKI", "acute kidney injury"),
    ("BPH", "benign prostatic hyperplasia"), ("ADHD", "attention deficit hyperactivity disorder"),
    ("OCD", "obsessive compulsive disorder"), ("PTSD", "post traumatic stress disorder"),
    ("GAD", "generalized anxiety disorder"), ("MDD", "major depressive disorder"),
    ("SAD", "seasonal affective disorder"), ("HLD", "hyperlipidemia"),
    ("DM", "diabetes mellitus"), ("DKA", "diabetic ketoacidosis"),
    ("HHS", "hyperosmolar hyperglycemic state"), ("NASH", "nonalcoholic steatohepatitis"),
    ("NAFLD", "nonalcoholic fatty liver disease"), ("PUD", "peptic ulcer disease"),
    ("UC", "ulcerative colitis"), ("CD", "Crohn's disease"),
    ("RSV", "respiratory syncytial virus"), ("TB", "tuberculosis"),
    ("HIV", "human immunodeficiency virus"), ("AIDS", "acquired immunodeficiency syndrome"),
    ("STI", "sexually transmitted infection"), ("STD", "sexually transmitted disease"),
    ("HSV", "herpes simplex virus"), ("HPV", "human papillomavirus"),
    ("HBV", "hepatitis B virus"), ("HCV", "hepatitis C virus"),
    ("PID", "pelvic inflammatory disease"), ("PCOS", "polycystic ovary syndrome"),
    ("BV", "bacterial vaginosis"), ("EDD", "estimated date of delivery"),
    ("SAB", "spontaneous abortion"), ("PPH", "postpartum hemorrhage"),
    ("PIH", "pregnancy induced hypertension"), ("GDM", "gestational diabetes mellitus"),
    ("IUGR", "intrauterine growth restriction"), ("NICU", "neonatal intensive care unit"),
    ("RDS", "respiratory distress syndrome"), ("SIDS", "sudden infant death syndrome"),
    ("ASD", "atrial septal defect"), ("VSD", "ventricular septal defect"),
    ("PDA", "patent ductus arteriosus"), ("TOF", "tetralogy of Fallot"),
    ("CP", "cerebral palsy"), ("SCI", "spinal cord injury"),
    ("TBI", "traumatic brain injury"), ("LOC", "loss of consciousness"),
    ("GCS", "Glasgow Coma Scale"), ("ICP", "intracranial pressure"),
    ("SAH", "subarachnoid hemorrhage"), ("ICH", "intracerebral hemorrhage"),
    ("SDH", "subdural hematoma"), ("EDH", "epidural hematoma"),
    ("ALS", "amyotrophic lateral sclerosis"), ("MG", "myasthenia gravis"),
    ("GBS", "Guillain-Barre syndrome"), ("CTS", "carpal tunnel syndrome"),
    ("DJD", "degenerative joint disease"), ("DDD", "degenerative disc disease"),
    ("HNP", "herniated nucleus pulposus"), ("ORIF", "open reduction internal fixation"),
    ("THA", "total hip arthroplasty"), ("TKA", "total knee arthroplasty"),
    ("AAA", "abdominal aortic aneurysm"), ("MVP", "mitral valve prolapse"),
    ("MR", "mitral regurgitation"), ("AS", "aortic stenosis"),
    ("AR", "aortic regurgitation"), ("VT", "ventricular tachycardia"),
    ("VF", "ventricular fibrillation"), ("SVT", "supraventricular tachycardia"),
    ("PVC", "premature ventricular contraction"), ("NSTEMI", "non ST elevation myocardial infarction"),
    ("STEMI", "ST elevation myocardial infarction"), ("ACS", "acute coronary syndrome"),
    ("PCI", "percutaneous coronary intervention"), ("ICD", "implantable cardioverter defibrillator"),
    ("PPM", "permanent pacemaker"), ("BBB", "bundle branch block"),
    ("RBBB", "right bundle branch block"), ("LBBB", "left bundle branch block"),
    ("LVH", "left ventricular hypertrophy"), ("LVEF", "left ventricular ejection fraction"),
    ("EF", "ejection fraction"), ("PFO", "patent foramen ovale"),
    ("COVID", "coronavirus disease"), ("ARF", "acute respiratory failure"),
    ("CO2", "carbon dioxide"), ("O2", "oxygen"),
    ("SOB", "shortness of breath"), ("DOE", "dyspnea on exertion"),
    ("PND", "paroxysmal nocturnal dyspnea"), ("CXR", "chest x-ray"),
    ("PNA", "pneumonia"), ("URI", "upper respiratory infection"),
    ("OME", "otitis media with effusion"), ("AOM", "acute otitis media"),
    ("CRS", "chronic rhinosinusitis"), ("GERD", "gastroesophageal reflux disease"),
    ("HTN", "hypertension"), ("HH", "hiatal hernia"),
    ("HD", "hemodialysis"), ("PD", "peritoneal dialysis"),
    ("BPH", "benign prostatic hyperplasia"), ("ED", "erectile dysfunction"),
    ("OAB", "overactive bladder"), ("SUI", "stress urinary incontinence"),
    ("BMI", "body mass index"), ("DM2", "type 2 diabetes mellitus"),
    ("HbA1c", "hemoglobin A1c"), ("SLE", "systemic lupus erythematosus"),
    ("RA", "rheumatoid arthritis"), ("Gout", "gout"),
    ("OI", "osteogenesis imperfecta"), ("OP", "osteoporosis"),
    ("Fx", "fracture"), ("Dx", "diagnosis"),
    ("Hx", "history"), ("Sx", "symptoms"),
    ("Tx", "treatment"), ("Rx", "prescription"),
]


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    print("Parsing order file...")
    order_data = parse_order_file(ORDER_FILE)
    print(f"  {len(order_data)} order-file entries")

    print("Parsing tabular XML...")
    codes_out, notes_out, sevenchar_out = parse_tabular(TABULAR_XML, order_data)
    print(f"  {len(codes_out)} tabular_codes rows, {len(notes_out)} notes, {len(sevenchar_out)} 7th-char defs")

    print("Adding 7-character codes from the order file...")
    added = add_seventh_char_codes(codes_out, sevenchar_out, order_data)
    print(f"  {added} 7-character codes added")

    print("Parsing index XML...")
    terms_out = parse_index(INDEX_XML)
    print(f"  {len(terms_out)} index_terms rows")

    print("Inserting tabular_codes...")
    conn.executemany(
        """INSERT INTO tabular_codes
           (code, code_formatted, node_type, parent_code, chapter_num, chapter_desc,
            section_id, section_desc, short_title, long_desc, is_placeholder,
            is_header, is_billable, has_seventh_char, seventh_char_source_code, seventh_char, order_number)
           VALUES (:code, :code_formatted, :node_type, :parent_code, :chapter_num, :chapter_desc,
                   :section_id, :section_desc, :short_title, :long_desc, :is_placeholder,
                   :is_header, :is_billable, :has_seventh_char, :seventh_char_source_code, :seventh_char, :order_number)""",
        codes_out.values(),
    )

    print("Inserting tabular_notes...")
    conn.executemany(
        "INSERT INTO tabular_notes (code, note_type, note_text, ref_code) VALUES (?, ?, ?, ?)",
        notes_out,
    )

    print("Inserting seventh_char_defs...")
    conn.executemany(
        "INSERT INTO seventh_char_defs (code, char_value, meaning) VALUES (?, ?, ?)",
        sevenchar_out,
    )

    print("Inserting index_terms...")
    conn.executemany(
        """INSERT INTO index_terms
           (id, parent_id, letter, level, term_text, nemod_text, code_raw, code,
            manif_code, xref_type, xref_target, is_nec)
           VALUES (:id, :parent_id, :letter, :level, :term_text, :nemod_text, :code_raw, :code,
                   :manif_code, :xref_type, :xref_target, :is_nec)""",
        terms_out,
    )

    print("Building note_references (reverse index)...")
    cur = conn.execute(
        f"SELECT code, note_type, note_text FROM tabular_notes "
        f"WHERE note_type IN ({','.join('?' for _ in REVERSE_INDEX_NOTE_TYPES)})",
        list(REVERSE_INDEX_NOTE_TYPES),
    )
    note_refs = []
    for code, note_type, note_text in cur.fetchall():
        ref = extract_trailing_code_ref(note_text)
        condition_text = note_text[: note_text.rfind("(")].strip().strip(",").strip() if ref else note_text
        if condition_text:
            note_refs.append((code, note_type, condition_text))
    conn.executemany(
        "INSERT INTO note_references (code, note_type, condition_text) VALUES (?, ?, ?)",
        note_refs,
    )
    print(f"  {len(note_refs)} note_references rows")

    print("Inserting synonyms...")
    conn.executemany(
        "INSERT INTO synonyms (shorthand, canonical_term) VALUES (?, ?)",
        SYNONYM_SEED,
    )

    print("Building FTS5 indexes...")
    # Generated 7-character codes stay out of the FTS index: they're reached by
    # expanding their stem's hit to its billable descendants. Indexing them
    # would add 51k near-duplicate long titles and shift bm25 scores (corpus
    # size, average length) for every unrelated query.
    conn.execute(
        "INSERT INTO fts_tabular (code, short_title, long_desc) "
        "SELECT code, COALESCE(short_title, ''), COALESCE(long_desc, '') FROM tabular_codes "
        "WHERE seventh_char IS NULL"
    )
    conn.execute(
        "INSERT INTO fts_index_terms (term_id, code, term_text) "
        "SELECT id, COALESCE(code, code_raw, ''), term_text FROM index_terms"
    )
    conn.execute(
        "INSERT INTO fts_note_references (ref_id, code, condition_text) "
        "SELECT id, code, condition_text FROM note_references"
    )

    conn.commit()

    # ---- sanity checks ----
    print("\n--- Sanity checks ---")
    total_codes = conn.execute("SELECT COUNT(*) FROM tabular_codes WHERE node_type='diag'").fetchone()[0]
    billable = conn.execute("SELECT COUNT(*) FROM tabular_codes WHERE is_billable=1").fetchone()[0]
    chapters = conn.execute("SELECT COUNT(*) FROM tabular_codes WHERE node_type='chapter'").fetchone()[0]
    sections = conn.execute("SELECT COUNT(*) FROM tabular_codes WHERE node_type='section'").fetchone()[0]
    print(f"diag codes: {total_codes}, billable: {billable}, chapters: {chapters}, sections: {sections}")
    order_billable = sum(1 for v in order_data.values() if v["is_billable"])
    if billable != order_billable:
        raise RuntimeError(f"billable codes in DB ({billable}) != order file ({order_billable})")

    for spot_code in ["E08321", "M25551", "A000", "S72001A", "T401X1A", "S0100XA"]:
        row = conn.execute(
            "SELECT code_formatted, long_desc, short_title, is_billable FROM tabular_codes WHERE code=?",
            (spot_code,),
        ).fetchone()
        print(f"  spot-check {spot_code}: {row}")

    row = conn.execute(
        "SELECT code, chapter_desc, section_desc FROM tabular_codes WHERE code='M25551'"
    ).fetchone()
    print(f"  M25551 chapter/section: {row}")

    excl2 = conn.execute(
        "SELECT note_text FROM tabular_notes WHERE code='SEC:M20-M25' AND note_type='excludes2'"
    ).fetchall()
    print(f"  M20-M25 section excludes2: {excl2}")

    mi = conn.execute(
        "SELECT term_text, code FROM index_terms WHERE term_text LIKE 'Cholera%' LIMIT 5"
    ).fetchall()
    print(f"  index sample 'Cholera*': {mi}")

    conn.close()
    print("\nDone ->", DB_PATH)


if __name__ == "__main__":
    build()
