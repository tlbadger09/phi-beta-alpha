#!/usr/bin/env python3
"""
Phi Beta Alpha — Multi-State 1870 Census OCR Pipeline

Downloads Internet Archive M593 microfilm reels for target states and OCRs
them locally with Tesseract (zero API cost). Supports an optional in-session
mode where Claude reads each page image directly and writes a JSON sidecar
that the pipeline then ingests.

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
  python3 multi_state_pipeline.py --reel 1147 --state "South Carolina" --provider insession
"""

import os
import re
import sys
import json
import time
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
DB_PATH      = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"
CACHE_DIR    = Path.home() / "Documents/phi-beta-alpha/output/multi_state_reels"
STAGING_DIR  = Path.home() / "Documents/phi-beta-alpha/output/insession_staging"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(parents=True, exist_ok=True)

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

# OCR_PROMPT kept as reference for in-session transcription instructions
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
    """Convert JP2 bytes to JPEG bytes. 1600px max keeps text legible."""
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


def _preprocess_for_tesseract(img: Image.Image) -> Image.Image:
    """Grayscale + contrast boost for better Tesseract reads on census documents."""
    from PIL import ImageEnhance, ImageFilter
    if img.mode != "L":
        img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_page_tesseract(jpeg_bytes: bytes) -> list[dict]:
    """
    Primary OCR engine — Tesseract local, zero API cost.

    Uses word-level bounding boxes (image_to_data) to group words by line,
    then applies census-layout heuristics to extract name / age / sex / color.
    Accuracy on 19th-century cursive is low but sufficient for bulk indexing.
    """
    try:
        import pytesseract

        img = Image.open(io.BytesIO(jpeg_bytes))
        img = _preprocess_for_tesseract(img)

        data = pytesseract.image_to_data(
            img,
            config="--psm 6 --oem 3",
            output_type=pytesseract.Output.DICT,
        )

        # Group words by Tesseract line_num, sorted by x position
        lines: dict[int, list[tuple[int, str, int]]] = {}
        for i, text in enumerate(data["text"]):
            word = text.strip()
            conf = int(data["conf"][i])
            if not word or conf < 0:
                continue
            ln = data["line_num"][i]
            x = data["left"][i] + data["width"][i] // 2
            lines.setdefault(ln, []).append((x, word, conf))

        COLOR_VALS = {"B", "W", "M", "C", "I"}
        SEX_VALS   = {"M", "F"}
        HEADER_KW  = {"name", "age", "sex", "color", "race", "dwelling", "family",
                      "occupation", "birthplace", "schedule"}

        records = []
        for ln_num in sorted(lines.keys()):
            words = sorted(lines[ln_num], key=lambda t: t[0])
            text_list = [w for _, w, _ in words]

            # Skip column-header and likely-footer lines
            lower = [w.lower() for w in text_list]
            if sum(1 for w in lower if w in HEADER_KW) >= 2:
                continue
            if len(text_list) < 2:
                continue

            # Color field: first single uppercase letter in COLOR_VALS
            color = next(
                (w.upper() for _, w, _ in words if w.upper() in COLOR_VALS and len(w) == 1),
                "",
            )

            # Sex field: first single uppercase M or F not already claimed by color
            sex = next(
                (w.upper() for _, w, _ in words
                 if w.upper() in SEX_VALS and len(w) == 1 and w.upper() != color),
                "",
            )

            # Age: first number in [1, 120]
            age = None
            for _, w, _ in words:
                if w.isdigit() and 1 <= int(w) <= 120:
                    age = int(w)
                    break

            # Name: leftmost 1-3 capitalized alphabetic tokens
            name_words = []
            for _, w, _ in words:
                if w and w[0].isupper() and w.isalpha() and len(w) > 1:
                    name_words.append(w.rstrip(","))
                if len(name_words) >= 3:
                    break

            if not name_words:
                continue
            last_name  = name_words[0]
            first_name = name_words[1] if len(name_words) > 1 else ""

            records.append({
                "line_num":   ln_num,
                "last_name":  last_name,
                "first_name": first_name,
                "age":        age,
                "sex":        sex,
                "color":      color,
                "occupation": "",
                "birthplace": "",
                "_source":    "tesseract",
            })

        return records[:80]  # 1870 census pages have at most ~55 rows

    except ImportError:
        print("    pytesseract not installed — run: pip3 install pytesseract")
        return []
    except Exception as e:
        print(f"    Tesseract error: {e}")
        return []


def ocr_page_insession(page_path: Path) -> list[dict]:
    """
    In-session OCR: read a JSON sidecar written by Claude Code.

    Workflow:
      1. Run pipeline with --provider insession — it saves each page JPEG to
         output/insession_staging/<reel>/ and marks it "insession_pending".
      2. Claude Code reads each image with the Read tool and transcribes rows,
         then writes <page>.json next to the JPEG (format: JSON array of records).
      3. Re-run pipeline with --provider insession — it picks up the sidecars
         and inserts the records.

    Returns [] if no sidecar exists yet (page stays pending).
    """
    sidecar = page_path.with_suffix(".json")
    if not sidecar.exists():
        return []
    try:
        data = json.loads(sidecar.read_text())
        if isinstance(data, list):
            return data
    except Exception as e:
        print(f"    Sidecar read error ({sidecar.name}): {e}")
    return []


def ocr_page(jpeg_bytes: bytes, provider: str = "tesseract",
             page_path: Path | None = None) -> list[dict] | None:
    """
    Dispatch OCR to the chosen provider. Returns record list (possibly empty).
    Never calls any external API — Tesseract is local, insession reads sidecars.
    Returns None only on internal error (treat as transient, not quota).
    """
    if provider == "insession":
        if page_path is None:
            print("    insession mode requires page_path")
            return []
        return ocr_page_insession(page_path)
    # Default: tesseract
    return ocr_page_tesseract(jpeg_bytes)


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
        is_black = 1 if color in ("B", "M") else 0

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
    """Return True if this page should be skipped (success / blank / error)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    row = conn.execute(
        "SELECT status FROM pipeline_progress WHERE reel_number=? AND page_number=?",
        (reel_num, page_num)
    ).fetchone()
    conn.close()
    if row is None:
        return False
    return row[0] in ("success", "blank", "error")


def mark_page_progress(reel_num: int, page_num: int, state: str, county: str,
                       status: str, records: int = 0) -> None:
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
                 workers: int = 2, max_pages: int = None,
                 provider: str = "tesseract") -> dict:
    """Download and process one reel. Returns summary dict."""
    print(f"\n{'='*60}")
    print(f"PROCESSING: {state} — Reel {reel_num} ({county_desc})")
    print(f"Provider: {provider}")
    print(f"{'='*60}")

    zip_path = download_reel_zip(reel_num, state)
    if not zip_path:
        return {"reel": reel_num, "state": state, "error": "download failed", "total": 0, "black": 0}

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        print(f"  Bad ZIP file: {e}")
        return {"reel": reel_num, "state": state, "error": str(e), "total": 0, "black": 0}

    jp2_files = sorted([
        n for n in zf.namelist()
        if n.endswith('.jp2') and not n.endswith('_thumb.jp2')
    ])

    if max_pages:
        jp2_files = jp2_files[:max_pages]

    total_pages = len(jp2_files)
    print(f"  Pages in reel: {total_pages}")

    # Staging directory for in-session mode
    stage_dir = STAGING_DIR / str(reel_num)
    if provider == "insession":
        stage_dir.mkdir(parents=True, exist_ok=True)

    total_records  = 0
    black_records  = 0
    processed_pages = 0
    skipped_pages  = 0
    pending_pages  = 0  # in-session: pages saved but no sidecar yet

    # Use first part of county_desc as working county name
    current_county = county_desc.split("(")[0].split("-")[0].strip()
    lock = threading.Lock()

    def process_page(entry_name: str) -> tuple[int, int, str]:
        """Process one page. Returns (total, black, county)."""
        try:
            page_num = int(entry_name.split("_")[-1].replace(".jp2", ""))
        except (ValueError, IndexError):
            return 0, 0, ""

        if is_page_already_processed(reel_num, page_num):
            return -1, -1, ""  # -1 = skipped

        try:
            jp2_data  = zf.read(entry_name)
            jpeg_data = jp2_to_jpeg_bytes(jp2_data)
            if not jpeg_data:
                return 0, 0, ""
        except Exception as e:
            print(f"    Page {page_num}: read error {e}")
            return 0, 0, ""

        page_county = current_county
        page_path   = None

        if provider == "insession":
            # Save JPEG for Claude Code to read and transcribe
            page_path = stage_dir / f"page_{page_num:04d}.jpg"
            if not page_path.exists():
                page_path.write_bytes(jpeg_data)
            records = ocr_page(jpeg_data, provider="insession", page_path=page_path)
            if not records:
                # No sidecar yet — mark pending so the pipeline skips on next pass
                # (not "insession_pending" — use quota_error so reset_quota_errors can clear)
                mark_page_progress(reel_num, page_num, state, page_county, "quota_error", 0)
                return -2, -2, page_county  # -2 = pending
        else:
            records = ocr_page(jpeg_data, provider=provider)

        if records is None or not records:
            mark_page_progress(reel_num, page_num, state, page_county, "blank", 0)
            return 0, 0, page_county

        t, b = save_records_to_db(records, state, page_county, reel_num, page_num)
        mark_page_progress(reel_num, page_num, state, page_county, "success", t)
        return t, b, page_county

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_page, e): e for e in jp2_files}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                t, b, county = future.result()
                with lock:
                    if t == -1:
                        skipped_pages += 1
                    elif t == -2:
                        pending_pages += 1
                    else:
                        total_records  += t
                        black_records  += b
                        processed_pages += 1
                        if county:
                            current_county = county
                if i % 25 == 0:
                    print(f"  Progress: {i}/{total_pages} pages | "
                          f"{total_records} records ({black_records} Black) | "
                          f"{skipped_pages} skipped | {pending_pages} pending")
            except Exception as e:
                print(f"  Page error: {e}")

    summary = {
        "reel":          reel_num,
        "state":         state,
        "county":        current_county,
        "provider":      provider,
        "total_pages":   total_pages,
        "processed":     processed_pages,
        "skipped":       skipped_pages,
        "pending":       pending_pages,
        "total_records": total_records,
        "black_records": black_records,
        "timestamp":     datetime.datetime.now().isoformat(),
    }

    print(f"\n  DONE: Reel {reel_num} ({state})")
    print(f"  Processed {processed_pages} pages, {total_records} records, {black_records} Black")
    if pending_pages:
        print(f"  {pending_pages} pages saved to {stage_dir} — transcribe with Claude Code, then re-run")
    log_path = CACHE_DIR / f"reel_{reel_num}_{state.lower().replace(' ','_')}_summary.json"
    log_path.write_text(json.dumps(summary, indent=2))

    return summary


def main():
    parser = argparse.ArgumentParser(description="PBA Multi-State 1870 Census Pipeline (Tesseract, zero API cost)")
    parser.add_argument("--state",    help="Process one state (e.g. 'South Carolina')")
    parser.add_argument("--all",      action="store_true", help="Process all target states")
    parser.add_argument("--reel",     type=int, help="Process a specific reel number")
    parser.add_argument("--workers",  type=int, default=3, help="Parallel page workers (default 3)")
    parser.add_argument("--max-pages",type=int, default=None, help="Max pages per reel (for testing)")
    parser.add_argument("--priority", type=int, default=2,
                        help="Max priority to process (1=highest only, 2=top two, 3=all)")
    parser.add_argument("--provider", default="tesseract", choices=["tesseract", "insession"],
                        help="OCR provider: tesseract (default) or insession (sidecar JSON)")
    args = parser.parse_args()

    if args.reel and args.state:
        county_desc = f"Reel {args.reel}"
        for reel, desc, _ in REEL_CATALOG.get(args.state, []):
            if reel == args.reel:
                county_desc = desc
                break
        process_reel(args.reel, args.state, county_desc, args.workers, args.max_pages, args.provider)
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
    for state in states_to_process:
        reels = [r for r in REEL_CATALOG.get(state, []) if r[2] <= args.priority]
        if not reels:
            print(f"No reels configured for: {state}")
            continue
        print(f"\n{'#'*60}")
        print(f"STATE: {state} ({len(reels)} reels at priority ≤ {args.priority})")
        print(f"{'#'*60}")
        for reel_num, county_desc, priority in reels:
            summary = process_reel(reel_num, state, county_desc, args.workers,
                                   args.max_pages, args.provider)
            all_summaries.append(summary)

    master_log = CACHE_DIR / f"pipeline_run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    master_log.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nPipeline complete. Summary: {master_log}")

    total_records = sum(s.get("total_records", 0) for s in all_summaries)
    total_black   = sum(s.get("black_records", 0) for s in all_summaries)
    print(f"Total records added: {total_records:,} ({total_black:,} Black)")


if __name__ == "__main__":
    main()
