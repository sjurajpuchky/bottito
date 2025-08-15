import base64
import json
import os
import secrets

import dotenv
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

app = Flask(__name__)
app.secret_key = secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

SECRET_KEY = bytes(os.getenv("SECRET_KEY").encode("utf8"))
fernet = Fernet(SECRET_KEY)

@app.route("/create_promotion")
def create_promotion():
    data_param = request.args.get("data")
    if not data_param:
        abort(400, "Missing data parameter")

    try:
        decrypted_json = fernet.decrypt(data_param.encode()).decode()
        params = json.loads(decrypted_json)
    except Exception as e:
        abort(400, f"Failed to decrypt: {e}")

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



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=63325, use_reloader=False)