"""Write primitives for the basic credit-card model: create/edit a card,
map it to the sheet row its bill lands on, record a purchase (charge), and
record a real bank statement (header + lines). Reusable as-is by any
consumer -- a CLI, an HTTP endpoint, a statement importer -- that needs the
same validation applied the same way.

Extracted out of the consuming app's CLI, where card creation, `charge
add` and `statement add`/`add-line` validated their input inline (and an
importer for bank PDFs shared only the bare row construction), and out of
cli.py (`_do_edit_card`, kept there as an alias; `creditcard map`). Bank-
specific statement parsing stays out of this package on purpose -- see
opencashflow.credit_card's module docstring.

CreditCard.current_balance is never touched here: it's deprecated and read
by nothing (see the model).
"""
import dataclasses
import re
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from opencashflow.credit_card import CreditCard, CreditCardCharge, CreditCardStatement, CreditCardStatementLine
from opencashflow.enums import CREDIT_CARD_STATUSES
from opencashflow.models import CashflowSheet, SheetRow
from opencashflow.unset import UNSET, Unset

__all__ = [
    "UNSET", "STATEMENT_VALUE_FIELDS", "StatementUpsertResult",
    "create_card", "edit_card", "map_card", "add_charge",
    "create_statement", "upsert_statement", "add_statement_line",
]

# Every numeric CreditCardStatement column a statement's `values` may set.
STATEMENT_VALUE_FIELDS = (
    "previous_balance", "payments_received", "new_charges_total", "interest_charged",
    "other_charges", "new_balance", "minimum_payment", "total_payment_due",
    "remaining_principal_installments",
)
_STATEMENT_REQUIRED_FIELDS = ("new_balance", "total_payment_due")

_LAST4 = re.compile(r"^\d{4}$")


def _first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _decimal(value: Any) -> Decimal:
    # Via str(), never Decimal(float) -- that would carry float's binary
    # representation error into a stored money value.
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _check_card_fields(
    *, closing_day: Any = UNSET, due_day: Any = UNSET, last4: Any = UNSET, status: Any = UNSET,
) -> None:
    for field, value in (("closing_day", closing_day), ("due_day", due_day)):
        if isinstance(value, Unset) or value is None:
            continue
        if not 1 <= value <= 31:
            raise ValueError(f"{field} debe estar entre 1 y 31 (recibido: {value}).")
    if not isinstance(last4, Unset) and last4 is not None and not _LAST4.match(last4):
        raise ValueError("last4 debe ser exactamente los últimos 4 dígitos de la tarjeta -- nunca el número completo.")
    if not isinstance(status, Unset) and status not in CREDIT_CARD_STATUSES:
        raise ValueError(f"status debe ser uno de {CREDIT_CARD_STATUSES}, se recibió {status!r}")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def create_card(
    db, user_id: int, name: str, credit_limit: Decimal, *,
    bank: Optional[str] = None, currency: str = "USD",
    closing_day: Optional[int] = None, due_day: Optional[int] = None,
    interest_rate: Decimal = Decimal("0"), minimum_payment_rate: Decimal = Decimal("0.05"),
    last4: Optional[str] = None, expiration_date: Optional[date] = None,
    network: Optional[str] = None, holder_name: Optional[str] = None, status: str = "active",
) -> CreditCard:
    """Create a card owned by `user_id`. Commits. Raises ValueError --
    before writing anything -- for a closing/due day outside 1..31, a last4
    that isn't exactly 4 digits, or an unknown status. `currency` is stored
    upper-cased, `expiration_date` normalized to the 1st of its month."""
    _check_card_fields(closing_day=closing_day, due_day=due_day, last4=last4, status=status)
    card = CreditCard(
        user_id=user_id, name=name, bank=bank, credit_limit=_decimal(credit_limit),
        currency=currency.upper(), closing_day=closing_day, due_day=due_day,
        interest_rate=_decimal(interest_rate), minimum_payment_rate=_decimal(minimum_payment_rate),
        last4=last4, expiration_date=_first_of_month(expiration_date) if expiration_date else None,
        network=network, holder_name=holder_name, status=status,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def edit_card(
    db, card: CreditCard, *,
    name: Union[str, Unset] = UNSET,
    bank: Union[Optional[str], Unset] = UNSET,
    credit_limit: Union[Decimal, Unset] = UNSET,
    closing_day: Union[Optional[int], Unset] = UNSET,
    due_day: Union[Optional[int], Unset] = UNSET,
    interest_rate: Union[Decimal, Unset] = UNSET,
    minimum_payment_rate: Union[Decimal, Unset] = UNSET,
    currency: Union[str, Unset] = UNSET,
    last4: Union[Optional[str], Unset] = UNSET,
    expiration_date: Union[Optional[date], Unset] = UNSET,
    network: Union[Optional[str], Unset] = UNSET,
    holder_name: Union[Optional[str], Unset] = UNSET,
    status: Union[str, Unset] = UNSET,
) -> Dict[str, Tuple[Any, Any]]:
    """Partial update of `card`: only fields actually passed (not left at
    UNSET) are considered, and only ones that actually change something are
    applied; None clears a nullable field. Validated exactly like
    create_card, all before anything is touched. Commits only if something
    changed. Returns {field_name: (old_value, new_value)} for every field
    that changed."""
    _check_card_fields(closing_day=closing_day, due_day=due_day, last4=last4, status=status)

    def money(value):
        return value if isinstance(value, Unset) else _decimal(value)

    requested: Dict[str, Any] = {
        "name": name, "bank": bank, "credit_limit": money(credit_limit),
        "closing_day": closing_day, "due_day": due_day,
        "interest_rate": money(interest_rate), "minimum_payment_rate": money(minimum_payment_rate),
        "currency": currency.upper() if isinstance(currency, str) else currency,
        "last4": last4,
        "expiration_date": _first_of_month(expiration_date) if isinstance(expiration_date, date) else expiration_date,
        "network": network, "holder_name": holder_name, "status": status,
    }
    changes: Dict[str, Tuple[Any, Any]] = {}
    for field, new in requested.items():
        if isinstance(new, Unset):
            continue
        old = getattr(card, field)
        if new != old:
            changes[field] = (old, new)
            setattr(card, field, new)
    if changes:
        db.commit()
        db.refresh(card)
    return changes


def map_card(db, card: CreditCard, sheet: CashflowSheet, row: SheetRow) -> Tuple[Optional[int], Optional[int]]:
    """Point `card` at the sheet row its monthly bill lands on (what
    sync_period writes to when not told otherwise). Raises ValueError if
    `row` isn't a row of `sheet`. Commits. Returns the previous
    (mapped_sheet_id, mapped_row_id)."""
    if row.section.sheet_id != sheet.id:
        raise ValueError(f"La fila '{row.name}' [{row.id}] no pertenece a la planilla #{sheet.id}.")
    previous = (card.mapped_sheet_id, card.mapped_row_id)
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()
    return previous


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------

def add_charge(
    db, card: CreditCard, amount: Decimal, *,
    description: Optional[str] = None, category: Optional[str] = None,
    charge_date: Optional[date] = None, installments: int = 1,
    first_statement_period: Optional[date] = None,
) -> CreditCardCharge:
    """Record a purchase on `card`. `amount` is the WHOLE purchase's total,
    never a per-installment amount (see CreditCardCharge.amount).
    `charge_date` defaults to today; `first_statement_period` (the cycle
    installment #1 lands in -- None leaves the charge out of statement
    projections until it has one) is normalized to the 1st of its month.

    Raises ValueError for a card that isn't active (its existing debt can
    still be synced/paid, but no new purchases), or fewer than 1
    installment. Commits."""
    if card.status != "active":
        raise ValueError(
            f"La tarjeta '{card.name}' tiene estado '{card.status}' -- no se pueden registrar cargos nuevos "
            f"(su deuda ya existente se sigue pudiendo sincronizar/pagar normalmente)."
        )
    if installments < 1:
        raise ValueError(f"installments debe ser >= 1 (recibido: {installments}).")
    charge = CreditCardCharge(
        credit_card_id=card.id, amount=_decimal(amount), description=description, category=category,
        charge_date=charge_date or date.today(), installments=installments, installments_paid=0,
        first_statement_period=_first_of_month(first_statement_period) if first_statement_period else None,
    )
    db.add(charge)
    db.commit()
    db.refresh(charge)
    return charge


# ---------------------------------------------------------------------------
# Real statements
# ---------------------------------------------------------------------------

def _check_values(values: Mapping[str, Any]) -> None:
    unknown = sorted(set(values) - set(STATEMENT_VALUE_FIELDS))
    if unknown:
        raise ValueError(f"Campos de estado de cuenta desconocidos: {unknown}.")


def create_statement(
    db, card: CreditCard, billing_period: date, *, values: Mapping[str, Any], closing_date: date, due_date: date,
    currency: str, source: str, notes: Optional[str] = None, source_file: Optional[str] = None,
) -> CreditCardStatement:
    """Create `card`'s real statement for `billing_period` (normalized to the
    1st of its month). `values` maps STATEMENT_VALUE_FIELDS columns to their
    printed amounts; new_balance and total_payment_due are required. Raises
    ValueError for an unknown or missing field, or an unknown `source`.
    Flushes only -- the caller decides the transaction boundary (an importer
    writing several statements does it all-or-nothing)."""
    _check_values(values)
    missing = [f for f in _STATEMENT_REQUIRED_FIELDS if values.get(f) is None]
    if missing:
        raise ValueError(f"Un estado de cuenta necesita {', '.join(missing)}.")
    statement = CreditCardStatement(
        credit_card_id=card.id, billing_period=_first_of_month(billing_period), currency=currency,
        source=source, source_file=source_file, created_by=card.user_id,
        closing_date=closing_date, due_date=due_date, notes=notes,
        **{field: (None if value is None else _decimal(value)) for field, value in values.items()},
    )
    db.add(statement)
    db.flush()
    return statement


@dataclasses.dataclass
class StatementUpsertResult:
    statement: CreditCardStatement
    created: bool
    changes: Dict[str, Tuple[Any, Any]]  # empty when created, or when nothing changed


def upsert_statement(
    db, card: CreditCard, billing_period: date, *, values: Mapping[str, Any], closing_date: date, due_date: date,
    notes: Union[Optional[str], Unset] = UNSET,
) -> StatementUpsertResult:
    """Record `card`'s statement for `billing_period` by hand: create it
    (source="manual", in the card's currency) if there's none yet, or update
    the existing one in place -- only the `values` keys given, plus the
    dates, plus `notes` if passed. One statement per (card, cycle), so
    re-entering a corrected statement never duplicates it. Commits."""
    period = _first_of_month(billing_period)
    statement = (
        db.query(CreditCardStatement)
        .filter(CreditCardStatement.credit_card_id == card.id, CreditCardStatement.billing_period == period)
        .first()
    )
    if statement is None:
        statement = create_statement(
            db, card, period, values=values, closing_date=closing_date, due_date=due_date,
            currency=card.currency, source="manual", notes=None if isinstance(notes, Unset) else notes,
        )
        db.commit()
        db.refresh(statement)
        return StatementUpsertResult(statement=statement, created=True, changes={})

    _check_values(values)
    requested: Dict[str, Any] = {
        field: (None if value is None else _decimal(value)) for field, value in values.items()
    }
    requested.update(closing_date=closing_date, due_date=due_date)
    if not isinstance(notes, Unset):
        requested["notes"] = notes
    missing = [f for f in _STATEMENT_REQUIRED_FIELDS if f in requested and requested[f] is None]
    if missing:
        raise ValueError(f"Un estado de cuenta necesita {', '.join(missing)}.")

    changes: Dict[str, Tuple[Any, Any]] = {}
    for field, new in requested.items():
        old = getattr(statement, field)
        if new != old:
            changes[field] = (old, new)
            setattr(statement, field, new)
    if changes:
        db.commit()
        db.refresh(statement)
    return StatementUpsertResult(statement=statement, created=False, changes=changes)


def add_statement_line(
    db, statement: CreditCardStatement, *, line_type: str, description: str, transaction_date: date,
    amount: Decimal, installment_number: Optional[int] = None, installment_total: Optional[int] = None,
    charge_id: Optional[int] = None, category: Optional[str] = None,
) -> CreditCardStatementLine:
    """Add one printed line to `statement`. `amount` is THIS line's amount
    (per installment, for a cuota). Raises ValueError -- before adding
    anything -- for an unknown line_type, an installment number without its
    total (or vice versa) or outside 1..total, a charge that doesn't exist
    or belongs to another card, or a second line for the same charge's same
    installment. Adds only -- the caller flushes/commits."""
    if (installment_number is None) != (installment_total is None):
        raise ValueError("installment_number e installment_total van juntos: da los dos o ninguno.")
    if installment_number is not None and installment_total is not None:
        if installment_number < 1 or installment_total < 1 or installment_number > installment_total:
            raise ValueError(
                f"Cuota {installment_number}/{installment_total} inválida: el número debe ser >= 1 y <= el total."
            )
    if charge_id is not None:
        charge = db.get(CreditCardCharge, charge_id)
        if charge is None:
            raise ValueError(f"No existe el cargo #{charge_id}.")
        if charge.credit_card_id != statement.credit_card_id:
            raise ValueError(f"El cargo #{charge_id} no pertenece a la tarjeta de este estado de cuenta.")
        if installment_number is not None:
            dup = (
                db.query(CreditCardStatementLine)
                .filter(
                    CreditCardStatementLine.charge_id == charge_id,
                    CreditCardStatementLine.installment_number == installment_number,
                )
                .first()
            )
            if dup is not None:
                raise ValueError(
                    f"Ya existe una línea real para el cargo #{charge_id} cuota {installment_number} "
                    f"(línea #{dup.id}, estado #{dup.statement_id}) -- no se puede duplicar."
                )
    line = CreditCardStatementLine(
        statement_id=statement.id, charge_id=charge_id, line_type=line_type,
        installment_number=installment_number, installment_total=installment_total,
        description=description, category=category, transaction_date=transaction_date, amount=_decimal(amount),
    )
    db.add(line)
    return line
