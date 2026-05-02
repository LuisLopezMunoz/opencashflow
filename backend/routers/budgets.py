from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.budget import Budget, BudgetCategory
from backend.models.user import User
from backend.schemas.budget import (
    BudgetCategoryCreate,
    BudgetCategoryOut,
    BudgetCategoryUpdate,
    BudgetCreate,
    BudgetOut,
    BudgetUpdate,
    BudgetWithCategoriesOut,
)

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _get_budget_for_user(budget_id: int, user_id: int, db: Session) -> Budget:
    budget = db.query(Budget).filter(
        Budget.id == budget_id, Budget.user_id == user_id
    ).first()
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget


# --- Budgets ---

@router.get("/", response_model=List[BudgetOut])
def list_budgets(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(Budget).filter(Budget.user_id == current_user.id).all()


@router.post("/", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
def create_budget(
    budget_in: BudgetCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    budget = Budget(**budget_in.model_dump(), user_id=current_user.id)
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budget


@router.get("/{budget_id}", response_model=BudgetWithCategoriesOut)
def get_budget(
    budget_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_budget_for_user(budget_id, current_user.id, db)


@router.put("/{budget_id}", response_model=BudgetOut)
def update_budget(
    budget_id: int,
    budget_update: BudgetUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    budget = _get_budget_for_user(budget_id, current_user.id, db)
    for field, value in budget_update.model_dump(exclude_unset=True).items():
        setattr(budget, field, value)
    db.commit()
    db.refresh(budget)
    return budget


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget(
    budget_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    budget = _get_budget_for_user(budget_id, current_user.id, db)
    db.delete(budget)
    db.commit()


# --- Budget Categories ---

@router.get("/{budget_id}/categories", response_model=List[BudgetCategoryOut])
def list_categories(
    budget_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_budget_for_user(budget_id, current_user.id, db)
    return db.query(BudgetCategory).filter(BudgetCategory.budget_id == budget_id).all()


@router.post(
    "/{budget_id}/categories",
    response_model=BudgetCategoryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_category(
    budget_id: int,
    cat_in: BudgetCategoryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_budget_for_user(budget_id, current_user.id, db)
    cat = BudgetCategory(**cat_in.model_dump(), budget_id=budget_id)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.put("/{budget_id}/categories/{cat_id}", response_model=BudgetCategoryOut)
def update_category(
    budget_id: int,
    cat_id: int,
    cat_update: BudgetCategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_budget_for_user(budget_id, current_user.id, db)
    cat = db.query(BudgetCategory).filter(
        BudgetCategory.id == cat_id, BudgetCategory.budget_id == budget_id
    ).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Budget category not found")
    for field, value in cat_update.model_dump(exclude_unset=True).items():
        setattr(cat, field, value)
    db.commit()
    db.refresh(cat)
    return cat


@router.delete("/{budget_id}/categories/{cat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    budget_id: int,
    cat_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_budget_for_user(budget_id, current_user.id, db)
    cat = db.query(BudgetCategory).filter(
        BudgetCategory.id == cat_id, BudgetCategory.budget_id == budget_id
    ).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Budget category not found")
    db.delete(cat)
    db.commit()
