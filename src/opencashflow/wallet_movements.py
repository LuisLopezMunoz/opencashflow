"""A single real-world cash event that moves a Wallet's balance AND records
the corresponding sheet row as real, in one atomic write.

Direction is never asked for explicitly. A movement takes a POSITIVE
`amount` (how much of the row's own value was paid or received) and derives
which way the wallet moves from the row's own effective sign to the bottom
line (opencashflow.engine.effective_sign_to_top): an income-direction row
makes the wallet grow, an expense-direction row makes it shrink -- by
construction of what "recording a paycheck received" or "a bill paid" even
means. A row with no aggregation path (effective_sign is None) is rejected
outright -- there is no way to know which way the wallet should move.

The sheet-cell side is ADDITIVE on paid_value, not an absolute set -- a
movement is one real event among possibly several on the same cell this
period (two paychecks, a bill split across two wallets), not a total-setter.

Undoing a movement reverses BOTH sides at once: the wallet balance goes back
(a compensating WalletMovement row, `reverses_movement_id` pointing at the
original) and the cell pops one step back on its own record stack
(opencashflow.record_stack.pop_record_stack). Undo is refused if the cell
already has something more recent on top of this movement's own write (a
later record, or a second movement) -- popping the wrong thing would
silently corrupt an unrelated later fact instead of reversing this specific
event.
"""
import dataclasses
from decimal import Decimal
from typing import Optional

from opencashflow.engine import build_sum_rows_hierarchy, effective_sign_to_top
from opencashflow.models import CellActualEntry, SheetCell, SheetPeriod, SheetRow
from opencashflow.record_stack import guard_periods_not_closed, pop_record_stack, replay_record_stack
from opencashflow.wallet import Wallet, WalletMovement


@dataclasses.dataclass
class WalletMovementResult:
    movement: WalletMovement
    wallet_balance_before: float
    wallet_balance_after: float
    cell_paid_before: Optional[Decimal]
    cell_paid_after: Decimal


def do_wallet_movement_add(
    db, wallet: Wallet, sheet_id: int, row: SheetRow, period: SheetPeriod, amount: Decimal,
    *, note: Optional[str], created_by: Optional[int],
) -> WalletMovementResult:
    if amount <= 0:
        raise ValueError(
            "amount must be a positive number -- the row decides the sign (an income row adds "
            "to the wallet, an expense row subtracts), it is never asked for separately."
        )
    guard_periods_not_closed([period], "record a wallet movement")

    rows_by_id, child_to_parent = build_sum_rows_hierarchy(db, sheet_id)
    sign = effective_sign_to_top(row.id, rows_by_id, child_to_parent)
    if sign is None:
        raise ValueError(
            f"'{row.name}' has no sum_rows rule aggregating it toward the total -- cannot tell "
            f"whether a movement in it adds to or subtracts from the wallet."
        )

    cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
    if cell is None:
        cell = SheetCell(row_id=row.id, period_id=period.id)
        db.add(cell)
        db.flush()

    paid_before = cell.paid_value
    paid_after = (paid_before if paid_before is not None else Decimal(0)) + amount
    cell.paid_value = paid_after

    entry = CellActualEntry(
        cell_id=cell.id, actual_value=cell.actual_value, accrued_value=cell.accrued_value,
        paid_value=paid_after, note=note, created_by=created_by,
    )
    db.add(entry)
    db.flush()

    wallet_delta = float(sign * amount)
    balance_before = wallet.balance
    wallet.balance = balance_before + wallet_delta

    movement = WalletMovement(
        wallet_id=wallet.id, amount=wallet_delta, sheet_id=sheet_id, row_id=row.id, period_id=period.id,
        actual_entry_id=entry.id, note=note, created_by=created_by,
    )
    db.add(movement)
    db.commit()
    db.refresh(movement)
    db.refresh(wallet)

    return WalletMovementResult(
        movement=movement, wallet_balance_before=balance_before, wallet_balance_after=wallet.balance,
        cell_paid_before=paid_before, cell_paid_after=paid_after,
    )


@dataclasses.dataclass
class WalletMovementUndoResult:
    original: WalletMovement
    reversal: WalletMovement
    wallet_balance_before: float
    wallet_balance_after: float


def do_wallet_movement_undo(
    db, movement: WalletMovement, *, note: Optional[str], created_by: Optional[int],
) -> WalletMovementUndoResult:
    if movement.reverses_movement_id is not None:
        raise ValueError(f"Movement #{movement.id} is itself a reversal -- a reversal cannot be undone.")
    already = db.query(WalletMovement).filter(WalletMovement.reverses_movement_id == movement.id).first()
    if already is not None:
        raise ValueError(f"Movement #{movement.id} was already reversed by movement #{already.id}.")

    period = db.query(SheetPeriod).filter(SheetPeriod.id == movement.period_id).first()
    if period is not None:
        guard_periods_not_closed([period], "undo a wallet movement")

    cell = db.query(SheetCell).filter(
        SheetCell.row_id == movement.row_id, SheetCell.period_id == movement.period_id,
    ).first()
    if cell is None:
        raise ValueError(f"The cell associated with movement #{movement.id} no longer exists.")

    stack = replay_record_stack(list(cell.actual_entries))
    if not stack or stack[-1].id != movement.actual_entry_id:
        raise ValueError(
            f"Cannot undo movement #{movement.id}: the cell has something more recent on top "
            f"(another movement, or a plain record write) -- undo those first, in order."
        )

    _, _, new_cell_entry = pop_record_stack(db, cell, note=note, created_by=created_by)

    wallet = db.query(Wallet).filter(Wallet.id == movement.wallet_id).first()
    balance_before = wallet.balance
    wallet.balance = balance_before - movement.amount

    reversal = WalletMovement(
        wallet_id=wallet.id, amount=-movement.amount, sheet_id=movement.sheet_id, row_id=movement.row_id,
        period_id=movement.period_id, actual_entry_id=new_cell_entry.id, reverses_movement_id=movement.id,
        note=note, created_by=created_by,
    )
    db.add(reversal)
    db.commit()
    db.refresh(reversal)
    db.refresh(wallet)

    return WalletMovementUndoResult(
        original=movement, reversal=reversal, wallet_balance_before=balance_before, wallet_balance_after=wallet.balance,
    )
