#!/usr/bin/env python3
"""
bridge_engine.py — 1870 Bridge Engine

Crosses the 1870 wall for a confirmed post-1870 ancestor by searching:
  1. Slave schedules (1850/1860) — via enslaver-surname mapping
  2. Freedmen's Bureau records (bureau_patients / bureau_people)
  3. Freedman's Savings Bank depositors
  4. Uses county + surname + age/sex to score and rank candidates

Produces bridge_candidates rows in the DB, queryable by the app.

Usage:
  python3 scripts/bridge_engine.py --member MEMBER-BACON-CLIFTON-1906
  python3 scripts/bridge_engine.py --ancestor ANC-BACON-SIMON-PATRIARCH
  python3 scripts/bridge_engine.py --all
  python3 scripts/bridge_engine.py --report               # print candidate table
"""

import re
import sys
import json
import hashlib
import argparse
import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"


# ── Soundex (local copy so script is self-contained) ─────────────────────────
def soundex(name: str) -> str:
    name = name.upper().strip()
    if not name:
        return "0000"
    code_map = {"BFPV": "1", "CGJKQSXYZ": "2", "DT": "3",
                "L": "4", "MN": "5", "R": "6"}
    def cc(c):
        for keys, val in code_map.items():
            if c in keys:
                return val
        return "0"
    first = name[0]
    result = first
    prev = cc(first)
    for ch in name[1:]:
        c = cc(ch)
        if c != "0" and c != prev:
            result += c
        prev = c if c != "0" else prev
    return result.ljust(4, "0")[:4]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _age_score(target_year: int | None, candidate_age: int | None,
               schedule_year: int) -> int:
    """
    Score age match 0-40.
    target_year = birth year of the ancestor we're trying to find.
    candidate_age = age in the slave schedule / bank record.
    schedule_year = year of the source record.
    """
    if not target_year or not candidate_age:
        return 10  # neutral when data is missing
    implied_birth = schedule_year - candidate_age
    diff = abs(target_year - implied_birth)
    if diff <= 2:
        return 40
    if diff <= 5:
        return 30
    if diff <= 10:
        return 18
    if diff <= 15:
        return 8
    return 0


def _sex_score(target_sex: str | None, candidate_sex: str | None) -> int:
    if not target_sex or not candidate_sex:
        return 5  # neutral
    return 20 if target_sex.upper()[:1] == candidate_sex.upper()[:1] else 0


def _county_score(target_county: str | None, target_state: str | None,
                  candidate_county: str | None, candidate_state: str | None) -> int:
    if not target_county or not candidate_county:
        return 0
    # Normalize: Hampton County was Beaufort County in SC before 1878
    def norm_county(c, s):
        c = (c or "").lower().replace("county", "").strip()
        if s and "south carolina" in s.lower() and c in ("hampton", "beaufort"):
            return "beaufort_hampton"
        return c
    tc = norm_county(target_county, target_state)
    cc = norm_county(candidate_county, candidate_state)
    return 20 if tc == cc else (10 if tc[:4] == cc[:4] else 0)


def _surname_score(target_surname: str, enslaver_id: str | None,
                   candidate_table: str, conn: sqlite3.Connection) -> tuple[int, str]:
    """
    Score surname linkage 0-30. Returns (score, match_type).
    Highest when enslaver's known surname pool includes target_surname.
    """
    if candidate_table == "slave_schedule_entries":
        # Slave schedule entries have no name — surname match is entirely via enslaver link
        if not enslaver_id:
            return 0, "no_enslaver_link"
        rows = conn.execute(
            "SELECT surname, confidence FROM enslaver_surnames WHERE enslaver_id=?",
            (enslaver_id,)
        ).fetchall()
        for surname, conf in rows:
            if surname.lower() == target_surname.lower():
                return int(30 * conf / 100), "enslaver_surname_match"
        return 5, "same_enslaver_no_surname_match"

    if candidate_table in ("freedman_bank", "bureau_patients"):
        # Name is recorded — soundex match
        tsdx = soundex(target_surname)
        return 25, "soundex_match"  # already filtered to soundex match before scoring

    return 0, "unknown"


def _compute_score(target: dict, candidate: dict, candidate_table: str,
                   enslaver_id: str | None, conn: sqlite3.Connection) -> tuple[int, str]:
    """
    Compute overall candidate score (0-100) and match notes.
    target: ancestor dict (birth_year, birth_county, birth_state, first_name, last_name)
    candidate: dict from slave_schedule_entries / freedman_bank / bureau_patients
    """
    yr   = target.get("birth_year")
    sex  = None  # ancestors table doesn't store sex directly — derive from name clues
    cty  = target.get("birth_county")
    st   = target.get("birth_state")
    surn = target.get("last_name", "")

    # Schedule/bank year for age calculation
    sched_yr = candidate.get("schedule_year") or candidate.get("schedule_year", 1860)
    if candidate_table == "bureau_patients":
        sched_yr = 1866  # Freedmen's Bureau records ~1865-1870

    age_s = _age_score(yr, candidate.get("age"), sched_yr)
    sex_s = _sex_score(sex, candidate.get("sex"))
    cty_s = _county_score(cty, st, candidate.get("county"), candidate.get("state"))
    surn_s, match_type = _surname_score(surn, enslaver_id, candidate_table, conn)

    total = age_s + sex_s + cty_s + surn_s

    notes = (
        f"age_score={age_s} sex_score={sex_s} county_score={cty_s} "
        f"surname_score={surn_s} surname_match={match_type}"
    )
    return min(total, 100), notes


# ── Candidate search ───────────────────────────────────────────────────────────

def _search_slave_schedules(ancestor: dict, conn: sqlite3.Connection) -> list[dict]:
    """
    Find slave schedule entries plausibly matching this ancestor.
    Strategy:
      1. Find enslavers who gave the Bacon/Badger surname to freed people in the right county
      2. Filter their slave schedule entries by age/sex match
    """
    surname = ancestor.get("last_name", "")
    county  = ancestor.get("birth_county", "")
    state   = ancestor.get("birth_state", "")
    yr      = ancestor.get("birth_year")

    # Find enslavers linked to this surname
    enslaver_rows = conn.execute(
        "SELECT e.enslaver_id, e.county, e.state FROM enslavers e "
        "JOIN enslaver_surnames es ON es.enslaver_id = e.enslaver_id "
        "WHERE es.surname = ? COLLATE NOCASE",
        (surname,)
    ).fetchall()

    results = []
    for eid, e_county, e_state in enslaver_rows:
        # All schedule entries for this enslaver
        entries = conn.execute(
            "SELECT *, ? AS enslaver_id_ref FROM slave_schedule_entries WHERE enslaver_id=?",
            (eid, eid)
        ).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(slave_schedule_entries)").fetchall()]
        cols.append("enslaver_id_ref")
        for row in entries:
            d = dict(zip(cols, row))
            d["_enslaver_id"] = eid
            d["_enslaver_county"] = e_county
            d["county"] = d.get("county") or e_county
            d["state"]  = d.get("state") or e_state
            results.append(d)

    return results


def _search_freedman_bank(ancestor: dict, conn: sqlite3.Connection) -> list[dict]:
    """Search Freedman's Savings Bank records by soundex surname + county."""
    surname = ancestor.get("last_name", "")
    sdx     = soundex(surname)
    county  = ancestor.get("birth_county", "")
    state   = ancestor.get("birth_state", "")

    # Soundex match + fuzzy county
    rows = conn.execute(
        "SELECT * FROM freedman_bank WHERE last_soundex=?",
        (sdx,)
    ).fetchall()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(freedman_bank)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def _search_bureau(ancestor: dict, conn: sqlite3.Connection) -> list[dict]:
    """Search Freedmen's Bureau patient records by soundex surname."""
    surname = ancestor.get("last_name", "")
    sdx     = soundex(surname)

    rows = conn.execute(
        "SELECT * FROM bureau_patients WHERE last_soundex=?",
        (sdx,)
    ).fetchall()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(bureau_patients)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


# ── Bridge computation ─────────────────────────────────────────────────────────

def compute_bridge(ancestor_id: str, conn: sqlite3.Connection,
                   min_score: int = 15) -> list[dict]:
    """
    Find all pre-1870 candidates for the given ancestor.
    Returns sorted list of candidate dicts.
    """
    row = conn.execute(
        "SELECT * FROM lineage_ancestors WHERE ancestor_id=?", (ancestor_id,)
    ).fetchone()
    if not row:
        return []
    cols = [d[1] for d in conn.execute("PRAGMA table_info(lineage_ancestors)").fetchall()]
    ancestor = dict(zip(cols, row))

    # Only bridge pre-1870 ancestors (birth before ~1868 plausible)
    yr = ancestor.get("birth_year") or 0
    if yr > 1860:
        return []

    # Gather candidates from all three sources
    candidates = []

    # 1. Slave schedules
    for c in _search_slave_schedules(ancestor, conn):
        score, notes = _compute_score(ancestor, c, "slave_schedule_entries",
                                      c.get("_enslaver_id"), conn)
        if score >= min_score:
            candidates.append({
                "source_table": "slave_schedule_entries",
                "source_id":    c["entry_id"],
                "enslaver_id":  c.get("_enslaver_id"),
                "age":          c.get("age"),
                "sex":          c.get("sex"),
                "color":        c.get("color"),
                "schedule_year": c.get("schedule_year"),
                "county":       c.get("county"),
                "state":        c.get("state"),
                "citation":     c.get("source_citation", ""),
                "overall_score": score,
                "notes":        notes,
            })

    # 2. Freedman's Savings Bank
    for c in _search_freedman_bank(ancestor, conn):
        score, notes = _compute_score(ancestor, c, "freedman_bank", None, conn)
        if score >= min_score:
            candidates.append({
                "source_table": "freedman_bank",
                "source_id":    c["bank_id"],
                "enslaver_id":  None,
                "name":         c.get("full_name", ""),
                "age":          c.get("age"),
                "county":       c.get("county"),
                "state":        c.get("state"),
                "former_enslaver": c.get("former_enslaver"),
                "citation":     c.get("source_citation", ""),
                "overall_score": score,
                "notes":        notes,
            })

    # 3. Freedmen's Bureau
    for c in _search_bureau(ancestor, conn):
        score, notes = _compute_score(ancestor, c, "bureau_patients", None, conn)
        if score >= min_score:
            candidates.append({
                "source_table": "bureau_patients",
                "source_id":    c["id"],
                "enslaver_id":  None,
                "name":         c.get("full_name", ""),
                "age":          c.get("age"),
                "county":       c.get("county"),
                "state":        c.get("state"),
                "citation":     c.get("source_citation") or c.get("source_file", ""),
                "overall_score": score,
                "notes":        notes,
            })

    candidates.sort(key=lambda x: x["overall_score"], reverse=True)
    return candidates


def upsert_bridge_candidates(ancestor_id: str, member_id: str,
                              candidates: list[dict], conn: sqlite3.Connection) -> int:
    """Save candidates to bridge_candidates table. Returns count upserted."""
    count = 0
    for c in candidates:
        cid = hashlib.md5(
            f"{ancestor_id}:{c['source_table']}:{c['source_id']}".encode()
        ).hexdigest()[:16].upper()
        try:
            conn.execute("""
                INSERT INTO bridge_candidates
                  (candidate_id, member_id, ancestor_id, source_table, source_id,
                   enslaver_id, age_match_score, overall_score, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ancestor_id, source_table, source_id)
                DO UPDATE SET overall_score=excluded.overall_score,
                              notes=excluded.notes,
                              computed_at=datetime('now')
            """, (
                cid, member_id, ancestor_id,
                c["source_table"], c["source_id"],
                c.get("enslaver_id"),
                c.get("age_match_score", 0),
                c["overall_score"],
                c["notes"],
            ))
            count += 1
        except sqlite3.Error as e:
            print(f"  upsert error: {e}")
    conn.commit()
    return count


def print_report(conn: sqlite3.Connection, member_id: str | None = None) -> None:
    """Print bridge candidates table."""
    sql = """
        SELECT bc.ancestor_id, la.first_name, la.last_name, la.birth_year,
               bc.source_table, bc.source_id, bc.enslaver_id,
               bc.overall_score, bc.notes
        FROM bridge_candidates bc
        JOIN lineage_ancestors la ON la.ancestor_id = bc.ancestor_id
    """
    params = []
    if member_id:
        sql += " WHERE bc.member_id=?"
        params.append(member_id)
    sql += " ORDER BY bc.overall_score DESC"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No bridge candidates found.")
        return

    print(f"\n{'='*80}")
    print(f"{'BRIDGE CANDIDATES':^80}")
    print(f"{'='*80}")
    print(f"{'Ancestor':<32} {'Source':<22} {'Score':>5}  Notes")
    print("-" * 80)
    for row in rows:
        anc_id, fn, ln, yr, src_tbl, src_id, eid, score, notes = row
        name = f"{fn} {ln} (~{yr})" if yr else f"{fn} {ln}"
        src  = f"{src_tbl.replace('_',' ')} #{src_id}"
        print(f"{name:<32} {src:<22} {score:>5}  {(notes or '')[:40]}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def run_for_member(member_id: str, conn: sqlite3.Connection) -> int:
    ancestors = conn.execute(
        "SELECT ancestor_id, first_name, last_name, birth_year FROM lineage_ancestors "
        "WHERE member_id=? ORDER BY generation",
        (member_id,)
    ).fetchall()

    total_candidates = 0
    for anc_id, fn, ln, yr in ancestors:
        if yr and yr > 1860:
            continue  # no bridge needed for post-1860 ancestors
        print(f"  Bridge: {fn} {ln} (~{yr or '?'}) [{anc_id}]")
        candidates = compute_bridge(anc_id, conn)
        if candidates:
            saved = upsert_bridge_candidates(anc_id, member_id, candidates, conn)
            total_candidates += saved
            print(f"    {saved} candidate(s) saved:")
            for c in candidates[:5]:
                print(f"      score={c['overall_score']:>3} source={c['source_table']} "
                      f"county={c.get('county','')} age={c.get('age','')} "
                      f"sex={c.get('sex','')} enslaver={c.get('enslaver_id','')}")
        else:
            print(f"    No candidates meeting threshold (no matching sources yet)")
    return total_candidates


def main():
    parser = argparse.ArgumentParser(description="ΦΒΑ 1870 Bridge Engine")
    parser.add_argument("--member",   help="Run bridge for a specific member ID")
    parser.add_argument("--ancestor", help="Run bridge for a specific ancestor ID")
    parser.add_argument("--all",      action="store_true", help="Run for all members")
    parser.add_argument("--report",   action="store_true", help="Print candidates table and exit")
    parser.add_argument("--min-score", type=int, default=15,
                        help="Minimum score to store a candidate (default 15)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)

    if args.report:
        print_report(conn, args.member)
        conn.close()
        return

    if args.ancestor:
        # find member for this ancestor
        row = conn.execute(
            "SELECT member_id FROM lineage_ancestors WHERE ancestor_id=?",
            (args.ancestor,)
        ).fetchone()
        if not row:
            print(f"Ancestor not found: {args.ancestor}")
            conn.close()
            return
        candidates = compute_bridge(args.ancestor, conn)
        if candidates:
            upsert_bridge_candidates(args.ancestor, row[0], candidates, conn)
        print_report(conn, row[0])
        conn.close()
        return

    members = []
    if args.all:
        members = [r[0] for r in conn.execute("SELECT member_id FROM members").fetchall()]
    elif args.member:
        members = [args.member]
    else:
        parser.print_help()
        conn.close()
        return

    for mid in members:
        print(f"\n{'='*60}")
        print(f"Member: {mid}")
        print(f"{'='*60}")
        total = run_for_member(mid, conn)
        print(f"  Total candidates: {total}")

    print_report(conn, args.member if not args.all else None)
    conn.close()


if __name__ == "__main__":
    main()
