from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class WalletCreate(BaseModel):
    name: str
    wallet_type: str = "cash"
    currency: str = "USD"
    balance: float = 0.0
    description: Optional[str] = None


class WalletUpdate(BaseModel):
    name: Optional[str] = None
    wallet_type: Optional[str] = None
    currency: Optional[str] = None
    balance: Optional[float] = None
    description: Optional[str] = None


class WalletOut(BaseModel):
    id: int
    user_id: int
    name: str
    wallet_type: str
    currency: str
    balance: float
    description: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
