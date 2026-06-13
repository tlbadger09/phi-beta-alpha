# Deployment Guide

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_PASSWORD` | Yes (prod) | Admin login password. If unset, admin is open (dev only). |
| `FLASK_SECRET_KEY` | Yes (prod) | Session signing key. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `ANTHROPIC_API_KEY` | No | Only needed for OCR pipeline (scripts/census_ocr.py). Not used by the web app. |
| `OPENAI_API_KEY` | No | Optional — only if clawdbot or embedding tools are active. |

Set these in `lineage_check/.env`:

```bash
ADMIN_PASSWORD=your-strong-password-here
FLASK_SECRET_KEY=64-char-hex-string-here
```

The app loads `.env` via `python-dotenv` if present. In production, prefer setting
env vars at the process/service level rather than in a file.

## Production run (gunicorn)

```bash
pip3 install gunicorn
cd lineage_check
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

Use a reverse proxy (nginx, Caddy) in front for TLS. Example nginx snippet:

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Database location

The app expects the database at:

```
~/Documents/phi-beta-alpha/processed/lineage_1870.db
```

To move it, update `DB_PATH` in both `lineage_check/search.py` and `scripts/member_lookup.py`.

## Nightly backup (cron)

```bash
# Add to crontab: crontab -e
0 2 * * * /usr/bin/python3 /Users/tracybadger/Documents/phi-beta-alpha/scripts/backup_db.py --keep 7
```

Backups are written to `processed/backups/` with timestamps. The script:
1. Runs `PRAGMA integrity_check` and aborts on failure
2. Uses `sqlite3.backup()` for a consistent hot backup (safe while app is running)
3. Rotates to keep the last N backups

## Restoring a backup

```bash
# Stop the app first, then:
cp processed/backups/lineage_1870_YYYYMMDD_HHMMSS.db processed/lineage_1870.db
```

## Adding new OCR reels

1. Download JP2 ZIP from NARA catalog into `nara_microfilm/<state>/M<reel>.zip`
2. Run prep_pages.py (extracts JP2s, normalizes, writes manifest)
3. Run in-session OCR transcription loop (reads images, writes to census_ocr_1870)
4. Run insert_batch.py (validates and inserts with dedup on page+line)

SC reels not yet processed: **1135, 1136, 1139**
KY Louisville reels (reel ~503): use exponential-backoff downloader — NARA
occasionally returns 503 on high-traffic reels. Retry with 2s→4s→8s→16s delays.

## FTS5 index

The `names_fts` FTS5 table is populated from all three OCR tables + IPUMS.
If the DB is restored from backup, the FTS table is included automatically
(it's stored in the same SQLite file). To rebuild from scratch:

```bash
python3 - <<'EOF'
import sqlite3, sys
sys.path.insert(0, 'lineage_check')
conn = sqlite3.connect('processed/lineage_1870.db')
conn.execute("DELETE FROM names_fts")
conn.execute("DELETE FROM fts_rowid_map")
# Re-run the FTS population logic from the build session
conn.commit()
EOF
```

## Admin panel

URL: `/admin/submissions`  
Login: `/admin/login` (password = `ADMIN_PASSWORD` env var)

If `ADMIN_PASSWORD` is not set, the admin panel is open (for local dev only).
**Always set ADMIN_PASSWORD in production.**

## Certificate verification

Public URL: `/verify/<cert_id>`  
JSON API: `/api/verify/<cert_id>`  
QR codes on certificates link to the public verification page.
No PII is exposed — only cert validity, issue date, state of origin, and
number of verified generations.

## Security checklist

- [ ] `ADMIN_PASSWORD` set in env
- [ ] `FLASK_SECRET_KEY` set to a random 32+ byte hex string
- [ ] HTTPS configured (reverse proxy with TLS)
- [ ] `ANTHROPIC_API_KEY` NOT in `.env` (web app doesn't need it)
- [ ] `processed/backups/` not web-accessible
- [ ] `nara_microfilm/` not web-accessible
