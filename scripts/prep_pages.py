#!/usr/bin/env python3
"""
prep_pages.py — Extract JP2s from NARA reel ZIPs, convert to JPEG, write manifest.

Usage:
  python3 scripts/prep_pages.py --reel 1135 --state "South Carolina"
  python3 scripts/prep_pages.py --reel 1135 --state "South Carolina" --county "Beaufort" --start 50 --end 200

Output:
  output/prepped/<reel_num>/pages/  → JPEG files (page_NNNN.jpg)
  output/prepped/<reel_num>/manifest.json

The manifest lists every extracted page and records which have already been
processed (status: pending/done/blank) so OCR loops are resumable.
"""

import sys
import io
import json
import zipfile
import argparse
import datetime
from pathlib import Path

from PIL import Image

CACHE_DIR  = Path.home() / "Documents/phi-beta-alpha/output/multi_state_reels"
PREP_DIR   = Path.home() / "Documents/phi-beta-alpha/output/prepped"
MAX_DIM    = 1600   # pixels — keeps text legible at ~1/6 the token cost of full res
JPEG_QUAL  = 82


def jp2_to_jpeg(jp2_data: bytes, max_dim: int = MAX_DIM) -> bytes | None:
    try:
        img = Image.open(io.BytesIO(jp2_data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("L")
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUAL)
        return buf.getvalue()
    except Exception as e:
        print(f"  JP2 conversion error: {e}", file=sys.stderr)
        return None


def prep_reel(reel_num: int, state: str, county: str = "",
              start_page: int = 0, end_page: int | None = None,
              force: bool = False) -> dict:
    zip_name = f"populationschedu{reel_num:04d}unit_jp2.zip"
    zip_path = CACHE_DIR / zip_name
    if not zip_path.exists():
        print(f"ZIP not found: {zip_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = PREP_DIR / str(reel_num)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"

    # Load existing manifest if present (resumable)
    if manifest_path.exists() and not force:
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {
            "reel_number": reel_num,
            "state": state,
            "county": county,
            "created_at": datetime.datetime.now().isoformat(),
            "pages": {},
        }

    prefix = f"populationschedu{reel_num:04d}unit_jp2/"

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = sorted(n for n in zf.namelist()
                         if n.startswith(prefix) and n.endswith(".jp2"))

        total = len(members)
        print(f"Reel {reel_num} ({state}): {total} JP2 pages in ZIP")

        if end_page is None:
            end_page = total

        for idx, member in enumerate(members):
            page_num = idx  # 0-indexed matches filename suffix
            if page_num < start_page or page_num >= end_page:
                continue

            page_key = str(page_num)
            out_path = pages_dir / f"page_{page_num:04d}.jpg"

            # Skip already-extracted (unless force)
            if page_key in manifest["pages"] and out_path.exists() and not force:
                continue

            jp2_data = zf.read(member)
            jpeg_data = jp2_to_jpeg(jp2_data)

            if jpeg_data is None:
                manifest["pages"][page_key] = {
                    "page_num": page_num,
                    "path": str(out_path.relative_to(out_dir)),
                    "status": "error",
                }
                continue

            out_path.write_bytes(jpeg_data)
            size_kb = len(jpeg_data) // 1024

            manifest["pages"][page_key] = {
                "page_num": page_num,
                "path": str(out_path.relative_to(out_dir)),
                "size_kb": size_kb,
                "status": "pending",
            }

            done = sum(1 for p in manifest["pages"].values() if p["status"] != "error")
            print(f"  [{done}/{end_page - start_page}] page {page_num:04d} → {size_kb} KB", end="\r")

    print()
    manifest_path.write_text(json.dumps(manifest, indent=2))
    pending = sum(1 for p in manifest["pages"].values() if p["status"] == "pending")
    done    = sum(1 for p in manifest["pages"].values() if p["status"] == "done")
    print(f"Manifest written: {manifest_path}")
    print(f"  {pending} pending, {done} already done, {len(manifest['pages'])} total extracted")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Extract + normalize NARA JP2 reel pages")
    parser.add_argument("--reel",   type=int, required=True)
    parser.add_argument("--state",  required=True)
    parser.add_argument("--county", default="")
    parser.add_argument("--start",  type=int, default=0,    help="First page index (0-based)")
    parser.add_argument("--end",    type=int, default=None,  help="Last page index (exclusive)")
    parser.add_argument("--force",  action="store_true",     help="Re-extract even if already done")
    args = parser.parse_args()

    prep_reel(args.reel, args.state, args.county, args.start, args.end, args.force)


if __name__ == "__main__":
    main()
