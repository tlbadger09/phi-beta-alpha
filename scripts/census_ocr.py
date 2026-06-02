#!/usr/bin/env python3
"""
Phi Beta Alpha Historical Lineage Society
Census Image OCR Pipeline — v1.0

Processes handwritten census images (JPG/PNG) using GPT-4o Vision:
  1. Runs image through GPT-4o Vision OCR
  2. Parses output into structured per-person records
  3. Applies Soundex + Jaro-Winkler fuzzy name normalization
  4. Saves JSON output and writes to SQLite database

Usage:
  python3 census_ocr.py --image path/to/census_page.jpg \
                        --county "McIntosh" --state Georgia \
                        --year 1870 [--output results.json]

Requirements:
  OPENAI_API_KEY env var (or in ~/.zshrc)
"""

import os
import re
import sys
import json
import base64
import sqlite3
import hashlib
import argparse
import datetime
from pathlib import Path

import jellyfish
from openai import OpenAI
from PIL import Image

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH      = os.path.expanduser("~/Documents/phi-beta-alpha/processed/lineage_1870.db")
OUTPUT_DIR   = os.path.expanduser("~/Documents/phi-beta-alpha/output/ocr")
SCRIPTS_DIR  = os.path.dirname(os.path.abspath(__file__))

# ── State FIPS ────────────────────────────────────────────────────────────────
STATE_FIPS = {
    "alabama": 1, "arkansas": 5, "california": 6, "connecticut": 9,
    "delaware": 10, "district of columbia": 11, "florida": 12,
    "georgia": 13, "illinois": 17, "indiana": 18, "iowa": 19,
    "kansas": 20, "kentucky": 21, "louisiana": 22, "maine": 23,
    "maryland": 24, "massachusetts": 25, "michigan": 26, "minnesota": 27,
    "mississippi": 28, "missouri": 29, "nebraska": 31, "new jersey": 34,
    "new york": 36, "north carolina": 37, "ohio": 39, "pennsylvania": 42,
    "rhode island": 44, "south carolina": 45, "tennessee": 47, "texas": 48,
    "virginia": 51, "west virginia": 54, "wisconsin": 55,
}

# Georgia NHGIS county codes (partial — southern GA focus)
GA_COUNTY_NHGIS = {
    "mcintosh": 1302190, "liberty": 1302070, "glynn": 1301550,
    "camden": 1300650, "brantley": 1300510, "wayne": 1303310,
    "pierce": 1302550, "charlton": 1300730, "ware": 1303250,
    "appling": 1300210, "tattnall": 1302930, "long": 1302110,
    "bryan": 1300550, "chatham": 1300750, "effingham": 1301290,
    "bulloch": 1300570, "burke": 1300590, "richmond": 1302710,
    "baldwin": 1300330, "bibb": 1300470, "muscogee": 1302410,
    "dougherty": 1301210, "thomas": 1303010, "lowndes": 1302130,
}

# Race/color code normalization
RACE_MAP = {
    "w": 1, "white": 1,
    "b": 2, "black": 2, "blk": 2,
    "m": 4, "mu": 4, "mulatto": 4, "mul": 4,
    "c": 6, "chinese": 6,
    "i": 5, "indian": 5, "am. indian": 5,
}

# Common name spelling variants for fuzzy normalization
NAME_CORRECTIONS = {
    "isreal": "Israel", "ceasar": "Caesar", "cesar": "Caesar",
    "hanna": "Hannah", "dinah": "Dinah", "dina": "Dinah",
    "abram": "Abraham", "louiza": "Louisa",
    "henery": "Henry", "hennry": "Henry",
    "willm": "William", "wm": "William",
    "thos": "Thomas", "chas": "Charles", "jas": "James",
    "geo": "George", "jno": "John",
}


# ════════════════════════════════════════════════════════════════════════════════
# OCR ENGINE — GPT-4o Vision
# ════════════════════════════════════════════════════════════════════════════════

OCR_PROMPT = """You are an expert genealogist and archivist transcribing a handwritten US Federal Census page from 1870.

Extract EVERY person listed on this census page. For each person, output a JSON array where each element has exactly these fields:

{
  "line_num": <integer, row number on page starting at 1>,
  "dwelling": <integer or null>,
  "family": <integer or null>,
  "last_name": <string — use the actual last name; if ditto marks (") appear, repeat the last name from the previous family>,
  "first_name": <string>,
  "age": <integer or null>,
  "sex": <"M" or "F">,
  "color": <"W", "B", "M", "C", or "I" — White, Black, Mulatto, Chinese, Indian>,
  "occupation": <string or null>,
  "birthplace": <string — full state or country name, e.g. "Georgia", "South Carolina">,
  "real_estate_value": <integer or null>,
  "personal_estate_value": <integer or null>,
  "notes": <string — any unusual markings, illegible text, or uncertainty>
}

Rules:
- If a name is partially illegible, write your best guess and note uncertainty in "notes"
- Ditto marks ("  or do.) mean repeat the value from the line above
- Abbreviations: Wm=William, Thos=Thomas, Chas=Charles, Jas=James, Geo=George, Jno=John
- Color abbreviations: B=Black, W=White, M=Mulatto, C=Chinese, I=Indian
- Output ONLY the JSON array, no explanation text before or after
"""


def encode_image(image_path: str) -> tuple[str, str]:
    """Returns (base64_data, media_type) for the image."""
    path = Path(image_path)
    suffix = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(suffix, "image/jpeg")

    # Downscale if too large (GPT-4o handles up to ~20MB but smaller is faster)
    img = Image.open(image_path)
    max_dim = 4096
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"

    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode(), media_type


def run_ocr(image_path: str, client: OpenAI) -> str:
    """Send image to GPT-4o Vision and return raw OCR response."""
    print(f"  Sending to GPT-4o Vision: {Path(image_path).name} ...")
    b64, media_type = encode_image(image_path)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        max_tokens=16000,
        temperature=0,
    )
    return response.choices[0].message.content.strip()


# ════════════════════════════════════════════════════════════════════════════════
# PARSER
# ════════════════════════════════════════════════════════════════════════════════

def parse_ocr_response(raw: str) -> list[dict]:
    """Extract JSON array from GPT-4o response, handling markdown fences."""
    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("records", "persons", "entries", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []
    except json.JSONDecodeError as e:
        # Response was truncated — recover all complete records before the cut
        print(f"  WARNING: JSON truncated, recovering complete records...")
        # Find the last complete record (ends with closing brace + comma or brace + newline)
        # Walk back to find the last fully closed object
        brace_depth = 0
        last_complete = 0
        in_string = False
        escape_next = False
        for idx, ch in enumerate(cleaned):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0:
                    last_complete = idx + 1
        if last_complete > 0:
            truncated_fixed = cleaned[:last_complete] + "\n]"
            try:
                data = json.loads(truncated_fixed)
                if isinstance(data, list):
                    print(f"  Recovered {len(data)} complete records from truncated response.")
                    return data
            except json.JSONDecodeError:
                pass
        print(f"  Could not recover records. Raw snippet: {raw[:300]}")
        return []


# ════════════════════════════════════════════════════════════════════════════════
# FUZZY NAME NORMALIZATION
# ════════════════════════════════════════════════════════════════════════════════

# Known surname variants in Reconstruction-era Georgia (Gullah/Geechee region)
KNOWN_SURNAMES = [
    "Bacon", "Freeman", "Baker", "Gould", "Maxwell", "Thorpe", "Stacy",
    "Winn", "Johnson", "Jackson", "Williams", "Davis", "Wilson", "Brown",
    "Jones", "Thomas", "Walker", "Harris", "Martin", "Thompson", "White",
    "Moore", "Robinson", "Clark", "Lewis", "Anderson", "Green", "Hall",
    "Mitchell", "Taylor", "Campbell", "Carter", "Howard", "Young", "King",
    "Wright", "Scott", "Adams", "Nelson", "Hill", "Allen", "Bryant",
    "Washington", "Jefferson", "Lincoln", "Grant", "Sherman",
    # McIntosh/Liberty County specific families
    "Quarterman", "Hazard", "Delegal", "Grovner", "Dunham", "Demere",
    "Spalding", "Mcintosh", "Mcqueen", "Butler", "Hazzard",
]

KNOWN_FIRST_NAMES = [
    "Moses", "Simon", "Washington", "Abraham", "Isaac", "Jacob", "Joseph",
    "Benjamin", "Elijah", "Isaiah", "Jeremiah", "Solomon", "Caesar",
    "Prince", "Celia", "Harriet", "Sarah", "Mary", "Hannah", "Dinah",
    "Louisa", "Julia", "Rachel", "Rebecca", "Esther", "Martha",
    "George", "Henry", "James", "John", "William", "Thomas", "Charles",
    "Robert", "Samuel", "David", "Daniel", "Edward", "Richard",
    "Israel", "Tobias", "Ezekiel", "Caleb", "Josiah", "Andrew",
]


def normalize_name(name: str, known_names: list[str],
                   jw_threshold: float = 0.88) -> dict:
    """
    Normalizes a name using:
      1. Direct lookup corrections (abbreviations/common variants)
      2. Jaro-Winkler similarity against known name list
      3. Soundex code for phonetic grouping

    Returns dict with: normalized, soundex, confidence, match_type
    """
    if not name or name.strip() in ("", "\"", "do.", "ditto"):
        return {"normalized": name, "soundex": "", "confidence": 1.0, "match_type": "empty"}

    name_clean = name.strip().title()
    name_lower = name_clean.lower()

    # Step 1: Direct corrections
    if name_lower in NAME_CORRECTIONS:
        return {
            "normalized":  NAME_CORRECTIONS[name_lower],
            "soundex":     jellyfish.soundex(NAME_CORRECTIONS[name_lower]),
            "confidence":  0.95,
            "match_type":  "correction",
        }

    # Step 2: Jaro-Winkler against known list
    best_score = 0.0
    best_match = name_clean
    for known in known_names:
        score = jellyfish.jaro_winkler_similarity(name_lower, known.lower())
        if score > best_score:
            best_score = score
            best_match = known

    if best_score >= jw_threshold:
        return {
            "normalized":  best_match,
            "soundex":     jellyfish.soundex(best_match),
            "confidence":  round(best_score, 3),
            "match_type":  "jaro_winkler",
        }

    # Step 3: Soundex fallback — find known name with same soundex
    name_sdx = jellyfish.soundex(name_clean)
    sdx_matches = [n for n in known_names if jellyfish.soundex(n) == name_sdx]
    if len(sdx_matches) == 1:
        return {
            "normalized":  sdx_matches[0],
            "soundex":     name_sdx,
            "confidence":  0.70,
            "match_type":  "soundex",
        }
    elif len(sdx_matches) > 1:
        return {
            "normalized":  name_clean,  # ambiguous — keep as-is
            "soundex":     name_sdx,
            "confidence":  0.50,
            "match_type":  "soundex_ambiguous",
            "sdx_candidates": sdx_matches,
        }

    # No match found — return as-is with soundex
    return {
        "normalized":  name_clean,
        "soundex":     jellyfish.soundex(name_clean) if name_clean else "",
        "confidence":  1.0,
        "match_type":  "verbatim",
    }


def enrich_record(rec: dict, county: str, state: str, year: int) -> dict:
    """Add normalized names, FIPS codes, and derived fields to a parsed record."""
    # Normalize names
    rec["last_name_norm"]  = normalize_name(rec.get("last_name", ""),  KNOWN_SURNAMES)
    rec["first_name_norm"] = normalize_name(rec.get("first_name", ""), KNOWN_FIRST_NAMES)

    # Race/color normalization
    color_raw = str(rec.get("color", "")).strip().lower()
    rec["race_code"] = RACE_MAP.get(color_raw, 0)

    # Sex normalization
    sex_raw = str(rec.get("sex", "")).strip().upper()
    rec["sex_code"] = 1 if sex_raw == "M" else (2 if sex_raw == "F" else 0)

    # Birthplace → BPL FIPS
    bp = str(rec.get("birthplace", "")).strip().lower()
    rec["bpl_fips"] = STATE_FIPS.get(bp, 0)

    # State/county context
    rec["census_year"]    = year
    rec["state"]          = state
    rec["state_fips"]     = STATE_FIPS.get(state.lower(), 0)
    rec["county"]         = county
    county_key            = county.lower().replace(" county", "").strip()
    rec["county_nhgis"]   = GA_COUNTY_NHGIS.get(county_key) if rec["state_fips"] == 13 else None

    # Derive birth year from age
    age = rec.get("age")
    if age and isinstance(age, (int, float)) and age > 0:
        rec["birthyr_derived"] = year - int(age)
    else:
        rec["birthyr_derived"] = None

    # Generate a deterministic OCR record ID
    id_str = f"{year}|{county}|{state}|{rec.get('last_name','')}|{rec.get('first_name','')}|{rec.get('age','')}|{rec.get('line_num','')}"
    rec["ocr_id"] = "OCR-" + hashlib.md5(id_str.encode()).hexdigest()[:12].upper()

    return rec


# ════════════════════════════════════════════════════════════════════════════════
# DATABASE WRITER
# ════════════════════════════════════════════════════════════════════════════════

def ensure_ocr_table(conn: sqlite3.Connection):
    """Create ocr_records table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_records (
            ocr_id          TEXT PRIMARY KEY,
            census_year     INTEGER,
            state           TEXT,
            state_fips      INTEGER,
            county          TEXT,
            county_nhgis    INTEGER,
            image_file      TEXT,
            line_num        INTEGER,
            dwelling        INTEGER,
            family_num      INTEGER,
            last_name_raw   TEXT,
            first_name_raw  TEXT,
            last_name_norm  TEXT,
            first_name_norm TEXT,
            last_soundex    TEXT,
            first_soundex   TEXT,
            name_confidence REAL,
            age             INTEGER,
            birthyr_derived INTEGER,
            sex_code        INTEGER,
            sex             TEXT,
            race_code       INTEGER,
            color_raw       TEXT,
            occupation      TEXT,
            birthplace      TEXT,
            bpl_fips        INTEGER,
            re_value        INTEGER,
            pe_value        INTEGER,
            notes           TEXT,
            created_at      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_last  ON ocr_records(last_name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_first ON ocr_records(first_name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_year  ON ocr_records(census_year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_byr   ON ocr_records(birthyr_derived)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_county ON ocr_records(county)")
    conn.commit()


def save_to_db(records: list[dict], image_file: str) -> int:
    """Insert enriched records into ocr_records table. Returns count inserted."""
    conn = sqlite3.connect(DB_PATH)
    ensure_ocr_table(conn)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    inserted = 0
    skipped  = 0

    for rec in records:
        ln = rec.get("last_name_norm", {})
        fn = rec.get("first_name_norm", {})
        try:
            conn.execute("""
                INSERT OR REPLACE INTO ocr_records VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                rec.get("ocr_id"),
                rec.get("census_year"),
                rec.get("state"),
                rec.get("state_fips"),
                rec.get("county"),
                rec.get("county_nhgis"),
                Path(image_file).name,
                rec.get("line_num"),
                rec.get("dwelling"),
                rec.get("family"),
                rec.get("last_name"),
                rec.get("first_name"),
                ln.get("normalized") if isinstance(ln, dict) else ln,
                fn.get("normalized") if isinstance(fn, dict) else fn,
                ln.get("soundex")    if isinstance(ln, dict) else "",
                fn.get("soundex")    if isinstance(fn, dict) else "",
                min(
                    ln.get("confidence", 1.0) if isinstance(ln, dict) else 1.0,
                    fn.get("confidence", 1.0) if isinstance(fn, dict) else 1.0,
                ),
                rec.get("age"),
                rec.get("birthyr_derived"),
                rec.get("sex_code"),
                rec.get("sex"),
                rec.get("race_code"),
                rec.get("color"),
                rec.get("occupation"),
                rec.get("birthplace"),
                rec.get("bpl_fips"),
                rec.get("real_estate_value"),
                rec.get("personal_estate_value"),
                rec.get("notes"),
                now,
            ))
            inserted += 1
        except Exception as e:
            print(f"  DB insert error for {rec.get('first_name')} {rec.get('last_name')}: {e}")
            skipped += 1

    conn.commit()
    conn.close()
    return inserted


# ════════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ════════════════════════════════════════════════════════════════════════════════

def run_pipeline(image_path: str, county: str, state: str, year: int,
                 output_path: str = None, save_db: bool = True) -> list[dict]:

    print()
    print("=" * 64)
    print("PHI BETA ALPHA — CENSUS IMAGE OCR PIPELINE")
    print("=" * 64)
    print(f"  Image:   {image_path}")
    print(f"  County:  {county}, {state}  |  Year: {year}")
    print()

    # Validate image
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    img_info = Image.open(image_path)
    print(f"  Image size: {img_info.size[0]}x{img_info.size[1]}  |  "
          f"File: {os.path.getsize(image_path)//1024}KB")
    print()

    # Init OpenAI client
    api_key = os.environ.get("OPENAI_API_KEY") or _read_key_from_zshrc()
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set. Add to ~/.zshrc or export in shell.")
    client = OpenAI(api_key=api_key)

    # ── Step 1: OCR ───────────────────────────────────────────────────────────
    print("[1/4] Running GPT-4o Vision OCR...")
    raw_ocr = run_ocr(image_path, client)
    print(f"  Raw response length: {len(raw_ocr)} chars")

    # ── Step 2: Parse ─────────────────────────────────────────────────────────
    print("[2/4] Parsing OCR output...")
    records = parse_ocr_response(raw_ocr)
    print(f"  Parsed {len(records)} person records")

    if not records:
        print("  ERROR: No records parsed. Raw OCR output:")
        print(raw_ocr[:500])
        return []

    # ── Step 3: Enrich + fuzzy normalize ─────────────────────────────────────
    print("[3/4] Enriching records (fuzzy name normalization, FIPS codes)...")
    enriched = [enrich_record(r, county, state, year) for r in records]

    # ── Step 4: Save ──────────────────────────────────────────────────────────
    print("[4/4] Saving results...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # JSON output
    if not output_path:
        stem = Path(image_path).stem
        output_path = os.path.join(OUTPUT_DIR, f"{stem}_ocr_{year}.json")

    with open(output_path, "w") as f:
        json.dump({
            "source_image":  os.path.basename(image_path),
            "county":        county,
            "state":         state,
            "census_year":   year,
            "record_count":  len(enriched),
            "processed_at":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "records":       enriched,
        }, f, indent=2)
    print(f"  JSON saved: {output_path}")

    # Database
    if save_db:
        n = save_to_db(enriched, image_path)
        print(f"  Database: {n} records written to ocr_records table in lineage_1870.db")

    # ── Results summary ───────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("EXTRACTED RECORDS")
    print("=" * 64)

    race_labels = {1: "White", 2: "Black", 4: "Mulatto", 5: "Indian", 6: "Chinese", 0: "Unknown"}
    black_records = [r for r in enriched if r.get("race_code") in (2, 4)]

    print(f"  Total persons:  {len(enriched)}")
    print(f"  Black/Mulatto:  {len(black_records)}")
    print()

    print(f"  {'#':<4} {'LAST (raw→norm)':<28} {'FIRST (raw→norm)':<24} "
          f"{'AGE':<5} {'S':<3} {'C':<3} {'BORN':<5} {'BIRTHPLACE':<16} {'CONF'}")
    print(f"  {'-'*4} {'-'*28} {'-'*24} {'-'*5} {'-'*3} {'-'*3} {'-'*5} {'-'*16} {'-'*6}")

    for i, r in enumerate(enriched, 1):
        ln = r.get("last_name_norm", {})
        fn = r.get("first_name_norm", {})
        ln_raw   = r.get("last_name", "")
        fn_raw   = r.get("first_name", "")
        ln_norm  = ln.get("normalized", ln_raw) if isinstance(ln, dict) else ln_raw
        fn_norm  = fn.get("normalized", fn_raw) if isinstance(fn, dict) else fn_raw
        ln_conf  = ln.get("confidence", 1.0)    if isinstance(ln, dict) else 1.0
        fn_conf  = fn.get("confidence", 1.0)    if isinstance(fn, dict) else 1.0
        conf     = min(ln_conf, fn_conf)

        last_display  = f"{ln_raw}→{ln_norm}" if ln_raw != ln_norm else ln_raw
        first_display = f"{fn_raw}→{fn_norm}" if fn_raw != fn_norm else fn_raw

        byr = r.get("birthyr_derived", "")
        print(f"  {i:<4} {last_display[:27]:<28} {first_display[:23]:<24} "
              f"{str(r.get('age','')):<5} {r.get('sex',''):<3} "
              f"{r.get('color',''):<3} {str(byr):<5} "
              f"{str(r.get('birthplace',''))[:15]:<16} {conf:.2f}")

    print()
    print(f"  Output JSON: {output_path}")
    print(f"  Database:    {DB_PATH}")
    print(f"  Table:       ocr_records")
    print("=" * 64)

    return enriched


def _read_key_from_zshrc():
    """Fallback: read OPENAI_API_KEY from ~/.zshrc if not in env."""
    zshrc = os.path.expanduser("~/.zshrc")
    if not os.path.exists(zshrc):
        return None
    with open(zshrc) as f:
        for line in f:
            m = re.match(r'export\s+OPENAI_API_KEY=["\']?([^"\']+)["\']?', line.strip())
            if m:
                return m.group(1).strip()
    return None


# ════════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Phi Beta Alpha Census Image OCR Pipeline"
    )
    parser.add_argument("--image",   required=True, help="Path to census image (JPG/PNG)")
    parser.add_argument("--county",  required=True, help="County name (e.g. McIntosh)")
    parser.add_argument("--state",   default="Georgia", help="State name (default: Georgia)")
    parser.add_argument("--year",    default=1870, type=int, help="Census year (default: 1870)")
    parser.add_argument("--output",  default=None, help="Output JSON path (optional)")
    parser.add_argument("--no-db",   action="store_true", help="Skip database write")
    args = parser.parse_args()

    run_pipeline(
        image_path=args.image,
        county=args.county,
        state=args.state,
        year=args.year,
        output_path=args.output,
        save_db=not args.no_db,
    )


if __name__ == "__main__":
    main()
