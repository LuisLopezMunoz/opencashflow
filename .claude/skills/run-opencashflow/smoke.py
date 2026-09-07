"""Smoke test / worked example for the `opencashflow` library.

This is not a test in the pytest sense (no assertions) -- it is a runnable
demonstration that imports the package as an external consumer would and
exercises every write path the library exposes, printing the result of each
step. Run it after `pip install -e ".[dev]"` to confirm the library actually
works end to end, not just that its own unit tests pass in isolation.

    python .claude/skills/run-opencashflow/smoke.py

Everything runs against an in-memory SQLite database created fresh each run
-- it never touches a real `opencashflow.db`.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import opencashflow.wallet  # noqa: F401 -- registers Wallet/WalletMovement on Base.metadata
from opencashflow.cli import _do_set_override
from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CellActualEntry, SheetCell, SheetPeriod
from opencashflow.period_close import close_period
from opencashflow.wallet import Wallet
from opencashflow.wallet_movements import do_wallet_movement_add, do_wallet_movement_undo
from opencashflow.seed import seed_sheet


def cell_for(result, row_id, period_id):
    for section in result["sections"]:
        for row_data in section["rows"]:
            if row_data["row"].id == row_id:
                for cr in row_data["cells"]:
                    if cr.period_id == period_id:
                        return cr
    return None


def main():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    print("== 1. seed_sheet + compute_sheet ==")
    sheet = seed_sheet(db, user_id=1, months=3, base_period=date(2026, 1, 1))
    result = compute_sheet(sheet.id, db)
    first_period = db.query(SheetPeriod).filter_by(sheet_id=sheet.id).order_by(SheetPeriod.sort_order).first()
    saldo_final_row = next(
        r for s in result["sections"] for r in s["rows"] if r["row"].name == "SALDO FINAL"
    )["row"]
    saldo_before = cell_for(result, saldo_final_row.id, first_period.id).projected_value
    print(f"  Sheet #{sheet.id} seeded, {len(result['sections'])} sections. "
          f"SALDO FINAL ({first_period.label}) proyectado = {saldo_before}")
    assert saldo_before is not None

    print("== 2. _do_set_override (write path: manual override) ==")
    ingresos_row = next(
        r for s in result["sections"] for r in s["rows"]
        if r["row"].name not in ("SALDO INICIAL", "SALDO FINAL") and r["row"].section.section_type == "income"
    )["row"]
    write_results = _do_set_override(
        db, sheet.id, ingresos_row, [first_period],
        value=Decimal("2000000"), created_by=1, note="smoke test override",
    )
    result = compute_sheet(sheet.id, db)
    saldo_after_override = cell_for(result, saldo_final_row.id, first_period.id).projected_value
    print(f"  Wrote override on row '{ingresos_row.name}' -> {write_results[0].override.value}. "
          f"SALDO FINAL recomputed = {saldo_after_override}")
    assert saldo_after_override != saldo_before

    print("== 3. record set (write path: actual/accrued/paid + CellActualEntry) ==")
    cell = db.query(SheetCell).filter_by(row_id=ingresos_row.id, period_id=first_period.id).first()
    if not cell:
        cell = SheetCell(row_id=ingresos_row.id, period_id=first_period.id)
        db.add(cell)
        db.flush()
    cell.actual_value = cell.accrued_value = cell.paid_value = Decimal("2000000")
    db.add(CellActualEntry(cell_id=cell.id, actual_value=cell.actual_value,
                            accrued_value=cell.accrued_value, paid_value=cell.paid_value,
                            note="smoke test record", created_by=1))
    db.commit()
    print(f"  Recorded actual={cell.actual_value} on cell #{cell.id}")

    print("== 4. close_period (dry_run, then committed) ==")
    preview = close_period(db, sheet, first_period, acting_user_id=1, dry_run=True)
    print(f"  Preview: SALDO FINAL={preview.saldo_final_value}, real_net_flow={preview.real_net_flow}, "
          f"{len(preview.rollovers)} rollover(s), warnings={preview.warnings}")
    report = close_period(db, sheet, first_period, acting_user_id=1, dry_run=False)
    db.refresh(first_period)
    print(f"  Committed: period.is_closed={first_period.is_closed}, "
          f"next_period_label={report.next_period_label}")
    assert first_period.is_closed

    print("== 5. wallet + wallet_movements (write path: Wallet/WalletMovement) ==")
    wallet_obj = Wallet(user_id=1, name="Efectivo", wallet_type="cash", currency="CLP", balance=Decimal("0"))
    db.add(wallet_obj)
    db.commit()
    second_period = db.query(SheetPeriod).filter_by(sheet_id=sheet.id).order_by(SheetPeriod.sort_order).offset(1).first()
    add_result = do_wallet_movement_add(
        db, wallet_obj, sheet.id, ingresos_row, second_period,
        amount=Decimal("50000"), note="smoke test deposit", created_by=1,
    )
    db.refresh(wallet_obj)
    print(f"  wallet_movement_add -> movement #{add_result.movement.id}, wallet balance = {wallet_obj.balance}")
    undo_result = do_wallet_movement_undo(db, add_result.movement, note="smoke test undo", created_by=1)
    db.refresh(wallet_obj)
    print(f"  wallet_movement_undo -> reversal #{undo_result.reversal.id}, wallet balance = {wallet_obj.balance}")
    assert wallet_obj.balance == Decimal("0")

    print("\nAll write paths exercised successfully.")


if __name__ == "__main__":
    main()
