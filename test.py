import datetime
import os

import dotenv
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from telegram.ext import (
    Application,
)

from dbase import Base, User

dotenv.load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot_app = Application.builder().token(BOT_TOKEN).build()

DATABASE_URL = f"mysql+mysqlconnector://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@{os.getenv('MYSQL_HOST')}/{os.getenv('MYSQL_DATABASE')}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(engine)


def _now():
    return datetime.datetime.now(datetime.UTC)


with SessionLocal() as session:
    trial_users = session.execute(
        select(User)
        .where(User.is_subscribed == False)
    ).scalars().all()

    subscribed_users = session.execute(
        select(User)
        .where(User.is_subscribed == True)
        .where(User.subscribed_expiry_at >= _now())
    ).scalars().all()

    for user in trial_users + subscribed_users:
        print(f"Sending to {user.chat_id}")
