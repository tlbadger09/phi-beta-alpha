#!/usr/bin/env python3
"""
Nightly-style SQLite backup with rotation.
Usage: python3 scripts/backup_db.py [--keep N]
Keeps last N backups (default 7). Runs integrity check before writing.
"""

import sys
import sqlite3
import datetime
import argparse
import shutil
from pathlib import Path

DB_PATH    = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"
BACKUP_DIR = Path.home() / "Documents/phi-beta-alpha/processed/backups"


def run_backup(keep: int = 7) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Integrity check first
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    if result != "ok":
        print(f"INTEGRITY CHECK FAILED: {result}", file=sys.stderr)
        sys.exit(1)

    # Write backup
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"lineage_1870_{ts}.db"
    src_conn  = sqlite3.connect(str(DB_PATH), timeout=30)
    dest_conn = sqlite3.connect(str(dest), timeout=60)
    src_conn.backup(dest_conn)
    src_conn.close()
    dest_conn.close()

    size_mb = dest.stat().st_size / 1_048_576
    print(f"Backup written: {dest.name} ({size_mb:.1f} MB)")

    # Rotate: keep only the N most recent
    backups = sorted(BACKUP_DIR.glob("lineage_1870_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink()
        print(f"Rotated out: {old.name}")

    print(f"Backup complete. {min(len(backups), keep)} backups retained.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=7, help="Number of backups to retain")
    args = parser.parse_args()
    run_backup(args.keep)


if __name__ == "__main__":
    main()
