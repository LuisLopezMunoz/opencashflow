"""Basic credit-card tracking: the card itself, its purchases (charges,
possibly in installments), and real bank statements -- enough to answer
"how much of my credit limit is actually free right now" and to project
"what will this cycle's total-to-pay be" from tracked purchases.

Deliberately NOT here: bank-specific PDF statement parsing, and any
strategy for using available credit to cover a projected deficit (ranking
cards, deciding how much to draw from which). Those stay in a consuming
app on purpose -- they're the genuinely hard-to-replicate, product-shaped
part of a Chilean personal-finance tool; this module is just the same
"credit card" vocabulary any personal-finance app would have.

`user_id`/`created_by` are plain Integers, not ForeignKeys -- same
convention as CashflowSheet.user_id (see models.py): this package takes no
dependency on a host app's own user table or its SQLAlchemy registry.
"""
from datetime import date, datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from opencashflow.models import Base


class CreditCard(Base):
    __tablename__ = "credit_cards"

    id = Column(Integer, primary_key=True, index=True)
    # Plain int, not a ForeignKey -- see module docstring.
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    bank = Column(String(100), nullable=True)
    credit_limit = Column(Float, nullable=False)
    current_balance = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="USD")
    closing_day = Column(Integer, nullable=True)  # day of month when statement closes
    due_day = Column(Integer, nullable=True)      # day of month payment is due
    # Annual interest rate (e.g. 0.24 for 24 %) agreed with the bank (tasa pactada)
    interest_rate = Column(Float, nullable=False, default=0.0)
    # Minimum payment as a fraction of the current balance (e.g. 0.05 for 5 %)
    minimum_payment_rate = Column(Float, nullable=False, default=0.05)
    # Cross-registry reference to THIS SAME package's CashflowSheet.id /
    # SheetRow.id -- plain Integer, not a real FK, since a consuming app is
    # free to create a CreditCard before ever mapping it to a sheet row.
    # This is the sheet cell this card's monthly "total a pagar" gets
    # written onto -- set by `creditcard map`; sync_period() falls back to
    # these when the caller doesn't pass an explicit sheet/row of its own.
    mapped_sheet_id = Column(Integer, nullable=True)
    mapped_row_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Physical-card metadata -- purely descriptive except `status`, which has
    # real behavior attached in a consuming app (e.g. a card that isn't
    # "active" can still be synced/paid off, but is rejected for new
    # charges and excluded from any deficit-covering draw ranking).
    # Never store more than the last 4 digits or a full PAN/CVV -- this is a
    # personal finance tool, not a wallet; last4 is enough to recognize a
    # card in a bank statement.
    last4 = Column(String(4), nullable=True)
    # Month/year printed on the card -- day-normalized to the 1st, same
    # convention as CreditCardCharge.first_statement_period /
    # CreditCardStatement.billing_period (only the month/year is real).
    expiration_date = Column(Date, nullable=True)
    # Card network (Visa/Mastercard/Amex/...) -- free text, never a fixed
    # choices= list: not this package's job to keep up with every network.
    network = Column(String(20), nullable=True)
    holder_name = Column(String(100), nullable=True)
    # active | expired | blocked | cancelled -- validated in application
    # code, not a DB CHECK constraint, same convention as
    # CreditCardStatement.source / StatementLine.line_type below.
    status = Column(String(20), nullable=False, default="active")

    charges = relationship(
        "CreditCardCharge", back_populates="credit_card", cascade="all, delete-orphan"
    )
    statements = relationship(
        "CreditCardStatement", back_populates="credit_card", cascade="all, delete-orphan"
    )


class CreditCardCharge(Base):
    __tablename__ = "credit_card_charges"

    id = Column(Integer, primary_key=True, index=True)
    credit_card_id = Column(Integer, ForeignKey("credit_cards.id"), nullable=False)
    # The WHOLE PURCHASE'S TOTAL, never a per-installment amount -- e.g. a
    # $1,200,000 purchase in 12 installments is stored as amount=1_200_000,
    # installments=12, never as the ~100k/month per-cuota figure. Contrast
    # with CreditCardStatementLine.amount below, which IS the per-line
    # (per-cuota) amount -- never confuse the two.
    # Numeric, not Float, on purpose (unlike every other money field on
    # CreditCard/CreditCardCharge, which predate the statement-tracking
    # feature and stay Float to avoid an unrelated migration): this value
    # feeds compute_instant_statement's Decimal(str(charge.amount)) call and,
    # from there, installment_schedule -> SheetCell, which are Numeric/Decimal
    # throughout the core engine.
    amount = Column(Numeric(14, 2), nullable=False)
    description = Column(String(255), nullable=True)
    category = Column(String(100), nullable=True)
    charge_date = Column(Date, nullable=False, default=date.today)
    # Installments (cuotas): total number of installments (1 = single charge)
    installments = Column(Integer, nullable=False, default=1)
    # Number of installments already paid
    installments_paid = Column(Integer, nullable=False, default=0)
    # Which billing cycle installment #1 lands in -- day-normalized to the
    # 1st of the month, same convention as CreditCardStatement.billing_period
    # below. Nullable: a charge not (yet) tracked for statement projection
    # simply has no first_statement_period and is invisible to
    # compute_instant_statement()'s projection.
    first_statement_period = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    credit_card = relationship("CreditCard", back_populates="charges")
    statement_lines = relationship("CreditCardStatementLine", back_populates="charge")


class CreditCardStatement(Base):
    """A REAL bank statement (cartola) for one billing cycle of one credit
    card.

    No partial/in-progress statement state: a statement either exists here
    as a complete row (source="manual" or "pdf_import") or it doesn't exist
    at all yet for that (card, period) -- in which case
    compute_instant_statement() builds a purely projected view from
    CreditCardCharge instead of reading this table.

    `previous_balance`/`payments_received`/`interest_charged`/`other_charges`
    are the numbers AS PRINTED on the real statement. `new_charges_total` is
    kept separate from the sum of this statement's own `lines`
    (CreditCardStatementLine) on purpose, so a mismatch between "what the
    bank says the total new charges are" and "what our own line items sum
    to" is detectable later, never silently reconciled.
    """

    __tablename__ = "credit_card_statements"
    __table_args__ = (UniqueConstraint("credit_card_id", "billing_period", name="uq_statement_period"),)

    id = Column(Integer, primary_key=True, index=True)
    credit_card_id = Column(Integer, ForeignKey("credit_cards.id"), nullable=False)
    # Cycle IDENTIFIER for lookups -- day-normalized to the 1st of the
    # month, same convention as opencashflow.models.SheetPeriod.period_date.
    # Distinct from closing_date/due_date below, which are the REAL calendar
    # dates printed on the statement.
    billing_period = Column(Date, nullable=False)

    previous_balance = Column(Numeric(14, 2), nullable=True)
    payments_received = Column(Numeric(14, 2), nullable=True)
    # The bank's OWN printed total of new charges this cycle -- kept
    # separate from summing this statement's `lines` so a mismatch between
    # the two is detectable later, never silently reconciled.
    new_charges_total = Column(Numeric(14, 2), nullable=True)
    interest_charged = Column(Numeric(14, 2), nullable=True)
    other_charges = Column(Numeric(14, 2), nullable=True)
    # The two numbers this whole feature exists to capture -- required.
    new_balance = Column(Numeric(14, 2), nullable=False)
    minimum_payment = Column(Numeric(14, 2), nullable=True)
    # The number that feeds a SheetRow (see sync_period).
    total_payment_due = Column(Numeric(14, 2), nullable=False)
    # "SALDO CAPITAL CUOTAS" as printed by the bank -- total remaining
    # PRINCIPAL (excluding future interest) across every active installment
    # plan on this card, as of this statement's closing date. An aggregate
    # only: the bank does not break it out per purchase, so this is never
    # decomposable onto a specific CreditCardCharge/CreditCardStatementLine
    # -- see cupo_disponible(), the sole reader, which needs exactly one
    # total per card per cycle, nothing finer.
    # Nullable: not every bank/month prints it; its absence must never block
    # recording a statement -- cupo_disponible() falls back to its own
    # installment_schedule-based estimate when this is None.
    remaining_principal_installments = Column(Numeric(14, 2), nullable=True)

    closing_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    # "manual" | "pdf_import" -- enforced in application code, not a DB
    # CHECK constraint, matching how row_type/sign/override_type are
    # validated elsewhere in this package.
    source = Column(String(20), nullable=False)
    source_file = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    # Plain int, not a ForeignKey -- see module docstring.
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    credit_card = relationship("CreditCard", back_populates="statements")
    lines = relationship(
        "CreditCardStatementLine", back_populates="statement", cascade="all, delete-orphan"
    )


class CreditCardStatementLine(Base):
    """One printed line item of a real CreditCardStatement -- a charge,
    payment, credit, interest charge, or fee.

    `amount` is always the amount of THIS LINE as printed on the statement
    (the per-cuota amount for an installment line, never the whole
    purchase's total) -- this is the resolution to the ambiguity
    CreditCardCharge.amount already has; see that column's own comment.
    """

    __tablename__ = "credit_card_statement_lines"
    __table_args__ = (UniqueConstraint("charge_id", "installment_number", name="uq_charge_installment"),)

    id = Column(Integer, primary_key=True, index=True)
    statement_id = Column(Integer, ForeignKey("credit_card_statements.id"), nullable=False)
    # Populated only when this line is one installment of a tracked
    # multi-cuota CreditCardCharge -- nullable for a line the bank prints
    # that isn't (yet, or ever) linked back to a charge being tracked.
    charge_id = Column(Integer, ForeignKey("credit_card_charges.id"), nullable=True)
    # "charge" | "payment" | "credit" | "interest" | "fee"
    line_type = Column(String(20), nullable=False)
    # NULL for a non-installment line; 1/1 for a genuine single-payment
    # charge, for uniformity with a real multi-cuota charge's lines.
    installment_number = Column(Integer, nullable=True)
    installment_total = Column(Integer, nullable=True)
    description = Column(String, nullable=False)
    category = Column(String, nullable=True)
    # The ORIGINAL purchase date as printed on the line -- for cuota 3 of 12
    # this is NOT this statement's billing cycle date, it repeats every
    # month for the same purchase.
    transaction_date = Column(Date, nullable=False)
    # The amount of THIS LINE as printed -- see class docstring.
    amount = Column(Numeric(14, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    statement = relationship("CreditCardStatement", back_populates="lines")
    charge = relationship("CreditCardCharge", back_populates="statement_lines")
