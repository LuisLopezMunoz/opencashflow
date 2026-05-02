from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
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
    CreditCardUpdate,
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
