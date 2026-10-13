"""Write/clear/undo a REAL value (actual/accrued/paid) on a cell -- the real
layer's counterpart to opencashflow.overrides, and the primitive behind the
CLI's `record set`/`record clear`/`record undo`/`record history`. Reusable
as-is by any other consumer (a wizard menu action, an HTTP endpoint) that
needs the exact same guarantees: refuse to touch a closed period, append
every write to the cell's CellActualEntry log (never edit or delete one),
and report enough detail (previous/current values, the entries involved)
for a caller to describe what happened without re-deriving it.

Extracted out of cli.py, where `record set`'s write path lived inline inside
the command itself and -- unlike `record undo`/`record clear`, and unlike
every override write -- never called guard_periods_not_closed: a real value
could be silently written onto a period close_period had already frozen.
Same class of gap opencashflow.overrides was extracted to close.
"""
import dataclasses
import enum
from decimal import Decimal
from typing import List, Optional, Tuple, Union

from opencashflow.models import CellActualEntry, SheetCell, SheetPeriod, SheetRow, get_or_create_cell
from opencashflow.record_stack import EmptyRecordStackError, guard_periods_not_closed, pop_record_stack

RealValues = Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]


class _Unset(enum.Enum):
    TOKEN = 0


# "This field was not provided -- leave it untouched." Distinct from an
# explicit None, which is a legitimate value meaning "clear this one field"
# (actual_value/accrued_value/paid_value are all Optional[Decimal]).
UNSET = _Unset.TOKEN

FieldValue = Union[Optional[Decimal], _Unset]


def _period_label(period: SheetPeriod) -> str:
    return period.label or period.period_date.strftime("%b-%y")


def _find_cell(db, row: SheetRow, period: SheetPeriod) -> Optional[SheetCell]:
    return db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()


@dataclasses.dataclass
class RecordSetResult:
    period: SheetPeriod
    previous: RealValues
    current: RealValues
    entry: CellActualEntry


@dataclasses.dataclass
class RecordHistoryEntry:
    entry: CellActualEntry
    is_current: bool


@dataclasses.dataclass
class RecordUndoResult:
    period: SheetPeriod
    reverted_from: CellActualEntry
    reverted_to: Optional[CellActualEntry]  # None means "back to no recorded value"
    new_entry: CellActualEntry


@dataclasses.dataclass
class RecordClearResult:
    period: SheetPeriod
    cleared: bool
    previous: Optional[RealValues] = None


def set_record(
    db, row: SheetRow, period: SheetPeriod, *,
    actual_value: FieldValue = UNSET, accrued_value: FieldValue = UNSET, paid_value: FieldValue = UNSET,
    note: Optional[str] = None, created_by: int,
) -> RecordSetResult:
    """Write whichever of actual/accrued/paid were provided (UNSET = leave
    untouched, None = clear that one field) onto `row`'s cell for `period`,
    and append a full-snapshot CellActualEntry. Commits.

    Raises ValueError if all three are UNSET (nothing to write), or if
    `period` is closed -- checked before anything is written.
    """
    if actual_value is UNSET and accrued_value is UNSET and paid_value is UNSET:
        raise ValueError("Debes dar al menos uno de actual_value, accrued_value, paid_value.")
    guard_periods_not_closed([period], "registrar un record")

    cell = get_or_create_cell(db, row.id, period.id)
    previous: RealValues = (cell.actual_value, cell.accrued_value, cell.paid_value)
    if actual_value is not UNSET:
        cell.actual_value = actual_value
    if accrued_value is not UNSET:
        cell.accrued_value = accrued_value
    if paid_value is not UNSET:
        cell.paid_value = paid_value

    entry = CellActualEntry(
        cell_id=cell.id, actual_value=cell.actual_value, accrued_value=cell.accrued_value,
        paid_value=cell.paid_value, note=note, created_by=created_by,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return RecordSetResult(
        period=period, previous=previous,
        current=(cell.actual_value, cell.accrued_value, cell.paid_value), entry=entry,
    )


def record_history(db, row: SheetRow, period: SheetPeriod) -> List[RecordHistoryEntry]:
    """Every CellActualEntry for `row`'s cell in `period`, oldest first,
    with the one matching the cell's live values flagged is_current (the
    last match wins -- the log can repeat values)."""
    cell = _find_cell(db, row, period)
    if cell is None:
        return []
    live = (cell.actual_value, cell.accrued_value, cell.paid_value)
    views = []
    current_index = None
    for i, entry in enumerate(cell.actual_entries):
        if (entry.actual_value, entry.accrued_value, entry.paid_value) == live:
            current_index = i
        views.append(RecordHistoryEntry(entry=entry, is_current=False))
    if current_index is not None:
        views[current_index].is_current = True
    return views


def undo_record(
    db, row: SheetRow, period: SheetPeriod, *, note: Optional[str] = None, created_by: int,
) -> RecordUndoResult:
    """Pop one step off `row`'s record stack for `period`
    (record_stack.pop_record_stack). Commits.

    Raises ValueError if `period` is closed, or EmptyRecordStackError (a
    ValueError subclass) if there's nothing left to undo.
    """
    guard_periods_not_closed([period], "deshacer un record")

    cell = _find_cell(db, row, period)
    if cell is None or not cell.actual_entries:
        raise EmptyRecordStackError(
            f"'{row.name}' en {_period_label(period)} no tiene ningún record registrado -- no hay nada que deshacer."
        )

    try:
        reverted_from, reverted_to, new_entry = pop_record_stack(db, cell, note=note, created_by=created_by)
    except EmptyRecordStackError:
        raise EmptyRecordStackError(
            f"'{row.name}' en {_period_label(period)}: ya se deshicieron todos los cambios registrados -- "
            f"no queda nada más que deshacer."
        ) from None

    db.commit()
    db.refresh(new_entry)
    return RecordUndoResult(period=period, reverted_from=reverted_from, reverted_to=reverted_to, new_entry=new_entry)


def clear_record(
    db, row: SheetRow, periods: List[SheetPeriod], *, note: Optional[str] = None, created_by: int,
) -> List[RecordClearResult]:
    """Reset actual/accrued/paid to None on `row`'s cell for every period in
    `periods`, appending an all-None CellActualEntry for each one that had
    anything to clear (a period with nothing recorded is a reported no-op,
    not an error). Commits.

    Raises ValueError if any period in `periods` is closed -- checked before
    anything is cleared.
    """
    guard_periods_not_closed(periods, "limpiar un record")

    results: List[RecordClearResult] = []
    for period in periods:
        cell = _find_cell(db, row, period)
        if cell is None or (cell.actual_value is None and cell.accrued_value is None and cell.paid_value is None):
            results.append(RecordClearResult(period=period, cleared=False))
            continue

        previous: RealValues = (cell.actual_value, cell.accrued_value, cell.paid_value)
        cell.actual_value = None
        cell.accrued_value = None
        cell.paid_value = None
        write_note = note or "[record clear] limpieza manual del valor real -- vuelve a sin valor registrado"
        db.add(CellActualEntry(
            cell_id=cell.id, actual_value=None, accrued_value=None, paid_value=None,
            note=write_note, created_by=created_by,
        ))
        results.append(RecordClearResult(period=period, cleared=True, previous=previous))

    db.commit()
    return results
