# Phi Beta Alpha — Lineage Check

**Documentary lineage verification for Foundational Black Americans.**

Lineage Check searches digitized 1870 U.S. Federal Census records, IPUMS linked microdata, and Freedmen's Bureau records to verify direct descent from enslaved Black Americans. It produces tiered confidence scores, printable verification certificates, and an anchored backward-walk engine that chains living family memory to 1870 census records decade by decade.

> "You don't join it, you were born it."

---

## What it does

| Feature | Description |
|---------|-------------|
| **Name Search** | Phonetic search across 1.2M+ named OCR records in 10 states |
| **Household View** | Every match shows the full 1870 census household |
| **IPUMS Verification** | Cross-references against 3.4M IPUMS linked records (Tier 1–3) |
| **Freedmen's Bureau** | 2,500+ patient records from the Darien, GA dispensary (1865–1868) |
| **Backward Walk** | Walks from a living-confirmable anchor backward to 1870, decade by decade |
| **Bridge Engine** | Crosses the emancipation wall using slave schedules, Freedman's Bank, and Bureau records |
| **Certificates** | Printable verification certificates with QR code and NARA citations |
| **Enslaver Mapping** | Links freedpeople surnames to probable enslavers with documentary evidence |

---

## Data coverage (June 2026)

| Source | State | Records | Black Records |
|--------|-------|---------|--------------|
| `census_ocr_georgia_1870` | Georgia | 114,728 | — |
| `census_ocr_1870` | Alabama | 72,969 | 13,267 |
| `census_ocr_1870` | Florida | 24,185 | 1,552 |
| `census_ocr_1870` | South Carolina | 12,561 | 2,334 |
| `bureau_patients` | Georgia (McIntosh Co.) | 2,511 | 2,511 |
| IPUMS census (1870) | All states | 1,147,958 | 220,206+ |

Active indexing: North Carolina, Mississippi, Kentucky, Louisiana, Virginia, New York.

---

## Search pipeline

Queries run through four layers in order, stopping when enough results are found:

1. **Soundex** — phonetic hash on indexed `last_soundex` column; fast, tolerates spelling drift
2. **Nickname expansion** — 70+ 19th-century Black American name mappings
   (`Wash → Washington`, `Patsy → Martha`, `Polly → Mary`, `Viney → Lavinia`, etc.)
3. **Metaphone** — secondary phonetic fallback for OCR variants
4. **Levenshtein edit distance** — catches near-misses Soundex buckets miss (e.g. `Badger → Badgett`)

### Confidence scoring (0–100)

| Component | Points | Signal |
|-----------|--------|--------|
| Name similarity | 0–40 | Soundex + edit-distance weighted |
| IPUMS tier bonus | 0–40 | Tier 3 = 40 · Tier 2 = 28 · Tier 1 = 12 |
| Race confirmation | 0–10 | `is_black` flag on OCR record |
| Match method | 0–10 | exact=10, FTS=7, soundex=5, nickname/metaphone=4, edit=2 |

### Verification tiers

- **Tier 3** — Linked across 1870, 1880, and 1900 census in IPUMS (strongest)
- **Tier 2** — Linked to one additional census year beyond 1870
- **Tier 1** — Single 1870 record (name/age/location match)

---

## Anchored Backward Walk

The Backward Walk engine starts from a person that living family members can confirm — typically someone appearing in the 1950 census — and walks backward decade by decade to 1870.

Each step scores six signals (100 pts total):

| Signal | Points | What it checks |
|--------|--------|----------------|
| Name | 25 | Soundex + edit-distance match |
| Age progression | 20 | Birth year consistent across decades |
| Birthplace | 15 | State of birth stable across records |
| Household continuity | 20 | Shared surname members in household |
| Geography | 15 | Same county, or known Great Migration corridor |
| Sex/race | 5 | Consistency flag |

The **chain product** (confidence compounded across all links) and the **weakest link** are both reported — the weakest link caps the chain's overall trust.

At 1870, the chain hands off to the **Bridge Engine**, which searches slave schedules, Freedman's Bank records, and Freedmen's Bureau labor contracts to cross the emancipation wall.

---

## Setup

```bash
# Python 3.11+ required
pip3 install flask qrcode Pillow pytest

# Set environment variables
export FLASK_SECRET_KEY="your-secret-key"
export ADMIN_PASSWORD="your-admin-password"   # omit to run open in dev mode
export FS_TOKEN="..."                          # optional: FamilySearch API token
```

The app expects the database at `~/Documents/phi-beta-alpha/processed/lineage_1870.db`.

---

## Running

```bash
cd lineage_check
python3 app.py
# → http://localhost:5050
```

---

## Tests

```bash
python3 -m pytest tests/ -v
# 44 tests: Soundex, nicknames, metaphone, search layers, API endpoints, certificate generation
```

---

## OCR pipeline

The pipeline checkpoints progress in `pipeline_progress` — restarting any script is safe, already-processed pages are skipped.

```bash
# Multi-state pipeline (AL, FL, SC, MS, NC, LA, VA, NY)
python3 scripts/multi_state_pipeline.py

# SC-specific reels not yet in corpus: 1135, 1136, 1139
# KY Louisville reels: use downloader with exponential backoff (see DEPLOY.md)

# Database backup (keeps last 7)
python3 backup_db.py
python3 backup_db.py --keep 3
```

---

## Directory layout

```
lineage_check/          Flask web app (routes, search engine, templates, CSS)
scripts/                Pipeline scripts, member lookup, packet generator
processed/              lineage_1870.db + backups/
freedmens_bureau/       Freedmen's Bureau microfilm images (Roll 61)
research/               Chain reports, field notes
tests/                  pytest suite
ARCHITECTURE.md         Full system map
DEPLOY.md               Production deployment guide
```

---

## Mission

Phi Beta Alpha Historical Lineage Society documents and verifies the genealogical lineage of Foundational Black Americans — descendants of American chattel slavery. This dataset is owned by ΦΒΑ. Verified lineage belongs to the member, not a corporate database.

EIN 41-4969999 · 501(c)(3) · phibetaalpha.org
