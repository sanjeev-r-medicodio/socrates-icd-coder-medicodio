#!/usr/bin/env python3
"""Search benchmark: how often does search() find the code a coder would reach?

Two query sets, both drawn deterministically (fixed seed) from the CMS sources
in the DB, so any two builds can be compared on identical queries:

- index:   an Alphabetical Index path, e.g. "Pain, joint, hip" -> M25.55-
           (expected = any billable code under the path's code)
- tabular: a billable code's own long title, e.g. "Pain in right hip" -> M25.551

Reports top-1 and recall@20. This measures ranking consistency with the code
set, not clinical accuracy -- gold-labelled charts are still needed for that.

    python scripts/benchmark_search.py [--n 300] [--db path/to/icd10.db] [--seed 7]
"""
import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search_engine.core import DB_PATH, search  # noqa: E402


def index_path(conn, term_id):
    chain = []
    while term_id is not None:
        text, term_id = conn.execute(
            "SELECT term_text, parent_id FROM index_terms WHERE id = ?", (term_id,)
        ).fetchone()
        chain.append(text)
    return ", ".join(reversed(chain))


def expected_codes(conn, code):
    prefix = code.rstrip("-").replace(".", "")
    return {r[0] for r in conn.execute(
        "SELECT code FROM tabular_codes WHERE code >= ? AND code < ? AND is_billable = 1",
        (prefix, prefix + "~"),
    )}


def sample_cases(conn, n, seed):
    rng = random.Random(seed)
    index_rows = conn.execute(
        "SELECT id, COALESCE(code_raw, code) FROM index_terms "
        "WHERE COALESCE(code_raw, code) IS NOT NULL ORDER BY id"
    ).fetchall()
    cases = []
    for term_id, code in rng.sample(index_rows, n):
        exp = expected_codes(conn, code)
        if exp:
            cases.append(("index", index_path(conn, term_id), exp))
    tab_rows = conn.execute(
        "SELECT code, long_desc FROM tabular_codes "
        "WHERE is_billable = 1 AND seventh_char IS NULL ORDER BY code"
    ).fetchall()
    for code, desc in rng.sample(tab_rows, n):
        cases.append(("tabular", desc, {code}))
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--show-misses", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cases = sample_cases(conn, args.n, args.seed)
    stats = {}
    misses = []
    start = time.perf_counter()
    for kind, query, exp in cases:
        codes = [r.code for r in search(query, conn=conn).results]
        s = stats.setdefault(kind, {"n": 0, "top1": 0, "r20": 0})
        s["n"] += 1
        s["top1"] += bool(codes) and codes[0] in exp
        hit = any(c in exp for c in codes[:20])
        s["r20"] += hit
        if not hit:
            misses.append((kind, query, sorted(exp)[:3], codes[:3]))
    elapsed = time.perf_counter() - start

    for kind, s in stats.items():
        print(f"{kind:8} n={s['n']:4}  top1={s['top1'] / s['n']:.1%}  recall@20={s['r20'] / s['n']:.1%}")
    print(f"{len(cases)} queries in {elapsed:.1f}s ({elapsed / len(cases) * 1000:.0f} ms/query)")
    for m in misses[: args.show_misses]:
        print("  miss", m)


if __name__ == "__main__":
    main()
