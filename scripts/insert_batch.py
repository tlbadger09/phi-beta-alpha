#!/usr/bin/env python3
"""
insert_batch.py — Validate and insert OCR records into census_ocr_1870.

Input: JSON file with list of record dicts (produced by OCR step)
  Each record must have: reel_number, page_number, state, last_name
  Optional: first_name, age, sex, color, occupation, birthplace,
            dwelling, family, entry_num, county, notes

Usage:
  python3 scripts/insert_batch.py records.json
  python3 scripts/insert_batch.py records.json --dry-run
  python3 scripts/insert_batch.py records.json --state "South Carolina" --county "Beaufort"

Dedup: skips rows where (reel_number, page_number, entry_num, last_name, first_name)
       already exists. Uses INSERT OR IGNORE on a UNIQUE index when entry_num present.
"""

import sys
import json
import sqlite3
import argparse
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from member_lookup import soundex

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"

STATE_FIPS = {
    "alabama": 1, "florida": 12, "georgia": 13, "kentucky": 21,
    "louisiana": 22, "mississippi": 28, "north carolina": 37,
    "south carolina": 45, "tennessee": 47, "virginia": 51,
    "new york": 36, "maryland": 24,
}

VALID_COLOR = {"W", "B", "M", "C", "I", ""}
MAX_AGE = 120


def validate_record(rec: dict, reel_number: int, page_number: int,
                    state: str, county: str) -> dict | None:
    last_name  = (rec.get("last_name") or "").strip()
    first_name = (rec.get("first_name") or "").strip()
    if not last_name:
        return None  # skip unnamed rows

    # Age sanity
    age = rec.get("age")
    if age is not None:
        try:
            age = int(age)
            if age < 0 or age > MAX_AGE:
                age = None
        except (TypeError, ValueError):
            age = None

    color = (rec.get("color") or "").strip().upper()
    if color not in VALID_COLOR:
        color = ""

    sex = (rec.get("sex") or "").strip().upper()
    if sex not in ("M", "F", ""):
        sex = ""

    state_norm  = state.strip()
    county_norm = (rec.get("county") or county or "").strip()
    state_fips  = STATE_FIPS.get(state_norm.lower(), 0)

    is_black = 1 if color in ("B", "M") else 0

    last_sdx  = soundex(last_name)  if last_name  else None
    first_sdx = soundex(first_name) if first_name else None

    return {
        "state":        state_norm,
        "state_fips":   state_fips,
        "county":       county_norm,
        "reel_number":  reel_number,
        "page_number":  page_number,
        "household_num":rec.get("dwelling"),
        "entry_num":    rec.get("family") or rec.get("entry_num"),
        "last_name":    last_name,
        "first_name":   first_name,
        "last_soundex": last_sdx,
        "first_soundex":first_sdx,
        "age":          str(age) if age is not None else "",
        "sex":          sex,
        "race":         color,
        "occupation":   (rec.get("occupation") or "").strip(),
        "birthplace":   (rec.get("birthplace") or "").strip(),
        "real_value":   rec.get("real_estate_value"),
        "personal_value": rec.get("personal_estate_value"),
        "notes":        (rec.get("notes") or "").strip(),
        "is_black":     is_black,
        "processed_at": datetime.datetime.now().isoformat(),
    }


def insert_records(records: list[dict], reel_number: int, page_number: int,
                   state: str, county: str,
                   dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (inserted, skipped, invalid)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    inserted = skipped = invalid = 0
    now = datetime.datetime.now().isoformat()

    for raw in records:
        v = validate_record(raw, reel_number, page_number, state, county)
        if v is None:
            invalid += 1
            continue

        if dry_run:
            print(f"  DRY: {v['last_name']}, {v['first_name']} | "
                  f"age={v['age']} race={v['race']} county={v['county']}")
            inserted += 1
            continue

        # Dedup check: exact match on (reel, page, entry_num, last, first)
        if v["entry_num"] is not None:
            existing = conn.execute("""
                SELECT id FROM census_ocr_1870
                WHERE reel_number=? AND page_number=? AND entry_num=?
                  AND last_name=? AND first_name=?
            """, (v["reel_number"], v["page_number"], v["entry_num"],
                  v["last_name"], v["first_name"])).fetchone()
        else:
            existing = conn.execute("""
                SELECT id FROM census_ocr_1870
                WHERE reel_number=? AND page_number=?
                  AND last_name=? AND first_name=? AND age=?
            """, (v["reel_number"], v["page_number"],
                  v["last_name"], v["first_name"], v["age"])).fetchone()

        if existing:
            skipped += 1
            continue

        conn.execute("""
            INSERT INTO census_ocr_1870
              (state, state_fips, county, reel_number, page_number,
               household_num, entry_num, last_name, first_name,
               last_soundex, first_soundex, age, sex, race,
               occupation, birthplace, real_value, personal_value,
               notes, is_black, processed_at)
            VALUES
              (:state, :state_fips, :county, :reel_number, :page_number,
               :household_num, :entry_num, :last_name, :first_name,
               :last_soundex, :first_soundex, :age, :sex, :race,
               :occupation, :birthplace, :real_value, :personal_value,
               :notes, :is_black, :processed_at)
        """, v)
        inserted += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return inserted, skipped, invalid


def main():
    parser = argparse.ArgumentParser(description="Insert OCR records into census_ocr_1870")
    parser.add_argument("records_file", help="JSON file with list of record dicts")
    parser.add_argument("--reel",    type=int, help="Reel number (overrides field in records)")
    parser.add_argument("--page",    type=int, help="Page number (overrides field in records)")
    parser.add_argument("--state",   default="",  help="State name (overrides field in records)")
    parser.add_argument("--county",  default="",  help="County name (overrides field in records)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be inserted without writing")
    args = parser.parse_args()

    records_path = Path(args.records_file)
    if not records_path.exists():
        print(f"File not found: {records_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(records_path.read_text())

    # Support both a bare list and {reel, page, state, county, records: [...]}
    if isinstance(data, list):
        records = data
        reel    = args.reel or 0
        page    = args.page or 0
        state   = args.state or ""
        county  = args.county or ""
    else:
        records = data.get("records", [])
        reel    = args.reel or data.get("reel_number", 0)
        page    = args.page or data.get("page_number", 0)
        state   = args.state or data.get("state", "")
        county  = args.county or data.get("county", "")

    if not state:
        print("--state required", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(records)} records | reel={reel} page={page} {state}/{county}")
    ins, skip, inv = insert_records(records, reel, page, state, county, dry_run=args.dry_run)
    print(f"Result: {ins} inserted, {skip} skipped (duplicates), {inv} invalid (no last name)")

    if args.dry_run:
        print("[dry-run: no changes written]")


if __name__ == "__main__":
    main()
