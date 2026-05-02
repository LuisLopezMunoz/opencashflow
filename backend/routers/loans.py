from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.loan import BankLoan, LoanPayment
from backend.models.user import User
from backend.schemas.loan import (
    BankLoanCreate,
    BankLoanOut,
    BankLoanUpdate,
    LoanPaymentCreate,
    LoanPaymentOut,
    LoanPaymentUpdate,
)

router = APIRouter(prefix="/api/loans", tags=["loans"])


@router.get("/", response_model=List[BankLoanOut])
def list_loans(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(BankLoan).filter(BankLoan.user_id == current_user.id).all()


@router.post("/", response_model=BankLoanOut, status_code=status.HTTP_201_CREATED)
def create_loan(
    loan_in: BankLoanCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = BankLoan(**loan_in.model_dump(), user_id=current_user.id)
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


@router.get("/{loan_id}", response_model=BankLoanOut)
def get_loan(
    loan_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


@router.put("/{loan_id}", response_model=BankLoanOut)
def update_loan(
    loan_id: int,
    loan_update: BankLoanUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    for field, value in loan_update.model_dump(exclude_unset=True).items():
        setattr(loan, field, value)
    db.commit()
    db.refresh(loan)
    return loan


@router.delete("/{loan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_loan(
    loan_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    db.delete(loan)
    db.commit()


# --- Payments ---

@router.get("/{loan_id}/payments", response_model=List[LoanPaymentOut])
def list_payments(
    loan_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    return db.query(LoanPayment).filter(LoanPayment.loan_id == loan_id).all()


@router.post(
    "/{loan_id}/payments",
    response_model=LoanPaymentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    loan_id: int,
    payment_in: LoanPaymentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    data = payment_in.model_dump()
    if data.get("payment_date") is None:
        data["payment_date"] = date.today()
    payment = LoanPayment(**data, loan_id=loan_id)
    principal = payment_in.principal_portion or payment_in.amount
    loan.remaining_balance = max(0.0, loan.remaining_balance - principal)
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


@router.put("/{loan_id}/payments/{payment_id}", response_model=LoanPaymentOut)
def update_payment(
    loan_id: int,
    payment_id: int,
    payment_update: LoanPaymentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    payment = db.query(LoanPayment).filter(
        LoanPayment.id == payment_id, LoanPayment.loan_id == loan_id
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    for field, value in payment_update.model_dump(exclude_unset=True).items():
        setattr(payment, field, value)
    db.commit()
    db.refresh(payment)
    return payment


@router.delete("/{loan_id}/payments/{payment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_payment(
    loan_id: int,
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    loan = db.query(BankLoan).filter(
        BankLoan.id == loan_id, BankLoan.user_id == current_user.id
    ).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    payment = db.query(LoanPayment).filter(
        LoanPayment.id == payment_id, LoanPayment.loan_id == loan_id
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    principal = payment.principal_portion or payment.amount
    loan.remaining_balance += principal
    db.delete(payment)
    db.commit()
