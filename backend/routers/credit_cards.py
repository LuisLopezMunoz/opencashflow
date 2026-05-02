from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.credit_card import CreditCard, CreditCardCharge
from backend.models.user import User
from backend.schemas.credit_card import (
    CreditCardChargeCreate,
    CreditCardChargeOut,
    CreditCardChargeUpdate,
    CreditCardCreate,
    CreditCardOut,
    CreditCardProjectionOut,
    CreditCardUpdate,
    PaymentProjectionMonth,
)

router = APIRouter(prefix="/api/credit-cards", tags=["credit-cards"])


@router.get("/", response_model=List[CreditCardOut])
def list_credit_cards(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(CreditCard).filter(CreditCard.user_id == current_user.id).all()


@router.post("/", response_model=CreditCardOut, status_code=status.HTTP_201_CREATED)
def create_credit_card(
    card_in: CreditCardCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = CreditCard(**card_in.model_dump(), user_id=current_user.id)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


@router.get("/{card_id}", response_model=CreditCardOut)
def get_credit_card(
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    return card


@router.put("/{card_id}", response_model=CreditCardOut)
def update_credit_card(
    card_id: int,
    card_update: CreditCardUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    for field, value in card_update.model_dump(exclude_unset=True).items():
        setattr(card, field, value)
    db.commit()
    db.refresh(card)
    return card


@router.delete("/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credit_card(
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    db.delete(card)
    db.commit()


# --- Charges ---

@router.get("/{card_id}/charges", response_model=List[CreditCardChargeOut])
def list_charges(
    card_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    return db.query(CreditCardCharge).filter(CreditCardCharge.credit_card_id == card_id).all()


@router.post(
    "/{card_id}/charges",
    response_model=CreditCardChargeOut,
    status_code=status.HTTP_201_CREATED,
)
def create_charge(
    card_id: int,
    charge_in: CreditCardChargeCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    data = charge_in.model_dump()
    if data.get("charge_date") is None:
        data["charge_date"] = date.today()
    charge = CreditCardCharge(**data, credit_card_id=card_id)
    card.current_balance += charge_in.amount
    db.add(charge)
    db.commit()
    db.refresh(charge)
    return charge


@router.put("/{card_id}/charges/{charge_id}", response_model=CreditCardChargeOut)
def update_charge(
    card_id: int,
    charge_id: int,
    charge_update: CreditCardChargeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    charge = db.query(CreditCardCharge).filter(
        CreditCardCharge.id == charge_id, CreditCardCharge.credit_card_id == card_id
    ).first()
    if not charge:
        raise HTTPException(status_code=404, detail="Charge not found")
    updates = charge_update.model_dump(exclude_unset=True)
    if "amount" in updates:
        card.current_balance = card.current_balance - charge.amount + updates["amount"]
    for field, value in updates.items():
        setattr(charge, field, value)
    db.commit()
    db.refresh(charge)
    return charge


@router.delete("/{card_id}/charges/{charge_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_charge(
    card_id: int,
    charge_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")
    charge = db.query(CreditCardCharge).filter(
        CreditCardCharge.id == charge_id, CreditCardCharge.credit_card_id == card_id
    ).first()
    if not charge:
        raise HTTPException(status_code=404, detail="Charge not found")
    card.current_balance -= charge.amount
    db.delete(charge)
    db.commit()


# --- Payment projection ---

def _add_months(d: date, n: int) -> date:
    """Return a date n months after d."""
    import calendar
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


@router.get("/{card_id}/projection", response_model=CreditCardProjectionOut)
def get_payment_projection(
    card_id: int,
    months: int = Query(default=12, ge=1, le=60),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Project month-by-month balance, interest, and minimum payment for a credit card."""
    card = db.query(CreditCard).filter(
        CreditCard.id == card_id, CreditCard.user_id == current_user.id
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Credit card not found")

    monthly_rate = card.interest_rate / 12
    balance = card.current_balance
    today = date.today()
    projection: List[PaymentProjectionMonth] = []

    for i in range(months):
        month_start = _add_months(today.replace(day=1), i)
        month_label = month_start.strftime("%Y-%m")
        opening = round(balance, 2)

        # Charges due this month from active installment charges
        charges_due = 0.0
        active_charges = (
            db.query(CreditCardCharge)
            .filter(
                CreditCardCharge.credit_card_id == card_id,
                CreditCardCharge.installments > 1,
                CreditCardCharge.installments_paid < CreditCardCharge.installments,
            )
            .all()
        )
        for ch in active_charges:
            remaining = ch.installments - ch.installments_paid
            if remaining > 0:
                charges_due += ch.amount / ch.installments

        interest = round(balance * monthly_rate, 2)
        minimum = round(balance * card.minimum_payment_rate, 2) if balance > 0 else 0.0
        # Closing balance: add interest, subtract minimum payment
        balance = max(0.0, round(balance + interest - minimum, 2))

        projection.append(
            PaymentProjectionMonth(
                month=month_label,
                opening_balance=opening,
                charges_due=round(charges_due, 2),
                interest=interest,
                minimum_payment=minimum,
                closing_balance=balance,
            )
        )

    return CreditCardProjectionOut(
        card_id=card.id,
        card_name=card.name,
        current_balance=round(card.current_balance, 2),
        interest_rate=card.interest_rate,
        months=projection,
    )
