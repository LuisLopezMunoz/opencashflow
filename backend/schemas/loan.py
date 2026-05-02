from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class BankLoanCreate(BaseModel):
    name: str
    bank: Optional[str] = None
    principal_amount: float
    remaining_balance: float
    interest_rate: float = 0.0
    monthly_payment: float = 0.0
    currency: str = "USD"
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class BankLoanUpdate(BaseModel):
    name: Optional[str] = None
    bank: Optional[str] = None
    remaining_balance: Optional[float] = None
    interest_rate: Optional[float] = None
    monthly_payment: Optional[float] = None
    currency: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class BankLoanOut(BaseModel):
    id: int
    user_id: int
    name: str
    bank: Optional[str]
    principal_amount: float
    remaining_balance: float
    interest_rate: float
    monthly_payment: float
    currency: str
    start_date: Optional[date]
    end_date: Optional[date]
    created_at: datetime

    model_config = {"from_attributes": True}


class LoanPaymentCreate(BaseModel):
    amount: float
    principal_portion: Optional[float] = None
    interest_portion: Optional[float] = None
    payment_date: Optional[date] = None
    notes: Optional[str] = None


class LoanPaymentUpdate(BaseModel):
    amount: Optional[float] = None
    principal_portion: Optional[float] = None
    interest_portion: Optional[float] = None
    payment_date: Optional[date] = None
    notes: Optional[str] = None


class LoanPaymentOut(BaseModel):
    id: int
    loan_id: int
    amount: float
    principal_portion: Optional[float]
    interest_portion: Optional[float]
    payment_date: date
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
