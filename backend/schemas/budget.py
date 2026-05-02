from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class BudgetCategoryCreate(BaseModel):
    name: str
    allocated_amount: float = 0.0


class BudgetCategoryUpdate(BaseModel):
    name: Optional[str] = None
    allocated_amount: Optional[float] = None


class BudgetCategoryOut(BaseModel):
    id: int
    budget_id: int
    name: str
    allocated_amount: float
    created_at: datetime

    model_config = {"from_attributes": True}


class BudgetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    period_type: str = "monthly"  # monthly / quarterly / yearly / custom
    start_date: date
    end_date: Optional[date] = None
    currency: str = "USD"
    total_amount: float = 0.0


class BudgetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    period_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    currency: Optional[str] = None
    total_amount: Optional[float] = None


class BudgetOut(BaseModel):
    id: int
    user_id: int
    name: str
    description: Optional[str]
    period_type: str
    start_date: date
    end_date: Optional[date]
    currency: str
    total_amount: float
    created_at: datetime

    model_config = {"from_attributes": True}


class BudgetWithCategoriesOut(BudgetOut):
    categories: List[BudgetCategoryOut] = []
