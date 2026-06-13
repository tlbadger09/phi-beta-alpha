# Phi Beta Alpha — Lineage Check

Documentary lineage verification tool for Foundational Black American descent.
Searches 1870 US Federal Census OCR records, IPUMS linked microdata, and
Freedmen's Bureau hospitalization records to produce tiered confidence scores.

## Record counts (as of June 2026)

| Source | State | Total Records | Black Records |
|--------|-------|--------------|--------------|
| census_ocr_georgia_1870 | Georgia | 114,728 | — |
| census_ocr_1870 | Alabama | 72,969 | 13,267 |
| census_ocr_1870 | Florida | 24,185 | 1,552 |
| census_ocr_1870 | South Carolina | 12,561 | 2,334 |
| bureau_patients | Georgia | 2,511 | 2,511 |
| IPUMS census (1870) | All | 1,147,958 | 220,206+ |

## Setup

```bash
# Python 3.11+ required
pip3 install flask qrcode Pillow pytest

# Environment (copy and edit)
cp lineage_check/.env.example lineage_check/.env
# Set ADMIN_PASSWORD and FLASK_SECRET_KEY
```

## Running the app

```bash
cd lineage_check
python3 app.py
# Visit http://localhost:5000
```

## Running tests

```bash
python3 -m pytest tests/ -v
# 44 tests: soundex, nicknames, metaphone, search, API, certs, OCR validation
```

## Running a backup

```bash
python3 scripts/backup_db.py          # keeps last 7 backups
python3 scripts/backup_db.py --keep 3 # keeps last 3
# Backups written to processed/backups/
```

## Resuming the OCR pipeline

The pipeline checkpoints progress in `pipeline_progress(reel_number, page_number)`.
Restarting any pipeline script is safe — already-processed pages are skipped.

```bash
# Resume multi-state pipeline (AL/FL/SC/MS/NC/LA/VA/NY)
python3 scripts/multi_state_pipeline.py

# SC-specific reels not yet processed: 1135, 1136, 1139
# KY Louisville reels: use downloader with exponential backoff (see DEPLOY.md)
```

## Search layers (in order)

1. **Soundex** — phonetic hash match on indexed `last_soundex` column
2. **Nickname expansion** — 70+ 19th-century Black American name mappings
   (Wash → Washington, Patsy → Martha/Patricia, Polly → Mary, etc.)
3. **Metaphone** — secondary phonetic fallback for spelling variants
4. **Edit distance** — Levenshtein ≤ 2 for near-miss names

## Confidence scoring (0–100)

| Component | Range | Source |
|-----------|-------|--------|
| Name score | 0–40 | Soundex/edit-distance similarity |
| Tier bonus | 0–40 | IPUMS census linkage (Tier 3=40, 2=28, 1=12) |
| Race bonus | 0–10 | `is_black` flag |
| Method bonus | 0–10 | exact=10, FTS=7, soundex=5, metaphone=4, nickname=4, edit=2 |

## Verification tiers

- **Tier 3** — Individual linked across 1870 + 1880 + 1900 IPUMS (strongest)
- **Tier 2** — Linked to one additional census year
- **Tier 1** — 1870 only (name/age/location match only)

## Directory layout

```
lineage_check/        Flask app + templates + static
scripts/              Pipeline, member lookup, backup
processed/            lineage_1870.db + backups/
research/             Badger chain report, other findings
nara_microfilm/       Downloaded JP2 ZIPs by reel
tests/                pytest suite
ARCHITECTURE.md       Full system map
DEPLOY.md             Production deployment guide
```
