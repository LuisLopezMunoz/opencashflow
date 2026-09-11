#!/usr/bin/env python3
"""Migration 0003: add a unique index on sheet_periods(sheet_id, sort_order)
(see MIGRATIONS.md) -- close_period/reopen_period's ordering guards compare
sort_order with strict </> to decide "earlier"/"later"; nothing at the DB
level previously stopped two periods on the same sheet from sharing one.

Usage:
    DATABASE_URL="sqlite:////path/to/opencashflow.db" python migrations/0003_period_sort_order_unique.py

Requires DATABASE_URL to be set explicitly -- never guesses a default path.
Idempotent: the index uses IF NOT EXISTS, safe to re-run. Checks for any
existing (sheet_id, sort_order) duplicate FIRST and refuses to proceed if it
finds one -- CREATE UNIQUE INDEX would otherwise fail with a generic SQLite
error that doesn't say which rows are the problem.
"""
import os
import sys

from sqlalchemy import create_engine, text

CHECK_DUPLICATES = """
SELECT sheet_id, sort_order, COUNT(*) AS n
FROM sheet_periods
GROUP BY sheet_id, sort_order
HAVING COUNT(*) > 1
"""

CREATE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_period_sort_order "
    "ON sheet_periods (sheet_id, sort_order)"
)


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set -- refusing to guess a database to migrate.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(database_url)
    print(f"Migrating {database_url} ...")

    with engine.begin() as conn:
        duplicates = conn.execute(text(CHECK_DUPLICATES)).fetchall()
        if duplicates:
            print("Found existing (sheet_id, sort_order) duplicates -- refusing to add the "
                  "unique index until these are resolved by hand:", file=sys.stderr)
            for sheet_id, sort_order, count in duplicates:
                print(f"  sheet_id={sheet_id} sort_order={sort_order}: {count} periods", file=sys.stderr)
            sys.exit(1)

        print(f"  {CREATE_INDEX}")
        conn.execute(text(CREATE_INDEX))
    print("Done.")


if __name__ == "__main__":
    main()
