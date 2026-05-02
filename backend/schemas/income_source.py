from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class IncomeSourceCreate(BaseModel):
    name: str
    income_type: str = "salary"
    amount: float
    frequency: str = "monthly"
    currency: str = "USD"
    is_active: bool = True
    description: Optional[str] = None


class IncomeSourceUpdate(BaseModel):
    name: Optional[str] = None
    income_type: Optional[str] = None
    amount: Optional[float] = None
    frequency: Optional[str] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


class IncomeSourceOut(BaseModel):
    id: int
    user_id: int
    name: str
    income_type: str
    amount: float
    frequency: str
    currency: str
    is_active: bool
    description: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
