# Lineage Check 10X — Session Report
**Date:** June 12, 2026  
**Commit:** 3242e16 and subsequent

---

## What shipped

### Workstream 2 — SC Integration + Badger Chain

- SC data already in `census_ocr_1870` from previous partial run: 12,561 records (Hampton 7,311, Horry 5,250)
- Identified David Badger (White, Farm Laborer, reel 1147, page 53, Hampton SC) as probable surname source
- **`research/badger_chain_report.md`** written with 25/100 confidence score, 5-step path forward
- Verdict: Badger chain NOT marked verified — needs SC death certificate for JHB (highest priority)

### Workstream 3 — Search Engine 10X

**`lineage_check/search.py` — major rewrite:**
- FTS5 virtual table (`names_fts`) with 210,485 records + `fts_rowid_map` for metadata
- 4-layer matching: Soundex → nickname expansion → Metaphone → Levenshtein edit-distance
- 70+ bidirectional nickname table for 19th-century Black American names
  - Wash → Washington, Patsy → Martha/Patricia, Polly → Mary/Molly, Hattie → Harriet, Si → Simon/Silas, Ben → Benjamin, etc.
- Metaphone single-coding implementation (6-char cap) registered as SQLite user function
- Composite confidence scoring (0–100): name score (0-40) + tier bonus (0-40) + race bonus (0-10) + method bonus (0-10)
- Historical abbreviation table: Wm → William, Jno → John, Jas → James, Thos → Thomas, Geo → George, etc.
- 15+ new DB indexes for sub-200ms search performance

### Workstream 4 — Verification & Member Experience

**`lineage_check/app.py`:**
- Admin login (`/admin/login`) with Flask session auth, env-var `ADMIN_PASSWORD`
- `@_require_admin` decorator — open in dev (no password set), protected in prod
- `/verify/<cert_id>` — public certificate verification page (no PII exposed)
- `/api/verify/<cert_id>` — JSON verification API
- `/admin/submissions/<id>/suggest` — auto-suggest search runs on submission arrival
- Certificate QR codes (ΦΒΑ Royal Blue #1a3a8f, 96×96px) linking to `/verify/<cert_id>`
- Stable `cert_id` (MD5 hash of member_id, 16 chars uppercase hex)

**New templates:**
- `admin_login.html` — clean password form, ΦΒΑ blue
- `verify_cert.html` — shows ✓ Valid / ✗ Not Found, no PII

**`certificate.html` updated:**
- Flex layout with QR code on right of seal
- `CERT #` format, `#c9a84c` gold border on QR

**DB schema additions:**
```sql
ALTER TABLE members ADD COLUMN cert_id TEXT;
CREATE TABLE certificates (cert_id, member_id, issued_at, issued_date, revoked, FK→members);
ALTER TABLE verification_submissions ADD COLUMN auto_candidates TEXT;
ALTER TABLE verification_submissions ADD COLUMN reviewed_by TEXT;
ALTER TABLE verification_submissions ADD COLUMN reviewed_at TEXT;
```

### Workstream 5 — Hardening & Ops

**`scripts/backup_db.py`:**
- `PRAGMA integrity_check` before writing
- `sqlite3.backup()` hot backup (safe while app running)
- Timestamped: `lineage_1870_YYYYMMDD_HHMMSS.db`
- Rotation: keeps last N (default 7) by mtime
- First run verified: produced 1,235.8 MB backup

**`tests/test_lineage_check.py` — 44 tests, all passing:**
- TestSoundex (5 tests): Bacon=B250, Badger=B326, Washington=W252, empty, abbreviations
- TestNicknames (6 tests): Wash→Washington, Patsy→Martha, Polly→Mary, Ben→Benjamin, Si→Simon, no self-return
- TestMetaphone (3 tests): Bacon==Bakun, Washington length, empty string
- TestSearch (7 tests): Bacon/Washington returns result, ≥70 confidence, Tier 3, empty, state filter, bureau results
- TestTierAssignment (2 tests): Tier 3 exists (both links), Tier 2 exists (single link)
- TestOCRInsertValidation (5 tests): age sanity, no null state, soundex populated, is_black binary, no exact-duplicate persons
- TestCertificate (3 tests): cert_id exists on all members, certificates table populated, no orphan certs
- TestAPIEndpoints (13 tests): all API routes, QR verify, homepage, search POST, XSS protection

**Security audit:** No SQL injection vectors found. Dynamic ORDER BY in `browse_county` uses whitelist dict with safe default.

**Documentation:**
- `README.md` — setup, record counts, search layers, confidence formula, directory layout
- `DEPLOY.md` — env vars, gunicorn, nginx, cron backup, KY Louisville downloader notes, FTS5 rebuild, admin security checklist
- `/status` page — added SVG coverage map (colored by tier: blue=complete, gold=in-pipeline, grey=pending) + per-state count table

**`ARCHITECTURE.md`** — created at session start: full system map, all 11 tables, all routes.

---

## Record counts (before → after)

| Source | Records | Black |
|--------|---------|-------|
| census_ocr_georgia_1870 | 114,728 | 39,787 |
| census_ocr_1870 (multi-state) | 109,715 | 17,153 |
| bureau_patients | 2,511 | 2,511 |
| IPUMS census 1870 | 1,147,958 | 220,206+ |
| **FTS5 index (new)** | **210,485** | — |
| Members | 2 | — |
| Certificates issued | 2 | — |

**Combined searchable named records: 224,443** (GA + multi-state + bureau)

---

## What remains

### High priority
1. **SC death certificate for John House Badger** — closes the Badger chain from Tier-2 to verified. Request from SC DHEC ($12 fee). See `research/badger_chain_report.md`.
2. **OCR reels 1135, 1136, 1139** — SC Beaufort/Charleston/Colleton, unblocks Sea Islands connections for FBA members
3. **KY Louisville reel ~503** — exponential-backoff downloader (NARA HTTP 503 intermittent failures)

### Medium priority
4. **Mobile-responsive CSS pass** — every public page; especially search results on narrow screens
5. **Lineage timeline enhancements** — generation gap labels, census-year evidence badges, FamilySearch deep links inline
6. **Admin auto-suggest display** — parse and show `auto_candidates` JSON in `admin_submissions.html`
7. **prep_pages.py + insert_batch.py** — JP2 extraction, normalization/deskew/crop, dedup-safe batch insert

### Lower priority
8. **MS/NC/LA reels** — next OCR batch after SC
9. **Workstream 1 OCR loop** — in-session transcription (read JP2s, write structured records)
10. **`/status` auto-refresh** — currently polls `/api/pipeline-progress`; could add WebSocket

---

## Recommended next session priorities

1. Request SC death certificate for John House Badger (manual step)
2. Run OCR on reels 1135/1136/1139 (SC Sea Islands) — unblocks the most members
3. Mobile CSS pass — low-hanging UX improvement before any public launch
4. Build `prep_pages.py` so in-session OCR loop can start on remaining reels

---

*All git commits on `main` branch. Run `python3 -m pytest tests/ -v` to verify green.*
