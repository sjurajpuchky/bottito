import json
import os
import secrets
from pathlib import Path

import dotenv
import requests
from fernet import Fernet
from flask import Flask, request, abort, render_template
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from dbase import Base, Channel

dotenv.load_dotenv()

DATABASE_URL = f"mysql+mysqlconnector://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@{os.getenv('MYSQL_HOST')}/{os.getenv('MYSQL_DATABASE')}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(engine)

# Generate a secure random secret key
secret_key = secrets.token_hex(16)
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__,
            template_folder=str(BASE_DIR / "templates"),  # <= ensure absolute path
            static_folder=str(BASE_DIR / "static"))  # (optional))
app.secret_key = secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

SECRET_KEY = bytes(os.getenv("SECRET_KEY").encode("utf8"))
BOT_IPC_URL = os.getenv("BOT_IPC_URL", "http://127.0.0.1:8787/ipc/post")
IPC_TOKEN = os.getenv("IPC_TOKEN")

fernet = Fernet(SECRET_KEY)

@app.route("/")
def homepage():
    return render_template("homepage.html")


@app.route("/create_promotion")
def create_promotion():
    data_param = request.args.get("data")
    if not data_param:
        abort(400, "Missing data parameter")

    try:
        decrypted_json = fernet.decrypt(data_param.encode()).decode()
        params = json.loads(decrypted_json)
        # Expected keys: mint, caption, logo, links (dict)
        mint = params.get("mint", "")
        user_id = params.get("user_id")
        caption = params.get("caption", "")
        logo = params.get("logo")  # may be None
        links = params.get("links", {}) or {}
        groups = {}

        with SessionLocal() as session:
            selected_channels = session.execute(
                select(Channel)
                .where(Channel.user_id == user_id)
            ).scalars().all()
            for selected_channel in selected_channels:
                groups[selected_channel.chat_id] = selected_channel.chat_name

        return render_template(
            "promote.html",
            mint=mint,
            caption=caption,
            logo=logo,
            links=links,
            groups=groups
        )
    except Exception as e:
        abort(400, f"Failed to decrypt: {e}")



@app.route("/submit_promotion", methods=["POST"])
def submit_promotion():
    form = request.form

    # Expected keys: mint, caption, logo, links (dict)
    mint = form.get("mint")
    logo = form.get("logo")
    caption = form.get("caption").replace("<p>", "").replace("</p>", "")
    chats = form.get("groups")

    chat_ids = [
        key.split("[", 1)[1].rstrip("]")
        for key, val in form.items()
        if key.startswith("groups[") and val == "1"
    ]

    links = {
        key.split("[", 1)[1].rstrip("]"): val
        for key, val in form.items()
        if key.startswith("links[")
    }
    metas = dict()
    metas['logo'] = logo
    metas['links'] = links

    payload = {
        "mint": mint,
        "caption": caption,
        "telegram": links.get("telegram"),  # optional
        "metas": metas,
        "chat_ids": chat_ids,
    }

    print(payload)
    headers = {"Authorization": f"Bearer {IPC_TOKEN}"}
    try:
        r = requests.post(BOT_IPC_URL, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
    except Exception as e:
        abort(502, f"IPC to bot failed: {e}")

    return render_template(
        "promote_done.html",
        mint=mint
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=63325, use_reloader=False)