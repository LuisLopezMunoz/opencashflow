from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.credit_card import CreditCard
from backend.models.income_source import IncomeSource
from backend.models.loan import BankLoan
from backend.models.transaction import Transaction
from backend.models.user import User
from backend.models.wallet import Wallet

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("/")
def get_summary(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    wallets = db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    wallet_ids = [w.id for w in wallets]

    total_wallet_balance = sum(w.balance for w in wallets)

    credit_cards = db.query(CreditCard).filter(CreditCard.user_id == current_user.id).all()
    total_credit_balance = sum(c.current_balance for c in credit_cards)
    total_credit_limit = sum(c.credit_limit for c in credit_cards)

    loans = db.query(BankLoan).filter(BankLoan.user_id == current_user.id).all()
    total_loan_balance = sum(lo.remaining_balance for lo in loans)
    total_monthly_loan_payment = sum(lo.monthly_payment for lo in loans)

    income_sources = (
        db.query(IncomeSource)
        .filter(IncomeSource.user_id == current_user.id, IncomeSource.is_active == True)
        .all()
    )
    total_monthly_income = 0.0
    for src in income_sources:
        if src.frequency == "weekly":
            total_monthly_income += src.amount * 4.33
        elif src.frequency == "biweekly":
            total_monthly_income += src.amount * 2.17
        elif src.frequency == "monthly":
            total_monthly_income += src.amount
        elif src.frequency == "yearly":
            total_monthly_income += src.amount / 12

    return {
        "total_wallet_balance": round(total_wallet_balance, 2),
        "wallets_count": len(wallets),
        "total_credit_card_balance": round(total_credit_balance, 2),
        "total_credit_limit": round(total_credit_limit, 2),
        "credit_cards_count": len(credit_cards),
        "total_loan_balance": round(total_loan_balance, 2),
        "total_monthly_loan_payment": round(total_monthly_loan_payment, 2),
        "loans_count": len(loans),
        "total_monthly_income": round(total_monthly_income, 2),
        "active_income_sources": len(income_sources),
        "net_monthly_cash_flow": round(
            total_monthly_income - total_monthly_loan_payment, 2
        ),
    }
