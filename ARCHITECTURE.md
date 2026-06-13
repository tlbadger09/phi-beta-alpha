# Lineage Check — System Architecture

**Generated:** 2026-06-12  
**App port:** 5050  
**Database:** `processed/lineage_1870.db` (955 MB)

---

## Directory Layout

```
phi-beta-alpha/
├── lineage_check/          # Flask web application
│   ├── app.py              # Routes + business logic (980 lines)
│   ├── search.py           # Search engine (793 lines)
│   ├── static/style.css    # All CSS
│   └── templates/          # Jinja2 templates (14 files)
│       ├── base.html           # Nav + layout shell
│       ├── index.html          # Search form + homepage stats
│       ├── results.html        # Search results + household panels
│       ├── members.html        # Verified members list
│       ├── lineage.html        # Ancestor timeline view
│       ├── certificate.html    # Printable verification certificate
│       ├── preview_cert.html   # Provisional cert from search result
│       ├── begin.html          # Verification submission form
│       ├── submission_status.html  # Public submission tracker
│       ├── admin_submissions.html  # Admin review queue
│       ├── explore.html        # County/state data explorer
│       ├── explore_county.html # County surname breakdown
│       ├── browse_county.html  # Paginated county record browser
│       └── status.html         # Pipeline coverage dashboard
├── scripts/
│   ├── multi_state_pipeline.py  # OLD API-based OCR pipeline (uses Claude/OpenAI API)
│   ├── census_ocr.py            # GA-specific OCR (legacy)
│   ├── bureau_ocr.py            # Freedmen's Bureau OCR (legacy)
│   ├── member_lookup.py         # Core Soundex, IPUMS query, scoring functions
│   └── scan_reel162.py          # One-off GA reel scanner
├── processed/
│   ├── lineage_1870.db          # Main SQLite database (955 MB)
│   ├── ingest.py                # IPUMS census table loader
│   ├── georgia_linkage.py       # GA tier linkage builder
│   └── georgia_tier3.py         # GA Tier 3 verification
├── output/
│   ├── multi_state_reels/       # 60 ZIP files (~39 GB), pipeline logs, summaries
│   ├── real_microfilm/          # Extracted sample pages (real_microfilm/)
│   ├── ocr/                     # Legacy per-page OCR JSON outputs
│   ├── bureau/                  # Freedmen's Bureau OCR outputs
│   └── reel_progress/           # Per-reel progress tracking (legacy)
├── freedmens_bureau/roll61/     # Bureau dispensary images (JP2 + PNG)
├── slave_schedule_liberty_1860.pdf
├── slave_schedule_mcintosh_1860.pdf
└── .env                         # API keys (ANTHROPIC_API_KEY removed 2026-06-02)
```

---

## Database Tables

### `census` — 3.4M rows — IPUMS 1870 National Sample
IPUMS linked microdata. No names — demographic only.  
Key columns: `HISTID`, `YEAR`, `STATEFIP`, `COUNTYNHG`, `AGE`, `BIRTHYR`,  
`SEX`, `RACE`, `BPL`, `LINK1870`, `LINK1880`, `LINK1900`

### `census_ocr_georgia_1870` — 114,728 rows — GA Named Records
Pipeline output for Georgia reels. Has names. Fully processed.  
Key columns: `reel_number`, `page_number`, `county`, `household_num`, `entry_num`,  
`last_name`, `first_name`, `last_soundex`, `first_soundex`, `age`, `sex`, `race`, `is_black`

### `census_ocr_1870` — 109,715 rows — Multi-State Named Records
Unified table for all non-GA states. Currently: AL (72,969), FL (24,185), SC (12,561).  
Key columns: same as GA table plus `state`, `state_fips`.  
**Known data quality issue:** county names for AL/FL/SC are corrupted by the AI county
detector in the old pipeline (returning Ohio/Iowa/California county names). State field
is correct; county is unreliable for AL and FL.

### `ocr_records` — 3,179 rows — Legacy GA Named Records
Early structured OCR output for Liberty + McIntosh counties GA. Most reliable name data.  
Key columns: `ocr_id`, `last_name_raw`, `first_name_raw`, `last_soundex`, `race_code`,  
`county`, `dwelling`, `family_num`, `birthyr_derived`

### `bureau_patients` — 2,511 rows — Freedmen's Bureau Dispensary
McIntosh County GA, 1865–1868. Medical register records.  
Key columns: `roll`, `full_name`, `last_name`, `first_name`, `last_soundex`, `age`, `condition`

### `members` — 2 rows — Verified ΦΒΑ Members
Key columns: `member_id`, `first_name`, `last_name`, `birth_year`, `birth_state`

### `lineage_ancestors` — 8 rows — Ancestor Chain Records
Key columns: `member_id`, `generation`, `first_name`, `last_name`, `ipums_histid`,  
`ipums_tier`, `verified`, `fs_household_id`

### `pipeline_progress` — tracks per-page OCR status
Key columns: `reel_number`, `page_number`, `state`, `county`, `status`, `records_saved`  
Status values: `success`, `blank`, `quota_error`, `error`  
UNIQUE constraint on `(reel_number, page_number)`

### `verification_submissions` — public submission queue
Key columns: `submission_id`, `member_name`, `member_email`, `anc_first`, `anc_last`,  
`anc_state`, `anc_county`, `ocr_id`, `ipums_histid`, `status`

---

## Web Routes (app.py)

| Route | Method | Description |
|-------|---------|-------------|
| `/` | GET | Search form + homepage stats |
| `/search` | POST | Search results with household clusters |
| `/members` | GET | All verified members + ancestor chains |
| `/lineage/<member_id>` | GET | Visual ancestor timeline |
| `/certificate/<member_id>` | GET | Printable verification certificate |
| `/preview-cert` | GET | Provisional cert from search result |
| `/begin` | GET/POST | Submit verification request |
| `/submission/<id>` | GET | Public submission status tracker |
| `/admin/submissions` | GET | Admin review queue |
| `/admin/submissions/<id>/approve` | POST | Approve → create member + ancestor |
| `/admin/submissions/<id>/reject` | POST | Reject submission |
| `/explore` | GET | County/state coverage explorer |
| `/explore/georgia/<county>` | GET | County surname breakdown |
| `/explore/georgia/<county>/browse` | GET | Paginated record browser |
| `/status` | GET | Pipeline coverage dashboard |
| `/api/search` | GET | JSON search API |
| `/api/members` | GET | JSON members list |
| `/api/states` | GET | JSON state coverage |
| `/api/pipeline-progress` | GET | JSON live pipeline status |

---

## Search Engine (search.py)

**Search flow:**
1. **Soundex match** against all named tables (ocr_records, census_ocr_georgia_1870, census_ocr_1870)
2. **Bureau search** (GA only, McIntosh County)
3. **Edit-distance fallback** (Levenshtein ≤ 0.30 miss ratio) when Soundex returns < 3 results
4. **Deduplication** by household key
5. **IPUMS verification** — assigns Tier 1/2/3 based on census linkage
6. **Confidence score** = name_confidence × 50 + tier_bonus + race_bonus (max 100)

**Soundex function** is registered as a SQLite user function (`soundex_py`) so it runs
inside SQL queries without Python scanning.

**Name abbreviation expansion** maps census shorthand (Wm→William, Jno→John, etc.)
before Soundex hashing.

**Demographic stub** — when no named matches exist, queries IPUMS to show how many
Black people of matching age lived in that state in 1870 (always something to return).

---

## OCR Pipeline Status

**Old pipeline** (`scripts/multi_state_pipeline.py`):
- Sent JP2 page images to Claude API (claude-haiku-4-5) or OpenAI GPT-4o
- Required `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
- Exhausted API credits May 2026 — no longer operational

**Completed by old pipeline:**
- Georgia: 114,728 records (complete, separate table)
- Alabama reels 8, 15, 16: ~72,969 records (county names corrupted)
- Florida reels 121: ~24,185 records (county names corrupted)
- South Carolina reel 1147 (partial): 12,561 records (Hampton + Horry, county names partially corrupted)

**Pending (quota_error, never started):**
- SC 1135 (Beaufort), 1136 (Charleston), 1139 (Colleton)
- LA 453, 458, 462, 468, 471, 479, 483
- MS 638, 640, 644, 646, 658, 660
- NC 928, 929, 934, 940, 944
- NY 860, 861, 870, 871, 872
- FL 122
- VA 1280, 1285, 1270, 1275

**New pipeline:** In-session reading via Claude Code's native image capability.
See `scripts/prep_pages.py` (extract + manifest) and `scripts/insert_batch.py` (validate + insert).

---

## Verification Tiers

| Tier | Meaning | Confidence Bonus |
|------|---------|-----------------|
| 0 | No IPUMS match | +0 |
| 1 | IPUMS match, 1870 only | +15 |
| 2 | Linked across 2 censuses | +35 |
| 3 | Linked across 1870+1880+1900 | +50 |

---

## Known Issues / Technical Debt

1. **County name corruption** — AI county detector in old pipeline hallucinated county names
   from other states. State is correct; county is unreliable for AL/FL/SC in census_ocr_1870.
2. **Admin queue unprotected** — `/admin/submissions` has no auth.
3. **census_ocr_1870 unique constraint absent** — can get duplicate records if pipeline retries.
4. **FTS5 not yet built** — searches rely on Soundex + Levenshtein (Python-level), no FTS index.
5. **SC data incomplete** — only reel 1147 (Hampton/Horry) processed; Beaufort, Charleston,
   Colleton pending.
6. **Badger enslaver unknown** — David Badger (White, Hampton SC, page 53 of reel 1147)
   identified as probable surname source. Full chain not closed without SC death certificate
   for John House Badger.
