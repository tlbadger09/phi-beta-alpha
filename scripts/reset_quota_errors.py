#!/usr/bin/env python3
"""
reset_quota_errors.py — Clear quota_error entries so the pipeline retries them.

Run after adding API credits to either Anthropic or OpenAI accounts.

Usage:
  python3 scripts/reset_quota_errors.py                    # reset all
  python3 scripts/reset_quota_errors.py --reel 1135        # one reel
  python3 scripts/reset_quota_errors.py --state "South Carolina"
  python3 scripts/reset_quota_errors.py --dry-run          # show what would be cleared
"""

import sqlite3
import argparse
from pathlib import Path

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reel",    type=int,  help="Specific reel number")
    parser.add_argument("--state",              help="Specific state")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=10)

    # Preview
    sql_count = "SELECT reel_number, state, COUNT(*) FROM pipeline_progress WHERE status='quota_error'"
    params = []
    if args.reel:
        sql_count += " AND reel_number=?"
        params.append(args.reel)
    if args.state:
        sql_count += " AND state=?"
        params.append(args.state)
    sql_count += " GROUP BY reel_number, state ORDER BY reel_number"

    rows = conn.execute(sql_count, params).fetchall()
    if not rows:
        print("No quota_error entries found matching those filters.")
        conn.close()
        return

    print(f"{'Reel':>6}  {'State':<20} {'Pages':>6}")
    print("-" * 38)
    total = 0
    for reel, state, cnt in rows:
        print(f"{reel:>6}  {state:<20} {cnt:>6}")
        total += cnt
    print("-" * 38)
    print(f"{'Total':>6}  {'':20} {total:>6}")

    if args.dry_run:
        print("\n[dry-run: no changes made]")
        conn.close()
        return

    sql_del = "DELETE FROM pipeline_progress WHERE status='quota_error'"
    del_params = []
    if args.reel:
        sql_del += " AND reel_number=?"
        del_params.append(args.reel)
    if args.state:
        sql_del += " AND state=?"
        del_params.append(args.state)

    deleted = conn.execute(sql_del, del_params).rowcount
    conn.commit()
    conn.close()
    print(f"\nCleared {deleted} quota_error entries. Re-run the pipeline to process them.")


if __name__ == "__main__":
    main()
