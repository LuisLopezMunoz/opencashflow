"""Credit card statement projection, "cupo disponible" (available credit),
and sync into a cashflow sheet cell.

Every write this module makes into a cashflow sheet goes through the exact
same CellOverride/CellActualEntry patterns opencashflow.period_close and a
consuming app's own CLI already use (see sync_period below), so from the
engine's point of view a synced credit card cell looks exactly like any
other manually-recorded cell. Nothing here ever calls sys.exit -- every
guard failure is a plain ValueError, so each caller translates it into
whatever its own error-reporting convention is.

Design: a statement is either fully REAL (a CreditCardStatement row exists
for that exact (card, billing_period)) or it doesn't exist at all yet --
there is no partial/in-progress statement state. For a period with no
CreditCardStatement row, "what's due this cycle" is built entirely by
projecting from CreditCardCharge rows that have a first_statement_period
set (see compute_instant_statement).

Deliberately NOT here: any strategy for deciding how much to draw from
which card to cover a projected deficit (ranking cards, choosing between
them) -- that's a consuming app's own financial strategy, not something
every user of this package shares. `_cupo_disponible_real_ahora`'s
`exclude_category` parameter exists for exactly this seam: an app that
creates its own synthetic "planned draw" charges (tagged with some
category of its own choosing) can exclude them from today's real cupo
without this module needing to know what that category means.
"""
import dataclasses
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from opencashflow.credit_card import CreditCard, CreditCardCharge, CreditCardStatement, CreditCardStatementLine
from opencashflow.models import CashflowSheet, CellActualEntry, CellOverride, SheetCell, SheetPeriod, SheetRow


# ---------------------------------------------------------------------------
# Part 2: rounding-remainder helper
# ---------------------------------------------------------------------------

def installment_schedule(total_amount: Decimal, installments: int) -> List[Decimal]:
    """Split `total_amount` into exactly `installments` Decimal cuota
    amounts that sum EXACTLY to `total_amount`.

    Convention (default behavior only -- see the warning below): cuotas
    1..N-1 are `total_amount // installments` (floor division), and cuota N
    absorbs whatever remainder that leaves. For 1_000_000 split 3 ways this
    is [333333, 333333, 333334], not [333333.33, 333333.33, 333333.34] --
    this package never fabricates sub-peso precision the bank itself didn't
    print.

    WARNING: this is an ASSUMED convention, unconfirmed against any real
    bank statement -- some issuers round the LAST cuota down instead, or
    distribute the remainder across the first N cuotas one peso at a time.
    This function is only ever used to build the NOT-YET-REAL projected
    view (see compute_instant_statement) -- a REAL CreditCardStatementLine
    for a given (charge_id, installment_number), once one exists, always
    overrides this computed guess and this function is never consulted for
    that installment again.
    """
    if installments < 1:
        raise ValueError(f"installments debe ser >= 1 (recibido: {installments}).")
    base = total_amount // installments
    schedule = [base] * (installments - 1)
    schedule.append(total_amount - base * (installments - 1))
    return schedule


# ---------------------------------------------------------------------------
# Card resolution -- exact-then-substring-then-hard-error, same philosophy
# as opencashflow.cli's row/section resolution. Never guesses on ambiguity.
# ---------------------------------------------------------------------------

def _match_cards_by_name(cards: List[CreditCard], needle: str) -> List[CreditCard]:
    needle_cf = needle.casefold()
    exact = [c for c in cards if c.name.casefold() == needle_cf]
    if exact:
        return exact
    return [c for c in cards if needle_cf in c.name.casefold()]


def _shift_months(d: date, delta: int) -> date:
    total = d.year * 12 + (d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def _payment_lag_months(card: CreditCard) -> int:
    """How many calendar months after a statement's billing_period its total
    is actually due/paid. A Chilean credit card statement closes on
    `closing_day` and is due on `due_day` -- when due_day is numerically
    less than closing_day, the due date necessarily falls in the FOLLOWING
    month (a statement closing the 25th can't have a due date of the 9th of
    that same month, that's before it even closes). This is real, observed
    behavior (confirmed against two different real statements: Santander
    closing_day=25/due_day=9, BCI closing_day=25/due_day=5 -- both pay the
    month after closing), not a guess -- so it's derived from fields
    already on CreditCard rather than a new one. Defaults to 1 (the
    near-universal convention) when closing_day/due_day aren't set, since
    compute_instant_statement/sync_period already require them for
    anything else useful anyway.
    """
    if card.closing_day is None or card.due_day is None:
        return 1
    return 1 if card.due_day < card.closing_day else 0


def resolve_credit_card(db, user_id_or_none: Optional[int], card_arg: str) -> CreditCard:
    """Resolve `card_arg` (numeric id, exact name, or substring) to a
    CreditCard, optionally scoped to `user_id_or_none`'s own cards. Raises
    ValueError (never sys.exit) listing every candidate on ambiguity --
    never guesses which card was meant.
    """
    query = db.query(CreditCard)
    if user_id_or_none is not None:
        query = query.filter(CreditCard.user_id == user_id_or_none)

    scope_note = f" del usuario #{user_id_or_none}" if user_id_or_none is not None else ""

    if card_arg.isdigit():
        card = query.filter(CreditCard.id == int(card_arg)).first()
        if card is None:
            raise ValueError(f"No existe la tarjeta #{card_arg}{scope_note}.")
        return card

    cards = query.all()
    matches = _match_cards_by_name(cards, card_arg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listing = "; ".join(f"[{c.id}] {c.name}" for c in matches)
        raise ValueError(f"'{card_arg}' es ambiguo, coincide con {len(matches)} tarjetas{scope_note}: {listing}")
    raise ValueError(f"No se encontró ninguna tarjeta que coincida con '{card_arg}'{scope_note}.")


# ---------------------------------------------------------------------------
# compute_instant_statement
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class InstantStatementLine:
    description: str
    amount: Decimal
    installment_number: Optional[int]
    installment_total: Optional[int]
    is_projected: bool


@dataclasses.dataclass
class InstantStatement:
    card: CreditCard
    period: date  # always day-normalized to the 1st of the month
    # None (with previous_balance_known=False) when no real prior statement
    # exists -- NEVER defaulted to 0 or to card.current_balance (that field
    # is the old bookkeeping counter this feature supersedes).
    previous_balance: Optional[Decimal]
    previous_balance_known: bool
    # True iff a real CreditCardStatement already exists for this exact
    # (card, period) -- in which case every field above/below is read
    # straight off that row, no projection math involved.
    is_real: bool
    lines: List[InstantStatementLine]
    charges_due_this_cycle: Decimal


def _installment_number_for_period(first_statement_period: date, period: date) -> Optional[int]:
    """Which installment_number (1-based) of a charge whose #1 cuota landed
    in `first_statement_period` falls in `period`, via plain month
    arithmetic. Returns None if `period` is before first_statement_period
    (this charge hasn't started yet) -- the caller separately checks the
    upper bound against installments (has this charge already finished?).
    """
    months_diff = (period.year - first_statement_period.year) * 12 + (period.month - first_statement_period.month)
    if months_diff < 0:
        return None
    return months_diff + 1


def compute_instant_statement(db, card: CreditCard, period: date) -> InstantStatement:
    """The "live" view of `card`'s statement as it affects the cashflow
    sheet's `period` (day-normalized to the 1st of the month, and understood
    as the PAYMENT month, not the billing month -- see _payment_lag_months):
    the real statement if one has already been recorded for the
    corresponding billing cycle, otherwise a purely projected one built from
    active charges.
    """
    period_norm = date(period.year, period.month, 1)
    billing_period_norm = _shift_months(period_norm, -_payment_lag_months(card))

    real_statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period == billing_period_norm)
        .first()
    )
    if real_statement is not None:
        lines = [
            InstantStatementLine(
                description=line.description,
                amount=line.amount,
                installment_number=line.installment_number,
                installment_total=line.installment_total,
                is_projected=False,
            )
            for line in real_statement.lines
        ]
        return InstantStatement(
            card=card,
            period=period_norm,
            previous_balance=real_statement.previous_balance,
            previous_balance_known=real_statement.previous_balance is not None,
            is_real=True,
            lines=lines,
            charges_due_this_cycle=real_statement.total_payment_due,
        )

    # --- Not real yet: previous_balance comes from the most recent real
    # statement strictly before this billing cycle, if any. -------------------
    prior_statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period < billing_period_norm)
        .order_by(CreditCardStatement.billing_period.desc())
        .first()
    )
    previous_balance = prior_statement.new_balance if prior_statement is not None else None
    previous_balance_known = prior_statement is not None

    # --- Build the projected lines: every charge on this card with a known
    # first_statement_period whose Nth cuota lands in this exact period,
    # skipping any (charge_id, installment_number) already accounted for by
    # a REAL line on some other already-real statement (never double count).
    lines: List[InstantStatementLine] = []
    charges_due_this_cycle = Decimal("0")
    charges = (
        db.query(CreditCardCharge)
        .filter(CreditCardCharge.credit_card_id == card.id, CreditCardCharge.first_statement_period.isnot(None))
        .all()
    )
    for charge in charges:
        installment_number = _installment_number_for_period(charge.first_statement_period, billing_period_norm)
        if installment_number is None or installment_number > charge.installments:
            continue  # this charge hasn't started yet, or has already fully cycled through

        already_real = (
            db.query(CreditCardStatementLine)
            .filter(
                CreditCardStatementLine.charge_id == charge.id,
                CreditCardStatementLine.installment_number == installment_number,
            )
            .first()
        )
        if already_real is not None:
            continue

        schedule = installment_schedule(Decimal(str(charge.amount)), charge.installments)
        amount = schedule[installment_number - 1]
        lines.append(InstantStatementLine(
            description=charge.description or "(sin descripción)",
            amount=amount,
            installment_number=installment_number,
            installment_total=charge.installments,
            is_projected=True,
        ))
        charges_due_this_cycle += amount

    return InstantStatement(
        card=card,
        period=period_norm,
        previous_balance=previous_balance,
        previous_balance_known=previous_balance_known,
        is_real=False,
        lines=lines,
        charges_due_this_cycle=charges_due_this_cycle,
    )


# ---------------------------------------------------------------------------
# "cupo disponible" (available credit)
# ---------------------------------------------------------------------------

def _current_open_billing_period(card: CreditCard, today: date) -> date:
    """The billing cycle currently open for `card`, identified (as above)
    by the calendar month it CLOSES in. If today hasn't passed closing_day
    yet, the open cycle is the one closing THIS month; otherwise it's the
    one closing next month. today.day == closing_day is treated as "still
    this cycle" -- an unconfirmed edge case (no real statement to check it
    against), documented here rather than silently guessed differently.
    """
    if card.closing_day is None:
        raise ValueError(f"La tarjeta '{card.name}' no tiene día de cierre (--closing-day) definido.")
    today_norm = date(today.year, today.month, 1)
    if today.day <= card.closing_day:
        return today_norm
    return _shift_months(today_norm, 1)


def _remaining_committed(charge: CreditCardCharge, current_open_billing_period: date) -> Decimal:
    """How much of `charge` is still committed against the card's limit as
    of `current_open_billing_period`: every installment from that cycle
    onward, assuming every EARLIER cycle already closed and was paid in
    full (see cupo_disponible's docstring for why that assumption is
    printed, never silent)."""
    schedule = installment_schedule(Decimal(str(charge.amount)), charge.installments)
    installment_number = _installment_number_for_period(charge.first_statement_period, current_open_billing_period)
    already_closed = 0 if installment_number is None else max(0, installment_number - 1)
    return sum(schedule[min(already_closed, len(schedule)):], Decimal("0"))


def _real_remaining_principal(db, card: CreditCard, current_open_billing_period: date) -> Optional[Decimal]:
    """The bank's own reported "SALDO CAPITAL CUOTAS" (see
    CreditCardStatement.remaining_principal_installments) from the REAL
    statement of the cycle that most recently closed -- i.e. the cycle
    immediately before `current_open_billing_period`, matching exactly the
    "every closed cycle is assumed paid" cutoff _remaining_committed already
    uses. Deliberately an EXACT match on that one specific prior cycle, not
    "whatever real statement is most recent" -- a stale figure from months
    ago must never masquerade as today's committed debt. Returns None if no
    such statement exists yet, or it exists but never had this field
    populated (older statement, or a bank/month that doesn't print it) --
    the caller falls back to the projected estimate either way.
    """
    previous_cycle = _shift_months(current_open_billing_period, -1)
    statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period == previous_cycle)
        .first()
    )
    if statement is None:
        return None
    return statement.remaining_principal_installments


def _cupo_disponible_detail(db, card: CreditCard, today: date) -> Tuple[Decimal, bool]:
    """(cupo_disponible, is_estimate). credit_limit menos el saldo
    comprometido -- real si el banco ya reportó "SALDO CAPITAL CUOTAS" para
    el ciclo que acaba de cerrar (is_estimate=False), o si no, una
    estimación pareja de las cuotas futuras de todas las compras activas
    (is_estimate=True) que puede sobreestimar el capital comprometido al
    incluir interés que todavía no se devenga -- confirmado contra un
    estado de cuenta real, donde la estimación sobrestimaba el capital
    comprometido en $249.729 sobre una deuda de ~$2,5M.

    Ambas rutas cargan la misma suposición explícita: todo ciclo ya CERRADO
    se asume pagado en su totalidad. Si esa disciplina no se cumplió alguna
    vez para esta tarjeta, el número (real o estimado) queda sobreestimado
    -- esto debe imprimirse en cualquier reporte que use esta función,
    nunca ocultarse.
    """
    current_open = _current_open_billing_period(card, today)
    charges = (
        db.query(CreditCardCharge)
        .filter(CreditCardCharge.credit_card_id == card.id, CreditCardCharge.first_statement_period.isnot(None))
        .all()
    )

    real = _real_remaining_principal(db, card, current_open)
    if real is not None:
        # "real" is the bank's own SALDO CAPITAL CUOTAS as of the last
        # CLOSED cycle -- it already accounts for every charge that existed
        # by then. A charge whose first_statement_period lands in the
        # CURRENTLY open cycle or later (a brand-new purchase since that
        # statement closed, or a synthetic charge a caller may have just
        # created) is NOT reflected in that figure and must be subtracted
        # separately, or a second cupo_disponible() call (a later period in
        # a multi-period projection, or simply a second call next month
        # before a new statement exists) would report the same room as
        # still free and let it be drawn twice.
        newly_committed = sum(
            (_remaining_committed(c, current_open) for c in charges if c.first_statement_period >= current_open),
            Decimal("0"),
        )
        return Decimal(str(card.credit_limit)) - real - newly_committed, False

    committed = sum((_remaining_committed(c, current_open) for c in charges), Decimal("0"))
    return Decimal(str(card.credit_limit)) - committed, True


def cupo_disponible(db, card: CreditCard, today: date) -> Decimal:
    """See _cupo_disponible_detail -- this is the value-only convenience
    wrapper kept for callers (and tests) that don't need to know whether the
    figure is real or estimated."""
    return _cupo_disponible_detail(db, card, today)[0]


def _is_last_statement_paid(db, card: CreditCard, previous_cycle: date, statement: Optional[CreditCardStatement]) -> bool:
    """Whether `previous_cycle`'s bill has actually been paid, per the
    mapped sheet row's own real layer (SheetCell.paid_value, written by
    `record set`/sync_period) -- the SAME source of truth `available`/
    `show --with-real` already use, not CreditCardStatement.payments_received
    (deliberately left unused). No mapped row, no statement, or no cell yet
    all mean "can't confirm it's paid" -> treated as unpaid, the safe
    default (never silently assumes a bill got paid).
    """
    if statement is None or card.mapped_row_id is None or card.mapped_sheet_id is None:
        return False
    payment_period = _shift_months(previous_cycle, _payment_lag_months(card))
    payment_period_dt = datetime(payment_period.year, payment_period.month, 1)
    period = (
        db.query(SheetPeriod)
        .filter(SheetPeriod.sheet_id == card.mapped_sheet_id, SheetPeriod.period_date == payment_period_dt)
        .first()
    )
    if period is None:
        return False
    cell = (
        db.query(SheetCell)
        .filter(SheetCell.row_id == card.mapped_row_id, SheetCell.period_id == period.id)
        .first()
    )
    return cell is not None and cell.paid_value is not None and cell.paid_value >= statement.total_payment_due


def _cupo_disponible_real_ahora(
    db, card: CreditCard, today: date, *, exclude_category: Optional[str] = None,
) -> Tuple[Decimal, bool]:
    """(cupo_disponible_real_ahora, is_estimate): what the bank's own app
    would show for this card RIGHT NOW -- deliberately different from
    _cupo_disponible_detail, which is a PLANNING figure that assumes the
    just-closed cycle's bill is already paid in full even before that
    payment actually happens. Two corrections on top of the same building
    blocks:

      1. If the just-closed cycle's bill (previous_cycle's
         CreditCardStatement.total_payment_due) has NOT actually been paid
         yet (_is_last_statement_paid), it still occupies room on the
         card TODAY -- subtracted in full, not assumed cleared.
      2. `exclude_category`, when given, excludes any charge tagged with
         that exact category from "new real charges since the last closed
         cycle" -- for a consuming app that creates its own synthetic
         "planned draw" charges (a PLAN, not a purchase that has actually
         happened yet -- the bank has never seen it), passing that
         category here keeps such planned charges from occupying room in
         today's real cupo. This module doesn't need to know what the
         category means, only that charges tagged with it should be
         excluded.
    """
    current_open = _current_open_billing_period(card, today)
    previous_cycle = _shift_months(current_open, -1)
    statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period == previous_cycle)
        .first()
    )
    unpaid_last_bill = Decimal("0")
    if statement is not None and not _is_last_statement_paid(db, card, previous_cycle, statement):
        unpaid_last_bill = statement.total_payment_due

    charges = (
        db.query(CreditCardCharge)
        .filter(CreditCardCharge.credit_card_id == card.id, CreditCardCharge.first_statement_period.isnot(None))
        .all()
    )
    new_real_charges = sum(
        (_remaining_committed(c, current_open) for c in charges
         if c.first_statement_period >= current_open and (exclude_category is None or c.category != exclude_category)),
        Decimal("0"),
    )

    real_principal = statement.remaining_principal_installments if statement is not None else None
    if real_principal is not None:
        return Decimal(str(card.credit_limit)) - real_principal - unpaid_last_bill - new_real_charges, False

    pre_existing = sum(
        (_remaining_committed(c, current_open) for c in charges if c.first_statement_period < current_open),
        Decimal("0"),
    )
    return Decimal(str(card.credit_limit)) - pre_existing - unpaid_last_bill - new_real_charges, True


# ---------------------------------------------------------------------------
# sync_period
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SyncReport:
    card_id: int
    card_name: str
    sheet_id: int
    row_id: int
    period: date  # day-normalized to the 1st of the month
    branch: str  # "real_statement" | "projection"
    old_value: Optional[Decimal]
    new_value: Decimal
    note: str
    dry_run: bool = False


def _get_or_create_cell(db, row_id: int, period_id: int) -> SheetCell:
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    return cell


def _supersede_and_write_override(
    db, cell: SheetCell, value: Decimal, note: str, created_by: int,
) -> Optional[CellOverride]:
    """Supersede the cell's active override (if any) and insert a new
    manual_value one. Returns the superseded override (or None) so the
    caller can report its old value."""
    previous = None
    for ov in cell.overrides:
        if ov.superseded_at is None:
            previous = ov
            break
    if previous is not None:
        previous.superseded_at = datetime.utcnow()
        db.flush()

    db.add(CellOverride(cell_id=cell.id, value=value, override_type="manual_value", note=note, created_by=created_by))
    db.flush()
    return previous


def _sync_period_write(
    db,
    card: CreditCard,
    sheet: Optional[CashflowSheet],
    row: Optional[SheetRow],
    period: date,
    acting_user_id: int,
) -> SyncReport:
    """Everything sync_period() does EXCEPT deciding dry_run/commit --
    factored out so a caller that's already managing its own open
    transaction across several writes (e.g. a multi-period draw preview
    that syncs a drawn card's future bill mid-loop, all inside one outer
    preview transaction) can reuse this exact write logic without
    sync_period's own commit()/rollback() firing early and discarding
    everything else already written in that same session. See
    sync_period's own docstring for what this actually writes -- unchanged,
    just without the transaction-boundary decision at the end. Returns a
    SyncReport with dry_run left at its dataclass default (False); the
    caller is responsible for setting it to whatever's actually true before
    showing it to anyone.
    """
    period_norm = date(period.year, period.month, 1)

    sheet_id = sheet.id if sheet is not None else card.mapped_sheet_id
    row_id = row.id if row is not None else card.mapped_row_id
    if sheet_id is None or row_id is None:
        raise ValueError(
            f"La tarjeta '{card.name}' no tiene una fila mapeada en ninguna planilla -- corre "
            f"'creditcard map' primero, o pasa sheet/row explícitamente."
        )

    sheet_obj = sheet
    if sheet_obj is None:
        sheet_obj = db.query(CashflowSheet).filter(CashflowSheet.id == sheet_id).first()
        if sheet_obj is None:
            raise ValueError(
                f"La planilla #{sheet_id} mapeada en la tarjeta '{card.name}' ya no existe -- "
                f"corre 'creditcard map' de nuevo."
            )

    row_obj = row
    if row_obj is None:
        row_obj = db.query(SheetRow).filter(SheetRow.id == row_id).first()
        if row_obj is None:
            raise ValueError(
                f"La fila #{row_id} mapeada en la tarjeta '{card.name}' ya no existe -- "
                f"corre 'creditcard map' de nuevo."
            )

    if row_obj.section.sheet_id != sheet_obj.id:
        raise ValueError(
            f"La fila '{row_obj.name}' (#{row_obj.id}) no pertenece a la planilla #{sheet_obj.id} "
            f"('{sheet_obj.name}')."
        )

    period_dt = datetime(period_norm.year, period_norm.month, 1)
    sheet_period = (
        db.query(SheetPeriod)
        .filter(SheetPeriod.sheet_id == sheet_obj.id, SheetPeriod.period_date == period_dt)
        .first()
    )
    if sheet_period is None:
        raise ValueError(
            f"La planilla #{sheet_obj.id} no tiene un período para {period_norm.strftime('%Y-%m')}."
        )

    period_label = period_norm.strftime("%Y-%m")
    lag = _payment_lag_months(card)
    billing_period_norm = _shift_months(period_norm, -lag)
    billing_label = billing_period_norm.strftime("%Y-%m")

    real_statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period == billing_period_norm)
        .first()
    )

    if real_statement is not None:
        cell = _get_or_create_cell(db, row_obj.id, sheet_period.id)
        old_value = cell.accrued_value
        new_value = real_statement.total_payment_due
        note = (
            f"creditcard_statements.sync_period: total a pagar del estado de cuenta real de "
            f"'{card.name}' que cerró {billing_label} (estado #{real_statement.id}), pagadero en "
            f"{period_label} (desfase de {lag} mes(es) entre cierre y pago)."
        )
        cell.accrued_value = new_value
        db.add(CellActualEntry(
            cell_id=cell.id,
            actual_value=cell.actual_value,
            accrued_value=cell.accrued_value,
            paid_value=cell.paid_value,
            note=note,
            created_by=acting_user_id,
        ))
        db.flush()
        branch = "real_statement"
    else:
        instant = compute_instant_statement(db, card, period_norm)
        cell = _get_or_create_cell(db, row_obj.id, sheet_period.id)
        new_value = instant.charges_due_this_cycle
        note = (
            f"creditcard_statements.sync_period: proyección de cargos activos de '{card.name}' para "
            f"el ciclo que cierra {billing_label}, pagadero en {period_label} (todavía no hay estado "
            f"de cuenta real registrado para ese ciclo)."
        )
        previous = _supersede_and_write_override(db, cell, new_value, note, acting_user_id)
        old_value = previous.value if previous is not None else None
        branch = "projection"

    return SyncReport(
        card_id=card.id,
        card_name=card.name,
        sheet_id=sheet_obj.id,
        row_id=row_obj.id,
        period=period_norm,
        branch=branch,
        old_value=old_value,
        new_value=new_value,
        note=note,
    )


def sync_period(
    db,
    card: CreditCard,
    sheet: Optional[CashflowSheet],
    row: Optional[SheetRow],
    period: date,
    acting_user_id: int,
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Write `card`'s statement for `period` onto a cashflow sheet cell.

    `sheet`/`row` are optional overrides of the card's own
    mapped_sheet_id/mapped_row_id (pass None to use the card's mapping;
    pass explicit CashflowSheet/SheetRow objects to target something else
    for this one call). Raises ValueError -- never sys.exit -- if neither
    the card's mapping nor explicit sheet/row resolve a target: run
    `creditcard map` first.

    Branch A -- a REAL CreditCardStatement exists for this period: writes
    cell.accrued_value = statement.total_payment_due and appends a
    CellActualEntry snapshot (actual_value/paid_value left exactly as they
    already were -- only accrued_value is being asserted here, same
    independent-fields philosophy as `record set`).

    Branch B -- no real statement yet: calls compute_instant_statement(),
    supersedes any active CellOverride on the cell, and writes a new
    override_type="manual_value" override with
    value=charges_due_this_cycle -- the engine only ever understands
    manual_value/manual_rule/lock (see opencashflow.engine's module
    docstring), so this is never anything else.

    dry_run=True: does everything (via _sync_period_write), then rolls back
    instead of committing -- same contract as
    opencashflow.period_close.close_period.
    """
    report = _sync_period_write(db, card, sheet, row, period, acting_user_id)
    report.dry_run = dry_run

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return report
