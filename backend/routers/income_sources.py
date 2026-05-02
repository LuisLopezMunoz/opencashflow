from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.income_source import IncomeSource
from backend.models.user import User
from backend.schemas.income_source import IncomeSourceCreate, IncomeSourceOut, IncomeSourceUpdate

router = APIRouter(prefix="/api/income-sources", tags=["income-sources"])


@router.get("/", response_model=List[IncomeSourceOut])
def list_income_sources(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(IncomeSource).filter(IncomeSource.user_id == current_user.id).all()


@router.post("/", response_model=IncomeSourceOut, status_code=status.HTTP_201_CREATED)
def create_income_source(
    source_in: IncomeSourceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    source = IncomeSource(**source_in.model_dump(), user_id=current_user.id)
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.get("/{source_id}", response_model=IncomeSourceOut)
def get_income_source(
    source_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    source = db.query(IncomeSource).filter(
        IncomeSource.id == source_id, IncomeSource.user_id == current_user.id
    ).first()
    if not source:
        raise HTTPException(status_code=404, detail="Income source not found")
    return source


@router.put("/{source_id}", response_model=IncomeSourceOut)
def update_income_source(
    source_id: int,
    source_update: IncomeSourceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    source = db.query(IncomeSource).filter(
        IncomeSource.id == source_id, IncomeSource.user_id == current_user.id
    ).first()
    if not source:
        raise HTTPException(status_code=404, detail="Income source not found")
    for field, value in source_update.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    db.commit()
    db.refresh(source)
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_income_source(
    source_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    source = db.query(IncomeSource).filter(
        IncomeSource.id == source_id, IncomeSource.user_id == current_user.id
    ).first()
    if not source:
        raise HTTPException(status_code=404, detail="Income source not found")
    db.delete(source)
    db.commit()
