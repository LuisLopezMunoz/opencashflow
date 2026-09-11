"""Close a cashflow sheet period: reconcile the projection against what
actually happened, freeze SALDO FINAL (the sheet's own "ending balance" row,
whatever it's named) at the real number, and roll any unpaid amount forward
onto the next period.

This is a reusable reconciliation primitive any consuming app's CLI or HTTP
layer can call directly -- close_period() itself never touches sys.exit or
any web-framework exception type, it only ever raises ValueError on a guard
failure, so each caller translates that into whatever its own
error-reporting convention is (stderr + exit code for a CLI, an HTTP 4xx for
a router).
"""
import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from opencashflow.engine import CellResult, compute_sheet
from opencashflow.enums import AGGREGATE_ROW_TYPES
from opencashflow.models import (
    CashflowSheet,
    CellActualEntry,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
    get_or_create_cell,
    supersede_active_override,
    supersede_and_write_override,
)


def _period_label(period: SheetPeriod) -> str:
    return period.label or period.period_date.strftime("%b-%y")


def _next_calendar_month(period_date: datetime) -> datetime:
    """The 1st of the calendar month immediately after `period_date`."""
    if period_date.month == 12:
        return datetime(period_date.year + 1, 1, 1)
    return datetime(period_date.year, period_date.month + 1, 1)


def find_balance_row(db, sheet_id: int) -> SheetRow:
    """Detect the sheet's "starting balance" row -- the only one with a
    previous_period rule -- raising ValueError (never exiting the process)
    on ambiguity or absence. This is the single source of truth for "which
    row is the running balance" that both a CLI and an HTTP endpoint can
    share, so they never disagree."""
    rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet_id).all()
    candidates = [
        r for r in rows
        if r.default_projection_rule and r.default_projection_rule.get("type") == "previous_period"
    ]
    if not candidates:
        raise ValueError(
            f"Sheet #{sheet_id} has no row using the 'previous_period' rule -- "
            f"the starting-balance row cannot be detected."
        )
    if len(candidates) > 1:
        listing = "; ".join(f"[{r.id}] {r.section.name} > {r.name}" for r in candidates)
        raise ValueError(
            f"Sheet #{sheet_id} has {len(candidates)} rows using the 'previous_period' rule -- "
            f"cannot choose which one is the starting balance: {listing}"
        )
    return candidates[0]


def _real_value(row_id: int, period_id: int, row_by_id: Dict[int, SheetRow],
                 cell_result_by_key: Dict[Tuple[int, int], CellResult]) -> Decimal:
    """Recompute a row's value for `period_id` from REAL (settled) numbers
    instead of projected ones, by walking the SAME sum_rows composition the
    engine itself evaluates -- recursing so each row's own sign is applied
    at the level it actually lives at.

    A leaf row's own sign usually isn't enough on its own (see
    opencashflow.engine's sign-resolution trio for the general case): the
    sign flip that makes an expense subtotal SUBTRACT from a higher-level
    total often lives on the SUBTOTAL/TOTAL row, not on the leaves
    underneath it. This function reads each leaf's settled cash (paid, else
    actual, else 0) instead of its projected_value, applying sign once per
    sum_rows level exactly like engine._evaluate_rule's own sum_rows branch
    does for projected numbers.

    Raises ValueError if `row_id` is part of a dependency cycle. This walk
    used to recurse into sum_rows with no cycle protection of its own,
    unlike compute_sheet (which detects cycles up front and marks every
    participating cell error="cycle_detected"); a cyclic sheet close_period
    could compute fine on the projected side used to crash with an unhandled
    RecursionError here, on the real side, violating this module's own
    documented "only ever raises ValueError" contract. Checking cr.error
    reuses compute_sheet's already-cycle-safe result instead of re-detecting
    cycles independently.
    """
    cr = cell_result_by_key.get((row_id, period_id))
    if cr is not None and cr.error == "cycle_detected":
        row = row_by_id.get(row_id)
        row_label = row.name if row else f"#{row_id}"
        raise ValueError(
            f"Cannot close this period: row '{row_label}' has a dependency cycle "
            f"(cycle_detected) in its projection rule -- fix the sheet's sum_rows "
            f"rules before closing."
        )
    row = row_by_id.get(row_id)
    rule = row.default_projection_rule if row else None
    if rule and rule.get("type") == "sum_rows":
        total = Decimal("0")
        for dep_id in rule.get("row_ids", []):
            dep_row = row_by_id.get(dep_id)
            if dep_row is None:
                continue
            sign = -1 if dep_row.sign == "negative" else 1
            total += sign * _real_value(dep_id, period_id, row_by_id, cell_result_by_key)
        return total
    if cr is None:
        return Decimal("0")
    if cr.paid_value is not None:
        return cr.paid_value
    if cr.actual_value is not None:
        return cr.actual_value
    return Decimal("0")


def resolve_balance_final_row(db, balance_row: SheetRow) -> SheetRow:
    """The "ending balance" row is whatever balance_row's own
    previous_period rule points at via row_id -- that's the row this same
    balance_row reads from the previous period to become next month's
    starting balance."""
    rule = balance_row.default_projection_rule or {}
    final_row_id = rule.get("row_id")
    if final_row_id is None:
        raise ValueError(
            f"Row '{balance_row.name}' uses the 'previous_period' rule without 'row_id' -- "
            f"cannot determine its associated ending-balance row."
        )
    final_row = db.query(SheetRow).filter(SheetRow.id == final_row_id).first()
    if final_row is None:
        raise ValueError(
            f"The ending-balance row (row_id={final_row_id}) referenced by '{balance_row.name}' "
            f"no longer exists."
        )
    return final_row


@dataclasses.dataclass
class RolloverEntry:
    row_name: str
    pending_amount: Decimal
    disposition: str


@dataclasses.dataclass
class CloseReport:
    sheet_id: int
    period_id: int
    period_label: str
    balance_row_name: str
    balance_final_row_name: str
    saldo_inicial_value: Decimal
    real_net_flow: Decimal
    saldo_final_value: Decimal
    rollovers: List[RolloverEntry] = dataclasses.field(default_factory=list)
    warnings: List[str] = dataclasses.field(default_factory=list)
    next_period_label: Optional[str] = None
    dry_run: bool = False


def close_period(
    db,
    sheet: CashflowSheet,
    period: SheetPeriod,
    acting_user_id: int,
    *,
    balance_row: Optional[SheetRow] = None,
    assume_unrecorded_as_pending: bool = False,
    dry_run: bool = False,
) -> CloseReport:
    """Close `period` on `sheet`: reconcile projected vs. real cash for
    every leaf row, freeze the ending balance at the real number, and roll
    any unpaid amount forward onto the next period.

    Raises ValueError (never sys.exit, never a web-framework exception) on
    every guard failure. Nothing is written to the database before every
    guard has passed.
    """
    if period.sheet_id != sheet.id:
        raise ValueError(f"Period #{period.id} does not belong to sheet #{sheet.id}.")

    # --- Guard 1: not already closed. ------------------------------------
    if period.is_closed:
        raise ValueError(f"Period {_period_label(period)} is already closed.")

    # --- Guard 2: every earlier period on this sheet is already closed. --
    open_earlier = (
        db.query(SheetPeriod)
        .filter(
            SheetPeriod.sheet_id == sheet.id,
            SheetPeriod.sort_order < period.sort_order,
            SheetPeriod.is_closed.is_(False),
        )
        .order_by(SheetPeriod.sort_order)
        .all()
    )
    if open_earlier:
        labels = ", ".join(_period_label(p) for p in open_earlier)
        raise ValueError(
            f"Cannot close {_period_label(period)}: earlier period(s) are still open "
            f"({labels}) -- this period's starting balance depends on those periods' "
            f"ending balance already reflecting reality."
        )

    # --- Resolve starting-balance / ending-balance rows. ------------------
    if balance_row is None:
        balance_row = find_balance_row(db, sheet.id)
    balance_final_row = resolve_balance_final_row(db, balance_row)

    # balance_final_row's own sum_rows tells us exactly what feeds into it
    # besides balance_row -- whether that's one aggregate "net flow" row or
    # several leaves summed straight in, both are legitimate shapes this
    # function must support without assuming a fixed number of dependencies.
    # real_net_flow below sums each one's REAL value (see _real_value) with
    # its own sign, exactly like the engine's own sum_rows evaluation would
    # with projected values.
    final_rule = balance_final_row.default_projection_rule or {}
    if final_rule.get("type") != "sum_rows":
        raise ValueError(
            f"'{balance_final_row.name}' does not use a 'sum_rows' rule -- the real net flow "
            f"cannot be computed generically."
        )
    final_row_ids = final_rule.get("row_ids", [])
    if balance_row.id not in final_row_ids:
        raise ValueError(
            f"'{balance_final_row.name}' does not include '{balance_row.name}' in its 'sum_rows' "
            f"rule -- the starting/ending balance pair doesn't have the expected shape."
        )
    net_flow_row_ids = [rid for rid in final_row_ids if rid != balance_row.id]

    # --- Compute the whole sheet once; every lookup below reads from this
    # single result (never recomputed independently, per design). ---------
    result = compute_sheet(sheet.id, db)
    cell_result_by_key: Dict[Tuple[int, int], CellResult] = {}
    row_by_id: Dict[int, SheetRow] = {}
    for section_data in result["sections"]:
        for row_data in section_data["rows"]:
            row = row_data["row"]
            row_by_id[row.id] = row
            for cr in row_data["cells"]:
                cell_result_by_key[(cr.row_id, cr.period_id)] = cr

    period_label = _period_label(period)
    excluded_row_ids = {balance_row.id, balance_final_row.id}

    # --- Guard: starting balance must resolve to an actual value. Checked
    # here, before any write (including the assume_unrecorded_as_pending
    # backfills below), to keep the "nothing is written before every guard
    # has passed" contract true even though this specific check needs
    # compute_sheet()'s result to evaluate. --------------------------------
    balance_cr = cell_result_by_key.get((balance_row.id, period.id))
    saldo_inicial_value = balance_cr.projected_value if balance_cr else None
    if saldo_inicial_value is None:
        raise ValueError(
            f"'{balance_row.name}' has no projected value for {period_label} -- cannot compute "
            f"the real ending balance without a starting balance. If this is the sheet's first "
            f"period, write a manual override with the real starting balance first."
        )

    warnings: List[str] = []
    pending_by_row: List[Tuple[SheetRow, Decimal]] = []

    # --- Write phase. Every guard above this point has already passed, so
    # from here on this function is actually mutating the session -- wrapped
    # in try/except so an unexpected failure partway through (a constraint
    # violation, a bug in a helper) leaves the database exactly as it was
    # before this call started, instead of a half-applied close. -----------
    try:
        for row_id, row in row_by_id.items():
            if row.row_type in AGGREGATE_ROW_TYPES or row_id in excluded_row_ids:
                continue
            cr = cell_result_by_key.get((row_id, period.id))
            if cr is None:
                continue

            accrued = cr.accrued_value
            paid = cr.paid_value
            projected = cr.projected_value

            if accrued is not None and paid is not None:
                pending_for_close = max(Decimal("0"), accrued - paid)
            elif accrued is not None:
                pending_for_close = accrued
            elif projected not in (None, 0):
                if assume_unrecorded_as_pending:
                    pending_for_close = projected
                    cell = get_or_create_cell(db, row_id, period.id)
                    cell.accrued_value = projected
                    cell.paid_value = Decimal("0")
                    db.add(CellActualEntry(
                        cell_id=cell.id,
                        actual_value=cell.actual_value,
                        accrued_value=cell.accrued_value,
                        paid_value=cell.paid_value,
                        note=(
                            f"period_close: no data recorded for {period_label}, assumed fully "
                            f"pending (projection {projected})."
                        ),
                        created_by=acting_user_id,
                    ))
                    db.flush()
                else:
                    pending_for_close = Decimal("0")
                    warnings.append(row.name)
            else:
                pending_for_close = Decimal("0")

            if pending_for_close > 0:
                pending_by_row.append((row, pending_for_close))

        real_net_flow = Decimal("0")
        for dep_id in net_flow_row_ids:
            dep_row = row_by_id.get(dep_id)
            if dep_row is None:
                continue
            sign = -1 if dep_row.sign == "negative" else 1
            real_net_flow += sign * _real_value(dep_id, period.id, row_by_id, cell_result_by_key)
        saldo_final_value = saldo_inicial_value + real_net_flow

        # --- Write the real ending balance for this period. ----------------
        balance_final_cell = get_or_create_cell(db, balance_final_row.id, period.id)
        supersede_and_write_override(
            db, balance_final_cell, saldo_final_value, created_by=acting_user_id,
            note=f"period_close: real ending balance for {period_label}",
        )

        # --- Roll unpaid amounts forward onto the next period. Looked up by
        # calendar date (the next calendar month), not sort_order + 1: a gap
        # in sort_order (a manually-edited sheet, a partial backfill) used to
        # make this behave exactly like "no next period" even when a
        # chronologically-adjacent period actually existed elsewhere. -------
        next_period_date = _next_calendar_month(period.period_date)
        next_period = (
            db.query(SheetPeriod)
            .filter(SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == next_period_date)
            .first()
        )
        next_period_label = _period_label(next_period) if next_period else None

        rollovers: List[RolloverEntry] = []
        for row, pending in pending_by_row:
            rule = row.default_projection_rule
            if rule and rule.get("type") == "carry_forward":
                rollovers.append(RolloverEntry(row.name, pending, "auto (carry_forward rule)"))
                continue

            if next_period is None:
                rollovers.append(
                    RolloverEntry(row.name, pending, "no next period (could not roll forward)")
                )
                # Also surfaced as a warning (not just buried in rollovers'
                # free-text disposition) -- this pending amount is about to
                # vanish from the system's persisted state entirely once
                # this call returns, which is easy to miss otherwise.
                warnings.append(
                    f"{row.name}: {pending} pending with no next period to roll forward onto -- "
                    f"this amount will not be tracked anywhere after this close."
                )
                continue

            next_cr = cell_result_by_key.get((row.id, next_period.id))
            base_next = next_cr.projected_value if next_cr else None
            base_next_amt = base_next if base_next is not None else Decimal("0")
            new_value = base_next_amt + pending

            next_cell = get_or_create_cell(db, row.id, next_period.id)
            replaced = supersede_and_write_override(
                db, next_cell, new_value, created_by=acting_user_id,
                note=(
                    f"period_close: original projection {base_next_amt} + unpaid rollover "
                    f"{pending} from {period_label}"
                ),
            )
            disposition = (
                "override written (replaced an existing override)" if replaced is not None else "override written"
            )
            rollovers.append(RolloverEntry(row.name, pending, disposition))

        # --- Mark the period closed and finish the transaction. ------------
        period.is_closed = True
        db.flush()
    except Exception:
        db.rollback()
        raise

    report = CloseReport(
        sheet_id=sheet.id,
        period_id=period.id,
        period_label=period_label,
        balance_row_name=balance_row.name,
        balance_final_row_name=balance_final_row.name,
        saldo_inicial_value=saldo_inicial_value,
        real_net_flow=real_net_flow,
        saldo_final_value=saldo_final_value,
        rollovers=rollovers,
        warnings=warnings,
        next_period_label=next_period_label,
        dry_run=dry_run,
    )

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return report


@dataclasses.dataclass
class ReopenReport:
    sheet_id: int
    period_id: int
    period_label: str
    balance_final_row_name: str
    touched_notes: List[str] = dataclasses.field(default_factory=list)
    dry_run: bool = False


def reopen_period(
    db,
    sheet: CashflowSheet,
    period: SheetPeriod,
    *,
    balance_row: Optional[SheetRow] = None,
    dry_run: bool = False,
) -> ReopenReport:
    """Undo close_period's one effect that reverses cleanly on its own:
    supersede the frozen "real ending balance" override on
    balance_final_row's cell for this period (so it recomputes from its own
    sum_rows rule again), and flip is_closed back to False.

    Deliberately narrow -- this does NOT automatically undo any
    assume_unrecorded_as_pending backfill (a CellActualEntry written onto an
    individual row) or rollover (a CellOverride written onto the NEXT
    period) the original close_period call may also have made. Reversing
    those correctly would mean re-deriving exactly what that close touched
    and unwinding it, including anything already built on top of it since
    (a later close, further records) -- real cascading-undo complexity with
    no bug behind it, deferred rather than risked. Instead,
    ReopenReport.touched_notes lists close_period's own backfill entries (on
    THIS period) and rollover overrides (on the NEXT period ONLY -- never on
    this period, since a "period_close:"-prefixed override active on THIS
    period's own cells could only be a rollover written by the PREVIOUS
    period's own close, not by this one) still carrying one of its
    "period_close: ..." note prefixes, so a caller can inspect and manually
    address them (`record undo` / `override clear`) if a fully clean
    re-close is wanted.

    Raises ValueError if:
      - `period` is not currently closed.
      - Any LATER period on `sheet` is already closed -- mirrors
        close_period's own "earlier periods must already be closed" guard,
        in reverse: a later period's own close already depends on this
        period's ending balance staying frozen as real.
    """
    if period.sheet_id != sheet.id:
        raise ValueError(f"Period #{period.id} does not belong to sheet #{sheet.id}.")
    if not period.is_closed:
        raise ValueError(f"Period {_period_label(period)} is not closed -- nothing to reopen.")

    # --- Guard: no LATER period is already closed. -------------------------
    later_closed = (
        db.query(SheetPeriod)
        .filter(
            SheetPeriod.sheet_id == sheet.id,
            SheetPeriod.sort_order > period.sort_order,
            SheetPeriod.is_closed.is_(True),
        )
        .order_by(SheetPeriod.sort_order)
        .all()
    )
    if later_closed:
        labels = ", ".join(_period_label(p) for p in later_closed)
        raise ValueError(
            f"Cannot reopen {_period_label(period)}: later period(s) are already closed "
            f"({labels}) -- their own close already depends on this period's ending balance "
            f"staying frozen as real. Reopen them first, in reverse chronological order."
        )

    if balance_row is None:
        balance_row = find_balance_row(db, sheet.id)
    balance_final_row = resolve_balance_final_row(db, balance_row)
    period_label = _period_label(period)

    # --- Guard: the cell's active override must actually be the one
    # close_period wrote for THIS close -- never assume it, since the
    # sheet's balance-row wiring (auto-detected via find_balance_row, or an
    # explicit `balance_row` passed in) could differ from what was true at
    # close time, or something else (e.g. a plain `override set`) could have
    # since overwritten it. Silently superseding whatever happens to be
    # active there would flip is_closed back to False while leaving the REAL
    # frozen close_period override untouched elsewhere, and report success.
    # Checked before any write, so "nothing written before every guard
    # passes" stays true even for this DB-read-dependent check. -------------
    expected_note_prefix = "period_close: real ending balance"
    balance_final_cell = (
        db.query(SheetCell)
        .filter(SheetCell.row_id == balance_final_row.id, SheetCell.period_id == period.id)
        .first()
    )
    active_override = None
    if balance_final_cell is not None:
        for ov in balance_final_cell.overrides:
            if ov.superseded_at is None:
                active_override = ov
                break
    if active_override is None:
        raise ValueError(
            f"Cannot reopen {period_label}: '{balance_final_row.name}' has no active override "
            f"for this period even though it is marked closed -- close_period always writes "
            f"one, so this looks like drift since the close. Investigate before reopening."
        )
    if not (active_override.note or "").startswith(expected_note_prefix):
        raise ValueError(
            f"Cannot reopen {period_label}: the active override on '{balance_final_row.name}' "
            f"for this period does not look like the one close_period wrote (note: "
            f"{active_override.note!r}) -- refusing to supersede a value that may not be "
            f"close_period's own frozen ending balance. Investigate before reopening."
        )

    try:
        supersede_active_override(db, balance_final_cell)
        period.is_closed = False
        db.flush()

        # --- Surface (read-only) anything else the original close touched,
        # so the caller can decide whether to clean it up by hand. A
        # "period_close:"-prefixed override can only ever legitimately exist
        # on the NEXT period (the rollover close_period writes above) -- the
        # "real ending balance" override close_period writes onto
        # `period.id` itself is exactly the one already superseded above, so
        # it's excluded by superseded_at.is_(None) by the time this query
        # runs regardless. Querying period.id here too used to surface a
        # PREVIOUS period's own rollover (written onto period.id as ITS
        # "next period") as if THIS reopen's close had made it --
        # misattributing real carried-forward financial history as "this
        # reopen's leftover mess" and inviting an operator to
        # `override clear` it. ------------------------------------------
        next_period = (
            db.query(SheetPeriod)
            .filter(SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == _next_calendar_month(period.period_date))
            .first()
        )

        touched_notes: List[str] = []
        touched_overrides = []
        if next_period is not None:
            touched_overrides = (
                db.query(CellOverride)
                .join(SheetCell, CellOverride.cell_id == SheetCell.id)
                .filter(
                    SheetCell.period_id == next_period.id,
                    CellOverride.superseded_at.is_(None),
                    CellOverride.note.isnot(None),
                    CellOverride.note.like("period_close:%"),
                )
                .all()
            )
        for ov in touched_overrides:
            touched_notes.append(f"{ov.cell.row.name}: override still active -- \"{ov.note}\"")

        touched_entries = (
            db.query(CellActualEntry)
            .join(SheetCell, CellActualEntry.cell_id == SheetCell.id)
            .filter(
                SheetCell.period_id == period.id,
                CellActualEntry.note.isnot(None),
                CellActualEntry.note.like("period_close:%"),
            )
            .all()
        )
        for entry in touched_entries:
            touched_notes.append(f"{entry.cell.row.name}: real entry recorded by the original close -- \"{entry.note}\"")
    except Exception:
        db.rollback()
        raise

    report = ReopenReport(
        sheet_id=sheet.id,
        period_id=period.id,
        period_label=period_label,
        balance_final_row_name=balance_final_row.name,
        touched_notes=touched_notes,
        dry_run=dry_run,
    )

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return report
