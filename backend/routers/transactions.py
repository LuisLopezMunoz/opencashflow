from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.transaction import Transaction
from backend.models.user import User
from backend.models.wallet import Wallet
from backend.schemas.transaction import TransactionCreate, TransactionOut, TransactionUpdate

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _get_wallet_for_user(wallet_id: int, user_id: int, db: Session) -> Wallet:
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == user_id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


@router.get("/", response_model=List[TransactionOut])
def list_transactions(
    wallet_id: Optional[int] = Query(None),
    transaction_type: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_wallet_ids = [
        w.id for w in db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    ]
    query = db.query(Transaction).filter(Transaction.wallet_id.in_(user_wallet_ids))
    if wallet_id is not None:
        if wallet_id not in user_wallet_ids:
            raise HTTPException(status_code=404, detail="Wallet not found")
        query = query.filter(Transaction.wallet_id == wallet_id)
    if transaction_type:
        query = query.filter(Transaction.transaction_type == transaction_type)
    if start_date:
        query = query.filter(Transaction.transaction_date >= start_date)
    if end_date:
        query = query.filter(Transaction.transaction_date <= end_date)
    return query.order_by(Transaction.transaction_date.desc()).all()


@router.post("/", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def create_transaction(
    tx_in: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = _get_wallet_for_user(tx_in.wallet_id, current_user.id, db)
    data = tx_in.model_dump()
    if data.get("transaction_date") is None:
        data["transaction_date"] = date.today()
    tx = Transaction(**data)
    # Update wallet balance
    if tx_in.transaction_type == "income":
        wallet.balance += tx_in.amount
    elif tx_in.transaction_type == "expense":
        wallet.balance -= tx_in.amount
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


@router.get("/{tx_id}", response_model=TransactionOut)
def get_transaction(
    tx_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_wallet_ids = [
        w.id for w in db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    ]
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.wallet_id.in_(user_wallet_ids)
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return tx


@router.put("/{tx_id}", response_model=TransactionOut)
def update_transaction(
    tx_id: int,
    tx_update: TransactionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_wallet_ids = [
        w.id for w in db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    ]
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.wallet_id.in_(user_wallet_ids)
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    for field, value in tx_update.model_dump(exclude_unset=True).items():
        setattr(tx, field, value)
    db.commit()
    db.refresh(tx)
    return tx


@router.delete("/{tx_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(
    tx_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_wallet_ids = [
        w.id for w in db.query(Wallet).filter(Wallet.user_id == current_user.id).all()
    ]
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.wallet_id.in_(user_wallet_ids)
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    wallet = db.query(Wallet).filter(Wallet.id == tx.wallet_id).first()
    if wallet:
        if tx.transaction_type == "income":
            wallet.balance -= tx.amount
        elif tx.transaction_type == "expense":
            wallet.balance += tx.amount
    db.delete(tx)
    db.commit()
