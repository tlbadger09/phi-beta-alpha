#!/usr/bin/env python3
"""
rebuild_fts.py — Rebuild the names_fts FTS5 index and fts_rowid_map.

Run after bulk OCR inserts to include new records in FTS search.
Safe to run while the app is running (uses WAL mode).

Usage:
  python3 scripts/rebuild_fts.py           # full rebuild
  python3 scripts/rebuild_fts.py --stats   # show current index size and quit
"""

import sys
import sqlite3
import argparse
import datetime
from pathlib import Path

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"


def show_stats(conn):
    fts_count  = conn.execute("SELECT COUNT(*) FROM names_fts").fetchone()[0]
    map_count  = conn.execute("SELECT COUNT(*) FROM fts_rowid_map").fetchone()[0]
    ga_count   = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870").fetchone()[0]
    ocr_count  = conn.execute("SELECT COUNT(*) FROM census_ocr_1870").fetchone()[0]
    cen_count  = conn.execute("SELECT COUNT(*) FROM census WHERE YEAR=1870").fetchone()[0]
    total_src  = ga_count + ocr_count + cen_count
    print(f"FTS index rows   : {fts_count:,}")
    print(f"FTS rowid_map    : {map_count:,}")
    print(f"Source total     : {total_src:,}  (GA={ga_count:,}, multi={ocr_count:,}, IPUMS={cen_count:,})")
    coverage = fts_count / max(total_src, 1) * 100
    print(f"Coverage         : {coverage:.1f}%")


def rebuild(conn, verbose=True):
    t0 = datetime.datetime.now()
    print("Clearing existing FTS index…")
    conn.execute("DELETE FROM names_fts")
    conn.execute("DELETE FROM fts_rowid_map")
    conn.commit()

    # ── census_ocr_georgia_1870 → rowid 1..N ──────────────────────────────────
    print("Indexing census_ocr_georgia_1870…")
    rows = conn.execute("""
        SELECT rowid, last_name, first_name, county, state
        FROM census_ocr_georgia_1870
        WHERE last_name IS NOT NULL AND last_name != ''
    """).fetchall()

    map_batch, fts_batch = [], []
    for row in rows:
        rid, last, first, county, state = row
        fts_rowid = rid  # 1-based rowid, stays in 1..~115k range
        map_batch.append((fts_rowid, "census_ocr_georgia_1870", str(rid), state or "Georgia", county or ""))
        fts_batch.append((fts_rowid, last, first or ""))

    conn.executemany("INSERT INTO fts_rowid_map VALUES (?,?,?,?,?)", map_batch)
    conn.executemany("INSERT INTO names_fts(rowid, last_name, first_name) VALUES (?,?,?)", fts_batch)
    conn.commit()
    if verbose:
        print(f"  {len(fts_batch):,} rows from census_ocr_georgia_1870")

    # ── census_ocr_1870 → rowid 3_000_000 + id ────────────────────────────────
    print("Indexing census_ocr_1870 (multi-state)…")
    rows = conn.execute("""
        SELECT id, last_name, first_name, county, state
        FROM census_ocr_1870
        WHERE last_name IS NOT NULL AND last_name != ''
    """).fetchall()

    map_batch, fts_batch = [], []
    for row in rows:
        rec_id, last, first, county, state = row
        fts_rowid = 3_000_000 + rec_id
        map_batch.append((fts_rowid, "census_ocr_1870", str(rec_id), state or "", county or ""))
        fts_batch.append((fts_rowid, last, first or ""))

    conn.executemany("INSERT INTO fts_rowid_map VALUES (?,?,?,?,?)", map_batch)
    conn.executemany("INSERT INTO names_fts(rowid, last_name, first_name) VALUES (?,?,?)", fts_batch)
    conn.commit()
    if verbose:
        print(f"  {len(fts_batch):,} rows from census_ocr_1870")

    # ── census (IPUMS 1870) → rowid 6_000_000 + rowid ───────────────────────
    print("Indexing census IPUMS (1870)…")
    rows = conn.execute("""
        SELECT rowid, NAMELAST, NAMEFIRST, STATEICP, COUNTY
        FROM census WHERE YEAR=1870
        AND NAMELAST IS NOT NULL AND NAMELAST != ''
    """).fetchall()

    map_batch, fts_batch = [], []
    for row in rows:
        src_rowid, last, first, stateicp, county = row
        fts_rowid = 6_000_000 + src_rowid
        map_batch.append((fts_rowid, "census", str(src_rowid), str(stateicp or ""), str(county or "")))
        fts_batch.append((fts_rowid, last, first or ""))

    # Batch insert in chunks to avoid huge transactions
    chunk = 50_000
    for i in range(0, len(map_batch), chunk):
        conn.executemany("INSERT INTO fts_rowid_map VALUES (?,?,?,?,?)", map_batch[i:i+chunk])
        conn.executemany("INSERT INTO names_fts(rowid, last_name, first_name) VALUES (?,?,?)", fts_batch[i:i+chunk])
        conn.commit()
        if verbose:
            pct = min(i + chunk, len(fts_batch))
            print(f"  {pct:,}/{len(fts_batch):,} IPUMS rows…", end="\r")

    print()
    elapsed = (datetime.datetime.now() - t0).total_seconds()
    total = conn.execute("SELECT COUNT(*) FROM names_fts").fetchone()[0]
    print(f"Rebuild complete: {total:,} rows in {elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(description="Rebuild FTS5 search index")
    parser.add_argument("--stats", action="store_true", help="Show index stats and exit")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")

    if args.stats:
        show_stats(conn)
        conn.close()
        return

    show_stats(conn)
    print()
    rebuild(conn)
    print()
    show_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
