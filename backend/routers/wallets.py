from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.user import User
from backend.models.wallet import Wallet
from backend.schemas.wallet import WalletCreate, WalletOut, WalletUpdate

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


@router.get("/", response_model=List[WalletOut])
def list_wallets(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(Wallet).filter(Wallet.user_id == current_user.id).all()


@router.post("/", response_model=WalletOut, status_code=status.HTTP_201_CREATED)
def create_wallet(
    wallet_in: WalletCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = Wallet(**wallet_in.model_dump(), user_id=current_user.id)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


@router.get("/{wallet_id}", response_model=WalletOut)
def get_wallet(
    wallet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == current_user.id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


@router.put("/{wallet_id}", response_model=WalletOut)
def update_wallet(
    wallet_id: int,
    wallet_update: WalletUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == current_user.id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    for field, value in wallet_update.model_dump(exclude_unset=True).items():
        setattr(wallet, field, value)
    db.commit()
    db.refresh(wallet)
    return wallet


@router.delete("/{wallet_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wallet(
    wallet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == current_user.id
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    db.delete(wallet)
    db.commit()
