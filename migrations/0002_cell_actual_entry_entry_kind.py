#!/usr/bin/env python3
"""Migration 0002: add cell_actual_entries.entry_kind and backfill it from
the note text it replaces (see MIGRATIONS.md and record_stack.py).

record_stack.replay_record_stack used to tell a real recorded value apart
from the compensating entry a previous undo wrote ONLY by sniffing whether
`note` happened to start with "[record undo] ", with nothing anywhere
reserving that prefix against a genuine caller-supplied note that happened
to start the same way. Any historical row already matching that pattern
must be reclassified to entry_kind='undo' or it replays as a fresh recorded
value instead of the undo it actually was.

Usage:
    DATABASE_URL="sqlite:////path/to/opencashflow.db" python migrations/0002_cell_actual_entry_entry_kind.py

Requires DATABASE_URL to be set explicitly -- never guesses a default path.
Idempotent: checks whether the column already exists before adding it, and
the backfill UPDATE is safe to re-run (it only ever sets entry_kind='undo'
for rows matching the note pattern -- running it again is a no-op).
"""
import os
import sys

from sqlalchemy import create_engine, inspect, text

TABLE = "cell_actual_entries"
UNDO_NOTE_PREFIX = "[record undo]"


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set -- refusing to guess a database to migrate.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(database_url)
    print(f"Migrating {database_url} ...")

    existing_columns = {c["name"] for c in inspect(engine).get_columns(TABLE)}
    with engine.begin() as conn:
        if "entry_kind" in existing_columns:
            print(f"  {TABLE}.entry_kind already exists, skipping ADD COLUMN.")
        else:
            stmt = (
                f"ALTER TABLE {TABLE} ADD COLUMN entry_kind VARCHAR(10) "
                f"NOT NULL DEFAULT 'record'"
            )
            print(f"  {stmt}")
            conn.execute(text(stmt))

        backfill = text(
            f"UPDATE {TABLE} SET entry_kind = 'undo' "
            f"WHERE note LIKE :pattern AND entry_kind != 'undo'"
        )
        result = conn.execute(backfill, {"pattern": f"{UNDO_NOTE_PREFIX}%"})
        print(f"  Backfilled entry_kind='undo' for {result.rowcount} historical row(s).")
    print("Done.")


if __name__ == "__main__":
    main()
