from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    delete,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


def _utcnow_naive():
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=False)  # Telegram user_id
    chat_id = Column(BigInteger, nullable=True)

    username = Column(String(64), nullable=True)  # @username
    first_name = Column(String(64), nullable=True)
    last_name = Column(String(64), nullable=True)

    is_subscribed = Column(Boolean, default=False)
    is_permanent = Column(Boolean, default=False)
    is_super_admin = Column(Boolean, default=False)
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


class SentToken(Base):
    __tablename__ = "sent_tokens"

    mint = Column(String(64), primary_key=True)
    status = Column(String(16), nullable=False, default="processing")
    claimed_at = Column(DateTime, nullable=False, default=_utcnow_naive)
    sent_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<SentToken(mint={self.mint}, status={self.status})>"


def claim_token_mint(session, mint: str, claim_ttl: timedelta = timedelta(minutes=30)) -> bool:
    """Atomically reserve a mint for delivery.

    A completed mint is never reclaimed. A processing claim can be reclaimed after
    the TTL so a crashed worker does not block the mint forever.
    """
    normalized_mint = mint.strip()
    if not normalized_mint:
        return False

    now = _utcnow_naive()
    session.add(SentToken(mint=normalized_mint, status="processing", claimed_at=now))
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()

    stale_before = now - claim_ttl
    result = session.execute(
        update(SentToken)
        .where(
            SentToken.mint == normalized_mint,
            SentToken.status == "processing",
            SentToken.claimed_at < stale_before,
        )
        .values(claimed_at=now)
    )
    session.commit()
    return result.rowcount == 1


def mark_token_sent(session, mint: str) -> None:
    session.execute(
        update(SentToken)
        .where(SentToken.mint == mint.strip(), SentToken.status == "processing")
        .values(status="sent", sent_at=_utcnow_naive())
    )
    session.commit()


def release_token_claim(session, mint: str) -> None:
    session.execute(
        delete(SentToken).where(
            SentToken.mint == mint.strip(),
            SentToken.status == "processing",
        )
    )
    session.commit()
