# Migrations

This project has no migration framework (no Alembic). `Base.metadata.create_all()`
(called on every `opencashflow init`/first run) only creates tables that don't exist
yet — it never alters a table that's already there. A **new** database always gets
the current schema for free, straight from the models. An **existing** database
(in particular, a real one with real data — never edit it directly without a backup
and an explicit `DATABASE_URL`) needs the DDL below applied by hand whenever a
schema-changing version is installed.

This log is the convention: one entry per schema-changing version, in order, each
with the exact DDL and a copy-pasteable SQLite snippet. A runnable script lives in
`migrations/` for any entry non-trivial enough to warrant one.

---

## v0.12.0

Three schema changes landed together in one batch, on purpose — see the project's
technical-debt remediation plan. Apply all three in one sitting against a **copy**
of the real database first, verify the app still works end-to-end against the copy,
then apply to the real one.

Applied to the maintainer's own real database on 2026-09-07, after an
`opencashflow.db.backup-2026-09-07-pre-techdebt-migration` backup and a dry run
against a throwaway copy. Verified via a full read pass through the ORM afterward.

### 1. `credit_cards`: `credit_limit` / `current_balance` / `interest_rate` /
   `minimum_payment_rate` — `Float` → `Numeric(14, 2)`

**No DDL needed on SQLite.** SQLite's column typing is a hint (affinity), not an
enforced type — verified empirically: a table created with these columns declared
`FLOAT` still reads back correctly as `Decimal` the moment the model declares them
`Numeric(14, 2)`, with zero `ALTER TABLE`. Just deploy the new `opencashflow`
version; there is nothing to run against the database.

(If this project is ever pointed at a stricter backend — Postgres, MySQL — that
DOES enforce column types, this one will need a real
`ALTER TABLE credit_cards ALTER COLUMN credit_limit TYPE NUMERIC(14,2)`-style
migration then, and this entry should be updated to say so before anyone assumes
the SQLite shortcut still applies.)

### 2. `wallet_movements`: new indexes + a unique constraint

```sql
CREATE INDEX IF NOT EXISTS ix_wallet_movements_wallet_id
    ON wallet_movements (wallet_id);
CREATE INDEX IF NOT EXISTS ix_wallet_movements_row_period
    ON wallet_movements (row_id, period_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_movements_reverses_movement_id
    ON wallet_movements (reverses_movement_id);
```

The unique index is the one that matters functionally: it closes a real TOCTOU
race in `do_wallet_movement_undo` (two concurrent undo calls on the same movement
could previously both pass its plain-SELECT "already reversed?" check before
either committed, both appending a reversal — a real double-reversal of money).
SQLite (like Postgres) treats multiple `NULL`s in a unique index as distinct, so
this does not block the common case (most movements have `reverses_movement_id =
NULL`). Run via `migrations/0001_wallet_movement_indexes.py`.

### 3. `cell_actual_entries`: new `entry_kind` column, with a data backfill

```sql
ALTER TABLE cell_actual_entries ADD COLUMN entry_kind VARCHAR(10) NOT NULL DEFAULT 'record';
UPDATE cell_actual_entries SET entry_kind = 'undo' WHERE note LIKE '[record undo]%';
```

Before this, `record_stack.replay_record_stack` told a real recorded value apart
from the compensating entry a previous undo wrote ONLY by sniffing whether `note`
happened to start with `"[record undo] "` — with nothing anywhere reserving that
prefix against a caller ever writing a genuine note that happened to start the
same way. Any historical row already matching that pattern must be backfilled to
`entry_kind = 'undo'` or it will be replayed as a fresh recorded value instead of
the undo it actually was, corrupting which entry is treated as "current" for
every cell it touches. Run via `migrations/0002_cell_actual_entry_entry_kind.py`
(it applies the `ALTER TABLE` and the backfill `UPDATE` together, and prints how
many rows it reclassified).

---

## How to run a migration script

```bash
# Never against the real DB first. Copy it, migrate the copy, verify the app
# still works end-to-end against the copy, THEN run it for real.
cp ~/.local/share/opencashflow/opencashflow.db /tmp/opencashflow-migration-test.db
DATABASE_URL="sqlite:////tmp/opencashflow-migration-test.db" python migrations/0001_wallet_movement_indexes.py
DATABASE_URL="sqlite:////tmp/opencashflow-migration-test.db" python migrations/0002_cell_actual_entry_entry_kind.py

# Only once the copy checks out:
DATABASE_URL="sqlite:///$HOME/.local/share/opencashflow/opencashflow.db" python migrations/0001_wallet_movement_indexes.py
DATABASE_URL="sqlite:///$HOME/.local/share/opencashflow/opencashflow.db" python migrations/0002_cell_actual_entry_entry_kind.py
```

Every script in `migrations/` requires `DATABASE_URL` to be set explicitly (it
never guesses a default path) and is idempotent (safe to re-run: `IF NOT EXISTS`
on the indexes, and it checks whether `entry_kind` already exists before trying
to add it again).
