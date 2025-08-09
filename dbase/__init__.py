from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, BigInteger
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=False)  # Telegram user_id
    chat_id = Column(BigInteger, nullable=True)

    username = Column(String(64), nullable=True)  # @username
    first_name = Column(String(64), nullable=True)
    last_name = Column(String(64), nullable=True)

    is_subscribed = Column(Boolean, default=False)
    wallet_address = Column(String(255), default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    subscribed_expiry_at = Column(DateTime, default=None)
    referral_id = Column(BigInteger, default=None)
    def __repr__(self):
        return f"<User(id={self.id}, username={self.username})>"
