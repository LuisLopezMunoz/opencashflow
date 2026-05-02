from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class TransactionCreate(BaseModel):
    wallet_id: int
    amount: float
    transaction_type: str  # income / expense / transfer
    category: Optional[str] = None
    description: Optional[str] = None
    transaction_date: Optional[date] = None
    recurrence: str = "unique"  # unique / sporadic / periodic
    period_type: Optional[str] = None  # daily / weekly / monthly / yearly
    next_occurrence: Optional[date] = None


class TransactionUpdate(BaseModel):
    amount: Optional[float] = None
    transaction_type: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    transaction_date: Optional[date] = None
    recurrence: Optional[str] = None
    period_type: Optional[str] = None
    next_occurrence: Optional[date] = None


class TransactionOut(BaseModel):
    id: int
    wallet_id: int
    amount: float
    transaction_type: str
    category: Optional[str]
    description: Optional[str]
    transaction_date: date
    recurrence: str
    period_type: Optional[str]
    next_occurrence: Optional[date]
    created_at: datetime

    model_config = {"from_attributes": True}
