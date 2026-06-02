#!/usr/bin/env python3
"""
Phi Beta Alpha — Multi-State 1870 Census OCR Pipeline

Downloads Internet Archive M593 microfilm reels for target states,
OCRs them with GPT-4o Vision, and saves named records to census_ocr_1870.

NARA M593 (1870 Population Schedules) reel ranges:
  Alabama:        1-45       Florida:       120-125
  South Carolina: 1134-1162  North Carolina: 922-958
  Mississippi:    638-685    Kentucky:      394-452
  Louisiana:      453-491    Virginia:      1264-1332
  New York:       796-921    Georgia:       126-173 (already in census_ocr_georgia_1870)

Usage:
  python3 multi_state_pipeline.py --state "South Carolina" [--workers 4]
  python3 multi_state_pipeline.py --all [--workers 4]
  python3 multi_state_pipeline.py --reel 1147 --state "South Carolina"
"""

import os
import re
import sys
import json
import time
import base64
import sqlite3
import hashlib
import zipfile
import io
import argparse
import datetime
import threading
import urllib.request
import ssl
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# macOS SSL fix
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

from PIL import Image

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH   = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"
CACHE_DIR = Path.home() / "Documents/phi-beta-alpha/output/multi_state_reels"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── AI client setup (Claude primary, OpenAI fallback) ─────────────────────────
def _read_key_from_file(varname, filepath):
    p = Path(filepath)
    if p.exists():
        for line in p.read_text().splitlines():
            if varname in line and "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key and not key.startswith("$"):
                    return key
    return None


def _find_key(varname):
    return (
        os.environ.get(varname)
        or _read_key_from_file(varname, Path(__file__).parent.parent / ".env")
        or _read_key_from_file(varname, Path.home() / ".zshrc")
    )


def get_claude_client():
    """Return Anthropic client. Raises if key not found."""
    import anthropic
    api_key = _find_key("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not found")
    return anthropic.Anthropic(api_key=api_key)


def get_openai_client():
    import openai
    api_key = _find_key("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not found")
    return openai.OpenAI(api_key=api_key)


def get_ai_client():
    """Return (client, provider). Prefers Claude, falls back to OpenAI."""
    try:
        client = get_claude_client()
        return client, "claude"
    except Exception:
        pass
    try:
        client = get_openai_client()
        return client, "openai"
    except Exception:
        pass
    raise RuntimeError("No AI API key found (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")


# ── State FIPS ─────────────────────────────────────────────────────────────────
STATE_FIPS = {
    "Alabama": 1, "Florida": 12, "Georgia": 13, "Kentucky": 21,
    "Louisiana": 22, "Mississippi": 28, "New York": 36, "North Carolina": 37,
    "South Carolina": 45, "Virginia": 51,
}

# ── NARA M593 reel catalog: state → [(reel_num, county_coverage, priority)] ───
# Priority 1 = highest Black population counties, process first
REEL_CATALOG = {
    "Alabama": [
        (8,   "Dallas County (Selma area)",     1),
        (15,  "Limestone-Lowndes Counties",     1),
        (16,  "Macon County",                   1),
        (17,  "Madison-Marengo Counties",        2),
        (19,  "Morgan-Perry Counties",           2),
        (11,  "Hale-Henry Counties",             2),
        (3,   "Calhoun-Choctaw Counties",        3),
        (6,   "Coosa-Covington Counties",        3),
    ],
    "Florida": [
        (121, "Gadsden-Jefferson Counties",      1),
        (122, "Lafayette-Leon Counties",         1),
        (120, "Alachua-Escambia Counties",       2),
        (123, "Levy-Madison Counties",           2),
        (124, "Manatee-St. Johns Counties",      3),
        (125, "Santa Rosa-Walton Counties",      3),
    ],
    "South Carolina": [
        (1135, "Beaufort County (Sea Islands)",  1),
        (1136, "Charleston County",              1),
        (1147, "Hampton-Horry Counties",         1),  # Badger family area
        (1137, "Chester-Chesterfield Counties",  2),
        (1140, "Darlington-Edgefield Counties",  2),
        (1148, "Kershaw-Lancaster Counties",     2),
        (1134, "Abbeville-Anderson Counties",    3),
        (1139, "Colleton County",                1),
    ],
    "North Carolina": [
        (928,  "Edgecombe County",               1),
        (929,  "Franklin-Granville Counties",    1),
        (934,  "Halifax County",                 1),
        (940,  "Northampton County",             1),
        (944,  "Vance-Warren Counties",          1),
        (922,  "Alamance-Anson Counties",        2),
        (930,  "Greene-Hertford Counties",       2),
        (936,  "Lenoir-Mecklenburg Counties",    2),
    ],
    "Mississippi": [
        (638,  "Adams County (Natchez)",         1),
        (640,  "Bolivar-Claiborne Counties",     1),
        (644,  "Copiah-Holmes Counties",         1),
        (646,  "Issaquena-Jefferson Counties",   1),
        (650,  "Lowndes-Monroe Counties",        2),
        (656,  "Sunflower-Tallahatchie Counties",2),
        (658,  "Tishomingo-Warren Counties",     1),
        (660,  "Washington County",              1),
    ],
    "Kentucky": [
        (408,  "Jefferson County (Louisville)",  1),
        (394,  "Adair-Bourbon Counties",         2),
        (396,  "Boyd-Butler Counties",           2),
        (404,  "Henry-Jefferson Counties",       2),
        (412,  "LaRue-Lincoln Counties",         2),
        (420,  "McCracken-Marion Counties",      2),
    ],
    "Louisiana": [
        (453,  "Ascension-Assumption Parishes",  1),
        (455,  "Bossier-Caddo Parishes",         2),
        (458,  "East Baton Rouge Parish",        1),
        (462,  "Iberia-Iberville Parishes",      1),
        (468,  "Jefferson-Lafourche Parishes",   1),
        (471,  "Orleans Parish (New Orleans)",   1),
        (479,  "St. Mary Parish",                1),
        (483,  "Tensas Parish",                  1),
    ],
    "Virginia": [
        (1285, "Mecklenburg County",             1),
        (1280, "Lunenburg-Mecklenburg Counties", 1),
        (1275, "Isle of Wight-King William",     1),
        (1295, "Prince George-Prince William",   2),
        (1264, "Accomac-Albemarle Counties",     2),
        (1270, "Brunswick-Campbell Counties",    1),
        (1300, "Richmond County area",           2),
        (1305, "Roanoke-Rockbridge Counties",    2),
    ],
    "New York": [
        (870,  "New York City (Manhattan) Pt 1", 1),
        (871,  "New York City (Manhattan) Pt 2", 1),
        (872,  "New York City (Manhattan) Pt 3", 1),
        (860,  "Kings County (Brooklyn) Pt 1",  1),
        (861,  "Kings County (Brooklyn) Pt 2",  1),
        (796,  "Albany County",                  2),
        (850,  "Erie County (Buffalo)",          2),
        (866,  "New York (Manhattan) - misc",    2),
    ],
}


# ── OCR Prompt (same as census_ocr.py) ────────────────────────────────────────
OCR_PROMPT = """You are an expert genealogist transcribing a handwritten US Federal Census page from 1870.

Extract EVERY person listed. Output a JSON array, each element:
{
  "line_num": <integer>,
  "dwelling": <integer or null>,
  "family": <integer or null>,
  "last_name": <string — repeat last name if ditto marks appear>,
  "first_name": <string>,
  "age": <integer or null>,
  "sex": <"M" or "F">,
  "color": <"W", "B", "M", "C", or "I">,
  "occupation": <string or null>,
  "birthplace": <string>,
  "real_estate_value": <integer or null>,
  "personal_estate_value": <integer or null>,
  "notes": <string or null>
}

Rules:
- Ditto marks mean repeat value from line above
- Abbreviations: Wm=William, Thos=Thomas, Chas=Charles, Jas=James, Geo=George, Jno=John
- Color: B=Black, W=White, M=Mulatto, C=Chinese, I=Indian
- Output ONLY the JSON array, no other text
"""


def soundex(name):
    name = name.upper().strip()
    if not name:
        return "0000"
    code_map = {"BFPV": "1", "CGJKQSXYZ": "2", "DT": "3",
                "L": "4", "MN": "5", "R": "6"}
    def char_code(c):
        for keys, val in code_map.items():
            if c in keys:
                return val
        return "0"
    first = name[0]
    result = first
    prev = char_code(first)
    for ch in name[1:]:
        c = char_code(ch)
        if c != "0" and c != prev:
            result += c
        prev = c if c != "0" else prev
    return result.ljust(4, "0")[:4]


def download_reel_zip(reel_num: int, state: str) -> Path | None:
    """Download the reel ZIP from Internet Archive if not cached."""
    item_id  = f"populationschedu{reel_num:04d}unit"
    zip_name = f"{item_id}_jp2.zip"
    cache_path = CACHE_DIR / zip_name

    if cache_path.exists() and cache_path.stat().st_size > 1_000_000:
        print(f"  [Reel {reel_num}] Using cached ZIP: {cache_path}")
        return cache_path

    url = f"https://archive.org/download/{item_id}/{zip_name}"
    print(f"  [Reel {reel_num}] Downloading {url} ...")

    # Try curl first (avoids Python SSL issues on macOS)
    try:
        result = subprocess.run(
            ["curl", "-L", "--silent", "--show-error", "-o", str(cache_path),
             "--retry", "3", "--retry-delay", "5",
             "-A", "PBALineageBot/1.0", url],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 100_000:
            print(f"  [Reel {reel_num}] Download complete: {cache_path.stat().st_size/1e6:.1f} MB")
            return cache_path
        else:
            err = result.stderr.strip()
            print(f"  [Reel {reel_num}] curl failed (rc={result.returncode}): {err}")
            # Check for 404 (reel doesn't exist on IA)
            if "404" in err or (cache_path.exists() and cache_path.stat().st_size < 1000):
                print(f"  [Reel {reel_num}] Not found on Internet Archive — skipping")
                if cache_path.exists():
                    cache_path.unlink()
                return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  [Reel {reel_num}] curl error: {e}, falling back to urllib")

    # Fallback: urllib with SSL bypass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PBALineageBot/1.0"})
        with urllib.request.urlopen(req, timeout=300, context=SSL_CTX) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 1024
            with open(cache_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        print(f"    {downloaded/1e6:.1f}/{total/1e6:.1f} MB ({downloaded/total*100:.0f}%)", end="\r")
        print(f"\n  [Reel {reel_num}] Download complete: {cache_path.stat().st_size/1e6:.1f} MB")
        return cache_path
    except Exception as e:
        print(f"  [Reel {reel_num}] Download failed: {e}")
        if cache_path.exists():
            cache_path.unlink()
        return None


def jp2_to_jpeg_bytes(jp2_data: bytes) -> bytes | None:
    """Convert JP2 bytes to JPEG bytes for OCR. 1600px max keeps text legible at ~1/6 the token cost of 4096px."""
    try:
        img = Image.open(io.BytesIO(jp2_data))
        max_dim = 1600
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception as e:
        print(f"    JP2 conversion error: {e}")
        return None


def ocr_page_claude(client, jpeg_bytes: bytes) -> list[dict] | None:
    """OCR via Claude. Returns None on quota/credit errors, [] on blank page, list on success."""
    import anthropic
    b64 = base64.b64encode(jpeg_bytes).decode()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }],
        )
        raw = resp.content[0].text.strip()
        return parse_ocr_response(raw)
    except Exception as e:
        err = str(e).lower()
        if "credit" in err or "quota" in err or "balance" in err or "billing" in err:
            print(f"    OCR quota/credit error (Claude): {e}")
            return None  # None signals quota error — stop retrying
        print(f"    OCR error (Claude): {e}")
        return []


def ocr_page_openai(client, jpeg_bytes: bytes) -> list[dict] | None:
    """OCR via OpenAI GPT-4o. Returns None on quota errors, [] on blank, list on success."""
    b64 = base64.b64encode(jpeg_bytes).decode()
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "high",
                    }},
                ],
            }],
            max_tokens=16000,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        return parse_ocr_response(raw)
    except Exception as e:
        err = str(e).lower()
        if "quota" in err or "insufficient" in err or "billing" in err or "429" in str(e):
            print(f"    OCR quota error (OpenAI): {e}")
            return None
        print(f"    OCR error (OpenAI): {e}")
        return []


def ocr_page(client, jpeg_bytes: bytes, county: str, state: str,
             provider: str = "claude") -> list[dict]:
    """Run OCR on a single census page image using appropriate client."""
    if provider == "claude":
        return ocr_page_claude(client, jpeg_bytes)
    else:
        return ocr_page_openai(client, jpeg_bytes)


def parse_ocr_response(raw: str) -> list[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        for key in ("records", "persons", "entries", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []
    except json.JSONDecodeError:
        # Try to recover truncated response
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
            try:
                data = json.loads(cleaned[:last_complete] + "\n]")
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []


def detect_county_from_page(client, jpeg_bytes: bytes, provider: str = "claude") -> str | None:
    """Quick scan to detect county/state from census page header."""
    b64 = base64.b64encode(jpeg_bytes).decode()
    COUNTY_PROMPT = (
        "This is a US 1870 census page. "
        "What county and state are printed at the top of the page? "
        "Reply with ONLY: County: X, State: Y"
    )
    try:
        if provider == "claude":
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": b64
                        }},
                        {"type": "text", "text": COUNTY_PROMPT},
                    ],
                }],
            )
            return resp.content[0].text.strip()
        else:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": COUNTY_PROMPT},
                    ],
                }],
                max_tokens=50, temperature=0,
            )
            return resp.choices[0].message.content.strip()
    except Exception:
        return None


def save_records_to_db(records: list[dict], state: str, county: str,
                        reel_num: int, page_num: int) -> tuple[int, int]:
    """Save OCR records to census_ocr_1870 table. Returns (total, black)."""
    if not records:
        return 0, 0

    state_fips = STATE_FIPS.get(state)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)

    total_saved = 0
    black_saved = 0
    ts = datetime.datetime.now().isoformat()

    for rec in records:
        last_name  = (rec.get("last_name") or "").strip()
        first_name = (rec.get("first_name") or "").strip()
        if not last_name:
            continue

        color = (rec.get("color") or "").upper().strip()
        is_black = 1 if color in ("B", "M") else 0  # Black + Mulatto

        last_sdx  = soundex(last_name) if last_name else None
        first_sdx = soundex(first_name) if first_name else None

        try:
            conn.execute("""
                INSERT OR IGNORE INTO census_ocr_1870
                (state, state_fips, county, reel_number, page_number,
                 household_num, entry_num, last_name, first_name,
                 last_soundex, first_soundex, age, sex, race,
                 occupation, birthplace, real_estate_val, personal_est_val,
                 relationship, is_black, processed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                state, state_fips, county, reel_num, page_num,
                rec.get("dwelling"), rec.get("line_num"),
                last_name, first_name,
                last_sdx, first_sdx,
                str(rec.get("age") or ""),
                rec.get("sex", ""),
                color,
                rec.get("occupation", ""),
                rec.get("birthplace", ""),
                str(rec.get("real_estate_value") or ""),
                str(rec.get("personal_estate_value") or ""),
                None,
                is_black,
                ts,
            ))
            total_saved += 1
            if is_black:
                black_saved += 1
        except sqlite3.Error:
            pass

    conn.commit()
    conn.close()
    return total_saved, black_saved


def is_page_already_processed(reel_num: int, page_num: int) -> bool:
    """Check if this page has been successfully OCR'd (success or blank, not quota_error)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    row = conn.execute(
        "SELECT status FROM pipeline_progress WHERE reel_number=? AND page_number=?",
        (reel_num, page_num)
    ).fetchone()
    conn.close()
    if row is None:
        return False
    # Re-process pages that got quota errors; skip successful and blank pages
    return row[0] in ("success", "blank", "error")


def mark_page_progress(reel_num: int, page_num: int, state: str, county: str,
                       status: str, records: int = 0) -> None:
    """Record page processing status in pipeline_progress."""
    ts = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("""
        INSERT INTO pipeline_progress (reel_number, page_number, state, county, status, records_saved, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(reel_number, page_number)
        DO UPDATE SET status=excluded.status, records_saved=excluded.records_saved, processed_at=excluded.processed_at
    """, (reel_num, page_num, state, county, status, records, ts))
    conn.commit()
    conn.close()


def process_reel(reel_num: int, state: str, county_desc: str,
                 workers: int = 2, max_pages: int = None) -> dict:
    """
    Download and process one reel. Returns summary dict.
    """
    print(f"\n{'='*60}")
    print(f"PROCESSING: {state} — Reel {reel_num} ({county_desc})")
    print(f"{'='*60}")

    zip_path = download_reel_zip(reel_num, state)
    if not zip_path:
        return {"reel": reel_num, "state": state, "error": "download failed", "total": 0, "black": 0}

    client, provider = get_ai_client()
    print(f"  Using AI provider: {provider}")

    # Get page list from ZIP
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        print(f"  Bad ZIP file: {e}")
        return {"reel": reel_num, "state": state, "error": str(e), "total": 0, "black": 0}

    item_id  = f"populationschedu{reel_num:04d}unit"
    jp2_files = sorted([
        n for n in zf.namelist()
        if n.endswith('.jp2') and not n.endswith('_thumb.jp2')
    ])

    if max_pages:
        jp2_files = jp2_files[:max_pages]

    total_pages = len(jp2_files)
    print(f"  Pages in reel: {total_pages}")

    total_records = 0
    black_records = 0
    processed_pages = 0
    skipped_pages = 0
    quota_errors = 0

    # Detect the actual county from the first readable page header
    detected_county = county_desc.split("(")[0].split("-")[0].strip()
    current_county  = detected_county

    lock = threading.Lock()

    def process_page(entry_name: str) -> tuple[int, int, str]:
        """Process one page. Returns (total, black, county)."""
        # Extract page number from filename
        try:
            page_num = int(entry_name.split("_")[-1].replace(".jp2", ""))
        except (ValueError, IndexError):
            return 0, 0, ""

        if is_page_already_processed(reel_num, page_num):
            return -1, -1, ""  # -1 signals "skipped"

        try:
            jp2_data  = zf.read(entry_name)
            jpeg_data = jp2_to_jpeg_bytes(jp2_data)
            if not jpeg_data:
                return 0, 0, ""
        except Exception as e:
            print(f"    Page {page_num}: read error {e}")
            return 0, 0, ""

        # For every 50th page, detect county from header
        page_county = current_county
        if page_num % 50 == 1:
            header = detect_county_from_page(client, jpeg_data, provider)
            if header:
                m = re.search(r"County:\s*([^,]+)", header, re.I)
                if m:
                    page_county = m.group(1).strip()

        records = ocr_page(client, jpeg_data, page_county, state, provider)
        if records is None:
            # None = quota/credit error — mark as quota_error so we retry later
            mark_page_progress(reel_num, page_num, state, page_county, "quota_error", 0)
            return -2, -2, page_county  # -2 signals quota error
        if not records:
            mark_page_progress(reel_num, page_num, state, page_county, "blank", 0)
            return 0, 0, page_county

        t, b = save_records_to_db(records, state, page_county, reel_num, page_num)
        mark_page_progress(reel_num, page_num, state, page_county, "success", t)
        return t, b, page_county

    # Process pages with thread pool
    stop_flag = threading.Event()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_page, e): e for e in jp2_files}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                t, b, county = future.result()
                with lock:
                    if t == -1:
                        skipped_pages += 1
                    elif t == -2:
                        # Quota error — cancel remaining futures
                        quota_errors += 1
                        if quota_errors >= 3 and not stop_flag.is_set():
                            print(f"\n  QUOTA ERROR: API credits exhausted after {processed_pages} pages.")
                            print(f"  Add credits and re-run — completed pages are saved and will be skipped.")
                            stop_flag.set()
                            for f in futures:
                                f.cancel()
                    else:
                        total_records += t
                        black_records += b
                        processed_pages += 1
                        if county:
                            current_county = county
                    if i % 25 == 0:
                        print(f"  Progress: {i}/{total_pages} pages | "
                              f"{total_records} records ({black_records} Black) | "
                              f"{skipped_pages} skipped")
            except Exception as e:
                print(f"  Page error: {e}")

    summary = {
        "reel":          reel_num,
        "state":         state,
        "county":        current_county,
        "total_pages":   total_pages,
        "processed":     processed_pages,
        "skipped":       skipped_pages,
        "quota_errors":  quota_errors,
        "total_records": total_records,
        "black_records": black_records,
        "timestamp":     datetime.datetime.now().isoformat(),
    }

    print(f"\n  DONE: Reel {reel_num} ({state})")
    print(f"  Processed {processed_pages} pages, {total_records} records, {black_records} Black")
    log_path = CACHE_DIR / f"reel_{reel_num}_{state.lower().replace(' ','_')}_summary.json"
    log_path.write_text(json.dumps(summary, indent=2))

    return summary


def check_credits():
    """Test API connectivity and credit status."""
    print("Checking API credits...")
    try:
        client = get_claude_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "Hi"}],
        )
        print("  Anthropic (Claude): OK — credits available")
        return True
    except Exception as e:
        err = str(e)
        if "credit" in err.lower() or "balance" in err.lower():
            print(f"  Anthropic (Claude): CREDITS EXHAUSTED — {e}")
        else:
            print(f"  Anthropic (Claude): Error — {e}")

    try:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=5,
            messages=[{"role": "user", "content": "Hi"}],
        )
        print("  OpenAI (GPT-4o): OK — credits available")
        return True
    except Exception as e:
        err = str(e)
        if "quota" in err.lower() or "insufficient" in err.lower():
            print(f"  OpenAI (GPT-4o): QUOTA EXHAUSTED — {e}")
        else:
            print(f"  OpenAI (GPT-4o): Error — {e}")

    print("\n  Both providers unavailable. Add credits before running the pipeline.")
    return False


def main():
    parser = argparse.ArgumentParser(description="PBA Multi-State 1870 Census Pipeline")
    parser.add_argument("--state",   help="Process one state (e.g. 'South Carolina')")
    parser.add_argument("--all",     action="store_true", help="Process all target states")
    parser.add_argument("--reel",    type=int, help="Process a specific reel number")
    parser.add_argument("--workers", type=int, default=3, help="Parallel page workers (default 3)")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages per reel (for testing)")
    parser.add_argument("--priority", type=int, default=2, help="Max priority to process (1=highest only, 2=top two, 3=all)")
    parser.add_argument("--check-credits", action="store_true", help="Test API credit status and exit")
    args = parser.parse_args()

    if args.check_credits:
        check_credits()
        return

    if args.reel and args.state:
        # Single reel mode
        county_desc = f"Reel {args.reel}"
        for reel, desc, _ in REEL_CATALOG.get(args.state, []):
            if reel == args.reel:
                county_desc = desc
                break
        process_reel(args.reel, args.state, county_desc, args.workers, args.max_pages)
        return

    states_to_process = []
    if args.all:
        states_to_process = list(REEL_CATALOG.keys())
    elif args.state:
        states_to_process = [args.state]
    else:
        parser.print_help()
        return

    all_summaries = []
    global_quota_stop = False  # set True when a reel hits quota — stop all further reels
    for state in states_to_process:
        if global_quota_stop:
            print(f"\n  GLOBAL QUOTA STOP: skipping {state} and all remaining states.")
            print(f"  Add credits and re-run — completed pages are saved and will be skipped.")
            break
        reels = [r for r in REEL_CATALOG.get(state, []) if r[2] <= args.priority]
        if not reels:
            print(f"No reels configured for: {state}")
            continue
        print(f"\n{'#'*60}")
        print(f"STATE: {state} ({len(reels)} reels at priority ≤ {args.priority})")
        print(f"{'#'*60}")
        for reel_num, county_desc, priority in reels:
            if global_quota_stop:
                break
            summary = process_reel(reel_num, state, county_desc, args.workers, args.max_pages)
            all_summaries.append(summary)
            # If this reel hit quota errors and processed 0 pages, stop everything
            if summary.get("quota_errors", 0) >= 3 and summary.get("processed", 0) == 0:
                print(f"\n  QUOTA CONFIRMED EXHAUSTED on Reel {reel_num} ({state}).")
                print(f"  Stopping pipeline. Add credits and re-run.")
                global_quota_stop = True
                break

    # Write master summary
    master_log = CACHE_DIR / f"pipeline_run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    master_log.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nPipeline complete. Summary: {master_log}")

    total_records = sum(s.get("total_records", 0) for s in all_summaries)
    total_black   = sum(s.get("black_records", 0) for s in all_summaries)
    print(f"Total records added: {total_records:,} ({total_black:,} Black)")


if __name__ == "__main__":
    main()
