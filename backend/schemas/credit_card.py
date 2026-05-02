from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class CreditCardCreate(BaseModel):
    name: str
    bank: Optional[str] = None
    credit_limit: float
    current_balance: float = 0.0
    currency: str = "USD"
    closing_day: Optional[int] = None
    due_day: Optional[int] = None


class CreditCardUpdate(BaseModel):
    name: Optional[str] = None
    bank: Optional[str] = None
    credit_limit: Optional[float] = None
    current_balance: Optional[float] = None
    currency: Optional[str] = None
    closing_day: Optional[int] = None
    due_day: Optional[int] = None


class CreditCardOut(BaseModel):
    id: int
    user_id: int
    name: str
    bank: Optional[str]
    credit_limit: float
    current_balance: float
    currency: str
    closing_day: Optional[int]
    due_day: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class CreditCardChargeCreate(BaseModel):
    amount: float
    description: Optional[str] = None
    category: Optional[str] = None
    charge_date: Optional[date] = None


class CreditCardChargeUpdate(BaseModel):
    amount: Optional[float] = None
    description: Optional[str] = None
    category: Optional[str] = None
    charge_date: Optional[date] = None


class CreditCardChargeOut(BaseModel):
    id: int
    credit_card_id: int
    amount: float
    description: Optional[str]
    category: Optional[str]
    charge_date: date
    created_at: datetime

    model_config = {"from_attributes": True}
