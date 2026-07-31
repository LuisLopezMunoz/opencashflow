from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from backend.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    wallets = relationship("Wallet", back_populates="owner", cascade="all, delete-orphan")
    credit_cards = relationship("CreditCard", back_populates="owner", cascade="all, delete-orphan")
    loans = relationship("BankLoan", back_populates="owner", cascade="all, delete-orphan")
    income_sources = relationship("IncomeSource", back_populates="owner", cascade="all, delete-orphan")
    wishlists = relationship("WishList", back_populates="owner", cascade="all, delete-orphan")
    budgets = relationship("Budget", back_populates="owner", cascade="all, delete-orphan")
    cashflow_sheets = relationship("CashflowSheet", back_populates="owner", cascade="all, delete-orphan")
