from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from backend.database import Base


class BankLoan(Base):
    __tablename__ = "bank_loans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    bank = Column(String(100), nullable=True)
    principal_amount = Column(Float, nullable=False)
    remaining_balance = Column(Float, nullable=False)
    interest_rate = Column(Float, nullable=False, default=0.0)  # annual percentage
    monthly_payment = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="USD")
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="loans")
    payments = relationship(
        "LoanPayment", back_populates="loan", cascade="all, delete-orphan"
    )


class LoanPayment(Base):
    __tablename__ = "loan_payments"

    id = Column(Integer, primary_key=True, index=True)
    loan_id = Column(Integer, ForeignKey("bank_loans.id"), nullable=False)
    amount = Column(Float, nullable=False)
    principal_portion = Column(Float, nullable=True)
    interest_portion = Column(Float, nullable=True)
    payment_date = Column(Date, nullable=False, default=date.today)
    notes = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    loan = relationship("BankLoan", back_populates="payments")
