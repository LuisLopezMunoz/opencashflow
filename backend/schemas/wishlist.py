from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class WishListCreate(BaseModel):
    name: str
    description: Optional[str] = None


class WishListUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class WishListOut(BaseModel):
    id: int
    user_id: int
    name: str
    description: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class WishListItemCreate(BaseModel):
    name: str
    description: Optional[str] = None
    estimated_price: Optional[float] = None
    category: Optional[str] = None
    priority: int = 5
    is_purchased: bool = False


class WishListItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    estimated_price: Optional[float] = None
    category: Optional[str] = None
    priority: Optional[int] = None
    is_purchased: Optional[bool] = None


class WishListItemOut(BaseModel):
    id: int
    wishlist_id: int
    name: str
    description: Optional[str]
    estimated_price: Optional[float]
    category: Optional[str]
    priority: int
    is_purchased: bool
    created_at: datetime

    model_config = {"from_attributes": True}
