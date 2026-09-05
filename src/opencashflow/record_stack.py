"""The "record" append-only stack: a cell's CellActualEntry history, read as
a push/pop undo log.

Any consuming app that lets a user record a real value (paid/accrued/actual)
against a cell over time needs a way to undo the last one without losing the
history that came before it -- CellActualEntry is append-only by design (see
its own docstring in models.py), so "undo" can never edit or delete a row,
only append a new one that restores the previous state. Naively looking at
"the second-to-last entry" breaks the moment undo itself is used twice in a
row: undo's own entry becomes the new last entry, so the next undo would
immediately redo the first instead of walking one step further back.
Replaying the whole log as a push/pop stack -- where an entry tagged as the
result of a previous undo POPS instead of PUSHES -- makes repeated undo walk
backward correctly with no special-casing.
"""
from typing import List, Optional, Tuple

from opencashflow.models import CellActualEntry, SheetCell, SheetPeriod

# Every entry pop_record_stack() itself writes gets this note PREFIX (any
# caller-supplied note text is appended after it, never replaces it) -- it's
# how a later call tells "this entry is itself the result of a previous
# undo" from "this is a real recorded value" when replaying the log. Kept
# private: nothing outside replay_record_stack/pop_record_stack needs to
# read this constant directly -- callers should never hand-roll their own
# note.startswith(...) check against it.
_RECORD_UNDO_MARKER = "[record undo] "


def replay_record_stack(entries: List[CellActualEntry]) -> List[CellActualEntry]:
    """Replays `entries` (already in chronological order, e.g.
    cell.actual_entries) as a push/pop log: an entry whose note starts with
    the undo marker is a POP (the result of a previous pop_record_stack()
    call), every other entry -- however it was written -- is a PUSH. Returns
    the resulting stack (oldest first); its top (`stack[-1]`, when
    non-empty) is the logically "current" entry.
    """
    stack: List[CellActualEntry] = []
    for entry in entries:
        if entry.note and entry.note.startswith(_RECORD_UNDO_MARKER):
            if stack:
                stack.pop()
        else:
            stack.append(entry)
    return stack


def guard_periods_not_closed(periods: List[SheetPeriod], action: str) -> None:
    """Raises ValueError naming every closed period in `periods`, phrased
    around `action` (e.g. "undo a record"), if any of them is_closed. A
    closed period's real values are meant to stay frozen -- see
    opencashflow.period_close.close_period and SheetPeriod.is_closed's own
    docstring for why this is a one-way flag with no reopen mechanism."""
    closed = [p for p in periods if p.is_closed]
    if closed:
        labels = ", ".join(p.label or p.period_date.strftime("%b-%y") for p in closed)
        raise ValueError(
            f"Cannot {action} on an already-closed period: {labels} -- a closed period's real "
            f"values are frozen at what was true when it closed; changing them afterward would "
            f"desynchronize it. There is no mechanism to reopen a closed period."
        )


def pop_record_stack(
    db, cell: SheetCell, *, note: Optional[str], created_by: Optional[int],
) -> Tuple[CellActualEntry, Optional[CellActualEntry], CellActualEntry]:
    """Pops the top of `cell`'s record stack (see replay_record_stack) and
    writes the compensating CellActualEntry. Returns (reverted_from,
    reverted_to, new_entry); `reverted_to` is None when the popped entry was
    the only one left (the cell goes back to "no value recorded").

    Raises ValueError("__empty_stack__") if the stack is already empty --
    callers with their own row/period-named message should catch this and
    raise their own; this function has no such context to phrase one. Does
    NOT check period.is_closed (call guard_periods_not_closed first) or "is
    this the entry I expected on top" -- callers with their own specific
    requirements (e.g. a wallet-movement undo checking it's reverting its
    own write, not something written after it) do those before calling this.
    """
    stack = replay_record_stack(list(cell.actual_entries))
    if not stack:
        raise ValueError("__empty_stack__")

    reverted_from = stack[-1]
    stack_after = stack[:-1]
    reverted_to = stack_after[-1] if stack_after else None

    new_actual = reverted_to.actual_value if reverted_to is not None else None
    new_accrued = reverted_to.accrued_value if reverted_to is not None else None
    new_paid = reverted_to.paid_value if reverted_to is not None else None
    cell.actual_value = new_actual
    cell.accrued_value = new_accrued
    cell.paid_value = new_paid

    # Always keep the marker's trailing space -- replay_record_stack's
    # startswith(_RECORD_UNDO_MARKER) check requires it verbatim, even when
    # there's no caller-supplied note to append after it.
    full_note = _RECORD_UNDO_MARKER + note if note else _RECORD_UNDO_MARKER
    new_entry = CellActualEntry(
        cell_id=cell.id, actual_value=new_actual, accrued_value=new_accrued, paid_value=new_paid,
        note=full_note, created_by=created_by,
    )
    db.add(new_entry)
    db.flush()
    return reverted_from, reverted_to, new_entry
