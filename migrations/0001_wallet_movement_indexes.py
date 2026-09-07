#!/usr/bin/env python3
"""Migration 0001: add indexes to wallet_movements, including the unique
index on reverses_movement_id that closes a real TOCTOU race in
do_wallet_movement_undo (see MIGRATIONS.md).

Usage:
    DATABASE_URL="sqlite:////path/to/opencashflow.db" python migrations/0001_wallet_movement_indexes.py

Requires DATABASE_URL to be set explicitly -- never guesses a default path.
Idempotent: every statement uses IF NOT EXISTS, safe to re-run.
"""
import os
import sys

from sqlalchemy import create_engine, text

STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS ix_wallet_movements_wallet_id "
    "ON wallet_movements (wallet_id)",
    "CREATE INDEX IF NOT EXISTS ix_wallet_movements_row_period "
    "ON wallet_movements (row_id, period_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_movements_reverses_movement_id "
    "ON wallet_movements (reverses_movement_id)",
]


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set -- refusing to guess a database to migrate.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(database_url)
    print(f"Migrating {database_url} ...")
    with engine.begin() as conn:
        for stmt in STATEMENTS:
            print(f"  {stmt}")
            conn.execute(text(stmt))
    print("Done.")


if __name__ == "__main__":
    main()
