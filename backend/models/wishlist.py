from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from backend.database import Base


class WishList(Base):
    __tablename__ = "wishlists"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="wishlists")
    items = relationship(
        "WishListItem", back_populates="wishlist", cascade="all, delete-orphan"
    )


class WishListItem(Base):
    __tablename__ = "wishlist_items"

    id = Column(Integer, primary_key=True, index=True)
    wishlist_id = Column(Integer, ForeignKey("wishlists.id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)
    estimated_price = Column(Float, nullable=True)
    # Category allows grouping items within a wish list (e.g. electronics, travel)
    category = Column(String(100), nullable=True)
    # Priority: 1 = highest
    priority = Column(Integer, nullable=False, default=5)
    is_purchased = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    wishlist = relationship("WishList", back_populates="items")
