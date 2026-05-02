from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from backend.database import Base


class CreditCard(Base):
    __tablename__ = "credit_cards"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    bank = Column(String(100), nullable=True)
    credit_limit = Column(Float, nullable=False)
    current_balance = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="USD")
    closing_day = Column(Integer, nullable=True)  # day of month when statement closes
    due_day = Column(Integer, nullable=True)      # day of month payment is due
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="credit_cards")
    charges = relationship(
        "CreditCardCharge", back_populates="credit_card", cascade="all, delete-orphan"
    )


class CreditCardCharge(Base):
    __tablename__ = "credit_card_charges"

    id = Column(Integer, primary_key=True, index=True)
    credit_card_id = Column(Integer, ForeignKey("credit_cards.id"), nullable=False)
    amount = Column(Float, nullable=False)
    description = Column(String(255), nullable=True)
    category = Column(String(100), nullable=True)
    charge_date = Column(Date, nullable=False, default=date.today)
    created_at = Column(DateTime, default=datetime.utcnow)

    credit_card = relationship("CreditCard", back_populates="charges")
