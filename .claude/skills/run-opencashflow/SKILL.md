---
name: run-opencashflow
description: Build, test, and exercise the opencashflow library (a framework-agnostic SQLAlchemy/Pydantic cashflow-projection engine) and its standalone `opencashflow` CLI. Use when asked to install it, run its tests, verify it works after a change, try its CLI directly, or see a worked example of its write paths (override, record, period close, wallet movement).
---

`opencashflow` is primarily a Python **library** (no server, no GUI), but since v0.8.1 it
also has its own standalone CLI (`opencashflow` console script / `python -m
opencashflow.cli`) -- a minimal, app-independent surface (no ledger, no credit cards, no
multi-user auth) good for trying the library out or running its example specs, but not a
substitute for a real consuming app's own richer CLI (e.g. `opencashflow-cli`'s `ocf`).
For exercising the library's write paths as CODE (the thing a consuming app actually
does), use `.claude/skills/run-opencashflow/smoke.py` -- a runnable worked example (not a
pytest file, no assertions-as-the-point) that seeds a sheet and walks every write path
the library exposes, printing the result of each step.

All paths below are relative to the repo root (`~/Escritorio/opencashflow`).

## Prerequisites

Python >=3.10 (verified against 3.14.7 in this container). No system packages needed --
pure Python, SQLAlchemy + Pydantic only, SQLite backing store.

## Setup

```bash
python3 -m venv .venv   # skip if .venv already exists
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run (agent path)

```bash
source .venv/bin/activate
python .claude/skills/run-opencashflow/smoke.py
```

Expected output (values may shift slightly if the seed data changes):

```
== 1. seed_sheet + compute_sheet ==
  Sheet #1 seeded, 7 sections. SALDO FINAL (Jan 2026) proyectado = 1537000.00
== 2. _do_set_override (write path: manual override) ==
  Wrote override on row 'Sueldo líquido' -> 2000000.00. SALDO FINAL recomputed = 1177000.00
== 3. record set (write path: actual/accrued/paid + CellActualEntry) ==
  Recorded actual=2000000.00 on cell #6
== 4. close_period (dry_run, then committed) ==
  Preview: SALDO FINAL=3200000.00, real_net_flow=2000000.00, 0 rollover(s), warnings=[...]
  Committed: period.is_closed=True, next_period_label=Feb 2026
== 5. wallet + wallet_movements (write path: Wallet/WalletMovement) ==
  wallet_movement_add -> movement #1, wallet balance = 50000.0
  wallet_movement_undo -> reversal #2, wallet balance = 0.0

All write paths exercised successfully.
```

It runs entirely against an in-memory SQLite database created fresh each run --
it never touches a real `opencashflow.db`, so it's always safe to re-run. A
non-zero exit / traceback means the library's public surface (`compute_sheet`,
`_do_set_override`, `close_period`, `do_wallet_movement_add/undo`) has a real
regression, not a flaky test.

## Run the standalone CLI directly

```bash
.venv/bin/opencashflow seed --user-id 1 --months 6
.venv/bin/opencashflow sheets
.venv/bin/opencashflow rows --sheet-id 1
```

Defaults to a throwaway `sqlite:///./opencashflow-demo.db` in the current directory
(override with `--db-url` or `$OPENCASHFLOW_DB_URL`) -- never a real consuming app's own
database. This is the generic surface `register_generic_commands` builds, plus a minimal
`seed`; it has no ledger/credit-card/auth commands (those live in a real consuming app).

## Test

```bash
source .venv/bin/activate
pytest tests/ -v
```

96 tests, all pass (confirmed this session). No known-flaky tests.

## Gotchas

- **`opencashflow.wallet` must be imported before `Base.metadata.create_all()`** on
  a fresh database -- `Wallet`/`WalletMovement` live in their own module and only
  register their tables on `opencashflow.models.Base`'s registry as a side effect of
  being imported. Skipping the import silently creates every table except
  `wallets`/`wallet_movements`. `smoke.py` does `import opencashflow.wallet  # noqa: F401`
  for exactly this reason.
- **Write-path result objects don't use the field names you'd guess**:
  `_do_set_override(...)` returns `OverrideWriteResult` objects whose written value
  is at `.override.value` (not `.new_value`, not `.override.manual_value`) --
  confirmed by inspecting `opencashflow.models.CellOverride`'s actual columns, not
  by guessing from the CLI's print statements.
- **`.venv` is not relocatable.** `pip install -e .` bakes this repo's absolute path
  into the venv's editable-finder files and script shebangs. If this folder is ever
  moved or renamed, re-run `pip install -e .` against the new path (or delete and
  recreate `.venv` entirely if even `pip` itself stops working).
- **`find_balance_row` (used by `close_period`) auto-detects the starting-balance
  row by scanning for the one row using the `previous_period` projection rule** --
  there's no config flag for it. A sheet with zero or more than one such row raises
  `ValueError` (never `sys.exit`), listing every candidate.
