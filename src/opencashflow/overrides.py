"""Write/clear a manual override on a cell -- the primitive behind the CLI's
`override set`/`override clear` and reusable as-is by any other consumer
(a wizard menu action, an HTTP endpoint) that needs the exact same
guarantees: refuse to touch a closed period, always supersede any existing
active override before writing a new one, and report enough detail (the
previous override, the value the cell reverts to) for a caller to describe
what happened without re-deriving it.

Extracted out of cli.py (where it used to live as `_do_set_override`/
`_do_clear_override`, underscore-prefixed and duplicated independently by
at least one other consumer that then missed the closed-period guard added
here -- see PENDIENTES.md) so this logic exists in exactly one place in the
core, not one per consumer.
"""
import dataclasses
from decimal import Decimal
from typing import Dict, List, Optional

from opencashflow.engine import CellResult, compute_sheet
from opencashflow.models import (
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    get_or_create_cell,
    supersede_active_override,
    supersede_and_write_override,
)
from opencashflow.record_stack import guard_periods_not_closed
from opencashflow.structure import validate_rule


@dataclasses.dataclass
class OverrideWriteResult:
    period: SheetPeriod
    override: Optional[CellOverride]  # None only when a --lock write was skipped
    previous_override: Optional[CellOverride] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclasses.dataclass
class OverrideClearResult:
    period: SheetPeriod
    previous_override: Optional[CellOverride]  # None means there was nothing active to clear
    reverted_value: Optional[Decimal] = None
    reverted_source: Optional[str] = None


def set_override(
    db, sheet_id: int, row: SheetRow, periods: List[SheetPeriod], *,
    value: Optional[Decimal] = None, rule: Optional[dict] = None, lock: bool = False,
    note: Optional[str] = None, created_by: int,
) -> List[OverrideWriteResult]:
    """Write the SAME override onto every period in `periods` for `row`.
    Exactly one of value/rule/lock is expected to be meaningful -- validated
    by the caller, since a caller that already knows which one it wants
    shouldn't have to fake the other two.

    --lock: computes the sheet ONCE up front (nothing this function does
    changes projected values before the lock writes, so one shared snapshot
    for the whole call is correct) and captures each targeted cell's current
    effective projected_value as the override's value -- NEVER None. A
    period whose captured value is None (nothing to lock yet) is skipped
    (skip_reason set, no override written for it), not silently written with
    value=None.

    Refuses (ValueError, via guard_periods_not_closed) to touch any period
    in `periods` that is_closed, or (via structure.validate_rule) to store
    a `rule` that references rows outside the sheet or would crash the
    engine -- both before writing anything. Commits once at the end. Every
    write supersedes any existing active override on that cell first (see
    models.supersede_and_write_override).
    """
    guard_periods_not_closed(periods, "escribir un override")
    if not lock and rule is not None:
        validate_rule(db, sheet_id, rule)

    captured_by_period: Dict[int, Optional[Decimal]] = {}
    if lock:
        result = compute_sheet(sheet_id, db)
        for section_data in result["sections"]:
            for row_data in section_data["rows"]:
                if row_data["row"].id != row.id:
                    continue
                for cr in row_data["cells"]:
                    captured_by_period[cr.period_id] = cr.projected_value

    if lock:
        override_type = "lock"
    elif rule is not None:
        override_type = "manual_rule"
    else:
        override_type = "manual_value"

    results: List[OverrideWriteResult] = []
    for period in periods:
        if lock:
            captured_value = captured_by_period.get(period.id)
            if captured_value is None:
                results.append(OverrideWriteResult(
                    period=period, override=None, skipped=True,
                    skip_reason="the cell has no effective value yet -- nothing to lock",
                ))
                continue
            ov_value, ov_rule = captured_value, None
        elif rule is not None:
            ov_value, ov_rule = None, rule
        else:
            ov_value, ov_rule = value, None

        cell = get_or_create_cell(db, row.id, period.id)
        override, previous = supersede_and_write_override(
            db, cell, ov_value, created_by=created_by, override_type=override_type,
            custom_rule=ov_rule, note=note,
        )
        results.append(OverrideWriteResult(period=period, override=override, previous_override=previous))

    db.commit()
    return results


def clear_override(db, sheet_id: int, row: SheetRow, periods: List[SheetPeriod]) -> List[OverrideClearResult]:
    """Supersede the active override (if any) on every (row, period) cell in
    `periods`. A period with no active override is a no-op for it (reported,
    not an error). After clearing, recomputes the sheet ONCE and reports
    what each cell's value reverted to -- its own rule's result, or None if
    it has none -- which is the whole point of the call.

    Refuses (ValueError, via guard_periods_not_closed) to touch any period
    in `periods` that is_closed, before clearing anything.
    """
    guard_periods_not_closed(periods, "limpiar un override")

    results: List[OverrideClearResult] = []
    for period in periods:
        cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
        previous = supersede_active_override(db, cell) if cell is not None else None
        results.append(OverrideClearResult(period=period, previous_override=previous))
    db.commit()

    result = compute_sheet(sheet_id, db)
    cell_result_by_period: Dict[int, CellResult] = {}
    for section_data in result["sections"]:
        for row_data in section_data["rows"]:
            if row_data["row"].id != row.id:
                continue
            for cr in row_data["cells"]:
                cell_result_by_period[cr.period_id] = cr

    for r in results:
        cr = cell_result_by_period.get(r.period.id)
        r.reverted_value = cr.projected_value if cr else None
        r.reverted_source = cr.effective_source if cr else None

    return results
