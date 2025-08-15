from datetime import datetime

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, BigInteger, PrimaryKeyConstraint
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=False)  # Telegram user_id
    chat_id = Column(BigInteger, nullable=True)

    username = Column(String(64), nullable=True)  # @username
    first_name = Column(String(64), nullable=True)
    last_name = Column(String(64), nullable=True)

    is_subscribed = Column(Boolean, default=False)
    is_permanent = Column(Boolean, default=False)
    wallet_address = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    subscribed_expiry_at = Column(DateTime, default=None)
    referral_id = Column(BigInteger, default=None)
    # Relationship to transactions
    transactions = relationship("TransactionSignature", back_populates="user", cascade="all, delete-orphan")
    referral_payments = relationship("ReferralPayment", back_populates="user", cascade="all, delete-orphan")
    channels = relationship("Channel", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username})>"


class TransactionSignature(Base):
    __tablename__ = "transaction_signatures"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Solana transaction signature
    signature = Column(String(128), nullable=False, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")

    def __repr__(self):
        return f"<TransactionSignature(user_id={self.user_id}, signature={self.signature})>"


class ReferralPayment(Base):
    __tablename__ = "referral_payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Optional: link this payment to the referred user's id
    referred_user_id = Column(BigInteger, nullable=True)

    amount_lamports = Column(BigInteger, nullable=True)

    payment_datetime = Column(DateTime, default=None)

    user = relationship("User", back_populates="referral_payments")

    def __repr__(self):
        return (
            f"<ReferralPayment(user_id={self.user_id}, "
            f"referred_user_id={self.referred_user_id}, "
            f"amount_lamports={self.amount_lamports}, datetime={self.payment_datetime})>"
        )


class Channel(Base):
    __tablename__ = "channels"

    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    chat_name = Column(String(250), nullable=True)

    # Composite primary key
    __table_args__ = (
        PrimaryKeyConstraint('user_id', 'chat_id'),
    )

    user = relationship("User", back_populates="channels")

    def __repr__(self):
        return f"<Channel(user_id={self.user_id}, chat_id={self.chat_id})>"
