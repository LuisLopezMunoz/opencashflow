"""Cash flow projection endpoint.

Projects income and expenses month by month for a given number of months,
combining:
- Periodic transactions (recurrence=periodic)
- Active income sources
- Recurring loan payments
- Minimum credit card payments (interest + minimum payment rate)
"""
from datetime import date
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.credit_card import CreditCard
from backend.models.income_source import IncomeSource
from backend.models.loan import BankLoan
from backend.models.transaction import Transaction
from backend.models.user import User
from backend.models.wallet import Wallet
from pydantic import BaseModel

router = APIRouter(prefix="/api/cash-flow", tags=["cash-flow"])


class CashFlowMonth(BaseModel):
    month: str  # YYYY-MM
    income: float
    expenses: float
    loan_payments: float
    credit_card_payments: float
    net: float


class CashFlowProjectionOut(BaseModel):
    months: List[CashFlowMonth]
    total_income: float
    total_expenses: float
    total_loan_payments: float
    total_credit_card_payments: float
    total_net: float


def _add_months(d: date, n: int) -> date:
    """Return a date that is n months after d (clamped to month-end if needed)."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _monthly_income_from_source(src: IncomeSource) -> float:
    """Convert an income source to its monthly equivalent."""
    if src.frequency == "weekly":
        return src.amount * 4.33
    if src.frequency == "biweekly":
        return src.amount * 2.17
    if src.frequency == "monthly":
        return src.amount
    if src.frequency == "yearly":
        return src.amount / 12
    return src.amount


def _periodic_tx_monthly(tx: Transaction) -> float:
    """Return monthly equivalent amount for a periodic transaction (positive = income)."""
    sign = 1.0 if tx.transaction_type == "income" else -1.0
    if tx.period_type == "daily":
        return tx.amount * sign * 30
    if tx.period_type == "weekly":
        return tx.amount * sign * 4.33
    if tx.period_type == "monthly":
        return tx.amount * sign
    if tx.period_type == "yearly":
        return tx.amount * sign / 12
    return tx.amount * sign


@router.get("/projection", response_model=CashFlowProjectionOut)
def get_cash_flow_projection(
    months: int = Query(default=12, ge=1, le=60),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = date.today()

    # --- Periodic transactions ---
    wallet_ids = [
        w.id
        for w in db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    ]
    periodic_txs = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id.in_(wallet_ids),
            Transaction.recurrence == "periodic",
        )
        .all()
    )

    # --- Active income sources ---
    income_sources = (
        db.query(IncomeSource)
        .filter(
            IncomeSource.user_id == current_user.id,
            IncomeSource.is_active.is_(True),
        )
        .all()
    )

    # --- Loans ---
    loans = db.query(BankLoan).filter(BankLoan.user_id == current_user.id).all()

    # --- Credit cards ---
    credit_cards = (
        db.query(CreditCard).filter(CreditCard.user_id == current_user.id).all()
    )

    result_months: List[CashFlowMonth] = []

    for i in range(months):
        month_start = _add_months(today.replace(day=1), i)
        month_end = _add_months(today.replace(day=1), i + 1)
        month_label = month_start.strftime("%Y-%m")

        # Income from periodic transactions
        periodic_income = sum(
            max(_periodic_tx_monthly(tx), 0) for tx in periodic_txs
        )
        periodic_expenses = sum(
            abs(min(_periodic_tx_monthly(tx), 0)) for tx in periodic_txs
        )

        # Income from income sources
        source_income = sum(_monthly_income_from_source(s) for s in income_sources)

        total_income = round(periodic_income + source_income, 2)
        total_expenses = round(periodic_expenses, 2)

        # Loan payments (only while loan is active)
        loan_payment = 0.0
        for loan in loans:
            if loan.end_date and loan.end_date < month_start:
                continue
            if loan.start_date and loan.start_date >= month_end:
                continue
            loan_payment += loan.monthly_payment
        loan_payment = round(loan_payment, 2)

        # Credit card minimum payments + interest
        cc_payment = 0.0
        for card in credit_cards:
            if card.current_balance <= 0:
                continue
            monthly_rate = card.interest_rate / 12
            interest = round(card.current_balance * monthly_rate, 2)
            minimum = round(card.current_balance * card.minimum_payment_rate, 2)
            cc_payment += interest + minimum
        cc_payment = round(cc_payment, 2)

        net = round(total_income - total_expenses - loan_payment - cc_payment, 2)

        result_months.append(
            CashFlowMonth(
                month=month_label,
                income=total_income,
                expenses=total_expenses,
                loan_payments=loan_payment,
                credit_card_payments=cc_payment,
                net=net,
            )
        )

    total_income = round(sum(m.income for m in result_months), 2)
    total_expenses = round(sum(m.expenses for m in result_months), 2)
    total_loan = round(sum(m.loan_payments for m in result_months), 2)
    total_cc = round(sum(m.credit_card_payments for m in result_months), 2)
    total_net = round(sum(m.net for m in result_months), 2)

    return CashFlowProjectionOut(
        months=result_months,
        total_income=total_income,
        total_expenses=total_expenses,
        total_loan_payments=total_loan,
        total_credit_card_payments=total_cc,
        total_net=total_net,
    )
