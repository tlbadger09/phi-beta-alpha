#!/usr/bin/env python3
"""
Phi Beta Alpha — Freedmen's Bureau Records OCR
Processes scanned Bureau images (PNG/JPG) through GPT-4o Vision to extract:
  - Document type (contract, letter, patient register, complaint, etc.)
  - Employer/party names
  - Freedpeople names
  - County, date, contract terms

Usage:
  python3 bureau_ocr.py --images /path/to/pages/*.png \
                        --roll 61 --office "Darien" \
                        [--output results.json] [--db]

Requires: ANTHROPIC_API_KEY in environment
"""

import os
import re
import sys
import json
import base64
import sqlite3
import argparse
import datetime
from pathlib import Path

import anthropic

DB_PATH    = os.path.expanduser("~/Documents/phi-beta-alpha/processed/lineage_1870.db")
OUTPUT_DIR = os.path.expanduser("~/Documents/phi-beta-alpha/output/bureau")

SYSTEM_PROMPT = """You are an expert transcriber of 19th-century handwritten documents from the U.S. Bureau of Refugees, Freedmen, and Abandoned Lands (Freedmen's Bureau), 1865-1872.

Your task is to extract structured information from scanned Bureau documents. These may include:
- Labor contracts between freedpeople and employers
- Letters received/sent by Bureau agents
- Patient registers (dispensary records)
- Complaint registers
- Affidavits
- Orders and circulars

For each document image, return a JSON object with these fields:
{
  "doc_type": "contract|letter|patient_register|complaint|affidavit|order|index|blank|other",
  "date": "YYYY-MM-DD or partial date string as written",
  "county": "county name if mentioned",
  "state": "GA or state if mentioned",
  "office": "Bureau office/agent location if mentioned",
  "employer_names": ["list of employer/planter names — last name first if possible"],
  "freedpeople": [
    {"name": "full name as written", "notes": "any details — age, prior owner, location"}
  ],
  "other_parties": ["other named individuals — Bureau agents, witnesses, etc."],
  "summary": "1-2 sentence description of what this document is and what it shows",
  "raw_text": "your best transcription of the full document text",
  "confidence": "high|medium|low"
}

Important rules:
- For labor contracts, list ALL freedpeople named as laborers
- Capture surname Bacon, DeLegal, Washington, and similar surnames with special care
- If a document is a folded contract exterior/cover page, note "cover page" in doc_type and extract the filing label text
- If the image is blank, a calibration target, or unreadable, set doc_type to "blank" and leave other fields empty
- Return ONLY valid JSON, no markdown fences
"""

def encode_image(path: str, max_px: int = 2048) -> tuple[str, str]:
    from PIL import Image
    import io
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"

def ocr_page(client: anthropic.Anthropic, image_path: str) -> dict:
    b64, mime = encode_image(image_path)

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": b64
                }},
                {"type": "text", "text": "Transcribe and extract structured data from this Freedmen's Bureau document."}
            ]}]
        )
        text = resp.content[0].text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except json.JSONDecodeError:
        return {"doc_type": "parse_error", "raw_text": text, "confidence": "low"}
    except Exception as e:
        return {"doc_type": "error", "error": str(e), "confidence": "low", "raw_text": ""}

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bureau_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            roll            INTEGER,
            office          TEXT,
            page_num        INTEGER,
            image_file      TEXT,
            doc_type        TEXT,
            doc_date        TEXT,
            county          TEXT,
            state           TEXT DEFAULT 'GA',
            summary         TEXT,
            raw_text        TEXT,
            confidence      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bureau_people (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id       INTEGER REFERENCES bureau_records(id),
            role            TEXT,  -- 'freedperson', 'employer', 'agent', 'witness', 'other'
            full_name       TEXT,
            last_name       TEXT,
            first_name      TEXT,
            last_soundex    TEXT,
            notes           TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_last ON bureau_people(last_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bp_soundex ON bureau_people(last_soundex)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_br_county ON bureau_records(county)")
    conn.commit()
    return conn

def soundex(name: str) -> str:
    if not name:
        return "0000"
    name = re.sub(r'[^A-Za-z]', '', name).upper()
    if not name:
        return "0000"
    map_ = {'B':'1','F':'1','P':'1','V':'1',
            'C':'2','G':'2','J':'2','K':'2','Q':'2','S':'2','X':'2','Z':'2',
            'D':'3','T':'3','L':'4','M':'5','N':'5','R':'6'}
    code = name[0]
    prev = map_.get(name[0], '0')
    for ch in name[1:]:
        c = map_.get(ch, '0')
        if c != '0' and c != prev:
            code += c
        prev = c
    return (code + "000")[:4]

def parse_name(full_name: str):
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return parts[-1], " ".join(parts[:-1])
    return full_name, ""

def save_to_db(conn, roll: int, office: str, page_num: int, image_file: str, data: dict):
    cur = conn.execute("""
        INSERT INTO bureau_records
          (roll, office, page_num, image_file, doc_type, doc_date, county, state, summary, raw_text, confidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        roll, office, page_num, image_file,
        data.get("doc_type"), data.get("date"), data.get("county"),
        data.get("state", "GA"), data.get("summary"), data.get("raw_text"),
        data.get("confidence")
    ))
    record_id = cur.lastrowid

    for fp in data.get("freedpeople", []):
        name = fp.get("name", "")
        last, first = parse_name(name)
        conn.execute("""
            INSERT INTO bureau_people (record_id, role, full_name, last_name, first_name, last_soundex, notes)
            VALUES (?,?,?,?,?,?,?)
        """, (record_id, "freedperson", name, last, first, soundex(last), fp.get("notes")))

    for emp in data.get("employer_names", []):
        last, first = parse_name(emp)
        conn.execute("""
            INSERT INTO bureau_people (record_id, role, full_name, last_name, first_name, last_soundex, notes)
            VALUES (?,?,?,?,?,?,?)
        """, (record_id, "employer", emp, last, first, soundex(last), None))

    for other in data.get("other_parties", []):
        last, first = parse_name(other)
        conn.execute("""
            INSERT INTO bureau_people (record_id, role, full_name, last_name, first_name, last_soundex, notes)
            VALUES (?,?,?,?,?,?,?)
        """, (record_id, "other", other, last, first, soundex(last), None))

    conn.commit()
    return record_id

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", nargs="+", required=True)
    parser.add_argument("--roll", type=int, default=0)
    parser.add_argument("--office", default="Unknown")
    parser.add_argument("--output", default=None)
    parser.add_argument("--db", action="store_true")
    parser.add_argument("--skip-blanks", action="store_true", default=True)
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = None
    if args.db:
        conn = init_db(DB_PATH)

    results = []
    image_paths = sorted(args.images)
    total = len(image_paths)

    for i, img_path in enumerate(image_paths):
        page_num = int(re.search(r'(\d{4})\.png', img_path).group(1)) if re.search(r'(\d{4})\.png', img_path) else i
        print(f"[{i+1}/{total}] Page {page_num}: {Path(img_path).name}", flush=True)

        data = ocr_page(client, img_path)
        data["_page"] = page_num
        data["_image"] = img_path

        doc_type = data.get("doc_type", "")
        if args.skip_blanks and doc_type in ("blank",):
            print(f"  → blank/skip")
            continue

        # Flag Bacon hits immediately
        all_names = (
            [fp.get("name","") for fp in data.get("freedpeople",[])] +
            data.get("employer_names",[]) +
            data.get("other_parties",[]) +
            [data.get("raw_text","")]
        )
        bacon_hit = any("bacon" in n.lower() for n in all_names)
        label = "*** BACON HIT ***" if bacon_hit else doc_type

        err = f" ERR:{data['error'][:80]}" if data.get("error") else ""
        print(f"  → {label}{err} | {data.get('date','')} | {data.get('county','')} | "
              f"employers: {data.get('employer_names',[])} | "
              f"freedpeople: {[fp['name'] for fp in data.get('freedpeople',[])]}")

        if conn:
            save_to_db(conn, args.roll, args.office, page_num, img_path, data)

        results.append(data)

    out_path = args.output or os.path.join(OUTPUT_DIR, f"roll{args.roll}_{args.office.lower().replace(' ','_')}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} records → {out_path}")

    # Summary
    bacon_hits = [r for r in results if any("bacon" in str(v).lower() for v in r.values())]
    contracts  = [r for r in results if r.get("doc_type") == "contract"]
    print(f"Contracts found: {len(contracts)}")
    print(f"Bacon mentions: {len(bacon_hits)}")
    if bacon_hits:
        for h in bacon_hits:
            print(f"  PAGE {h['_page']}: {h.get('summary')}")

if __name__ == "__main__":
    main()
