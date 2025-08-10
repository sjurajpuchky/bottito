import asyncio
import datetime
import json
import logging
import os
import re
from _decimal import Decimal

import dotenv
import requests
import websockets
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from sqlalchemy import create_engine
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker, Session
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram._botcommand import BotCommand
from telegram._botcommandscope import BotCommandScopeDefault
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler
from telegram.ext import CommandHandler, ChatMemberHandler
from telegram.ext import (
    ContextTypes,
    MessageHandler,
    filters,
    Application,
)
from websockets import ConnectionClosedError, WebSocketException

from dbase import Base, User, TransactionSignature, ReferralPayment

# -------------------------------
# ✅ Logging Configuration
# -------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("bottito")

# -------------------------------
# ✅ Load .env and setup
# -------------------------------
if not dotenv.load_dotenv():
    logger.warning(".env file not found or could not be loaded.")

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")  # Set this in your environment
HEADERS = {
    "accept": "application/json",
    "X-API-Key": MORALIS_API_KEY,
}

BOT_TOKEN = os.getenv("BOT_TOKEN")
SOLANA_WSS = os.getenv("SOLANA_WSS")
SOLANA_HTTP = os.getenv("SOLANA_HTTP")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
WALLET_PK = os.getenv("WALLET_PK")

EXPECTED_SOL = Decimal("0.02")
REFERRAL_LAMPORTS = int(EXPECTED_SOL * Decimal("0.25") * Decimal(1_000_000_000))
EXPECTED_LAMPORTS = int(EXPECTED_SOL * Decimal(1_000_000_000))
SYSTEM_PROGRAM = "11111111111111111111111111111111"

if not WALLET_ADDRESS:
    raise SystemExit("Set WALLET_ADDRESS env var.")

if not WALLET_PK:
    raise SystemExit("Set WALLET_PK env var.")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

bot_app = Application.builder().token(BOT_TOKEN).build()

# -------------------------------
# ✅ Database setup
# -------------------------------
DATABASE_URL = f"mysql+mysqlconnector://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@{os.getenv('MYSQL_HOST')}/{os.getenv('MYSQL_DATABASE')}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(engine)

pending_profile_edits = {}
pending_hash_edits = {}


def fetch_tx(signature: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed","maxSupportedTransactionVersion": 0, "commitment": "confirmed"}
        ],
    }
    r = requests.post(SOLANA_HTTP, json=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    if "error" in res:
        raise RuntimeError(res["error"])
    return res.get("result")


async def on_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.left_chat_member:
        return

    left_user = update.message.left_chat_member
    if left_user.is_bot:
        return

    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.id == left_user.id))
        await session.commit()

    logger.warning(f"🗑 Removed user {left_user.id} from database (left {update.effective_chat.id})")


def _btns_for_token(mint: str, telegram_url: str | None, links: dict | None) -> InlineKeyboardMarkup:
    links = links or {}
    btns = []

    # Always useful
    btn_row = [InlineKeyboardButton("View on Pump.fun", url=f"https://pump.fun/{mint}")]
    # Add Dexscreener (Solana)
    btn_row.append(InlineKeyboardButton("Dexscreener", url=f"https://dexscreener.com/solana/{mint}"))
    btns.append(btn_row)

    row2 = []
    tg_link = telegram_url or links.get("telegram")
    if tg_link:
        row2.append(InlineKeyboardButton("Telegram", url=tg_link))

    if links.get("website"):
        row2.append(InlineKeyboardButton("Website", url=links["website"]))

    if links.get("twitter"):
        row2.append(InlineKeyboardButton("Twitter/X", url=links["twitter"]))

    if row2:
        btns.append(row2)

    return InlineKeyboardMarkup(btns)


def _build_caption_html(mint: str, metas: dict, telegram_url: str | None) -> str:
    name = metas.get("name") or "Unknown"
    symbol = metas.get("symbol") or ""
    desc = metas.get("description") or ""
    links = metas.get("links") or {}
    fdv = metas.get("fullyDilutedValue")
    decimals = metas.get("decimals")

    parts = []
    title = f"<b>{name}</b>" + (f" <i>({symbol})</i>" if symbol else "")
    parts.append(title)
    parts.append(f"<b>Mint:</b> <code>{mint}</code>")
    if decimals is not None:
        parts.append(f"<b>Decimals:</b> {decimals}")
    if fdv:
        parts.append(f"<b>FDV:</b> {fdv}")
    # prefer explicit telegram parameter over links.telegram
    tg = telegram_url or links.get("telegram")
    if tg:
        parts.append(f"<b>Dev Contact:</b> {tg}")
    if desc:
        # keep it short-ish
        short = (desc[:280] + "…") if len(desc) > 300 else desc
        parts.append(f"\n{short}")

    parts.append("\n<b>Offer:</b> We sent a promo offer (600 USD) to the dev/channel owner.")
    return "\n".join(parts)


async def send_token_notice(user: User, mint_address: str, telegram: str | None, metas: dict):
    """
    Sends a rich notification to a user about a new token.
    - Photo: metas['logo'] (if present)
    - Caption: HTML-formatted with key info
    - Buttons: Pump.fun, Dexscreener, Telegram/Website/Twitter (when available)
    """
    # Extract fields from Moralis metadata
    logo = metas.get("logo")
    links = metas.get("links") or {}
    caption = _build_caption_html(mint_address, metas, telegram)
    keyboard = _btns_for_token(mint_address, telegram, links)

    try:
        if logo:
            # Send as photo with caption
            await bot_app.bot.send_photo(
                chat_id=user.id,
                photo=logo,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            logger.warning(f"Token {mint_address} sent to {user.chat_id}")
        else:
            # Fallback: no image available
            await bot_app.bot.send_message(
                chat_id=user.id,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            logger.warning(f"Token {mint_address} sent to {user.chat_id}")
    except TelegramError as e:
        logger.exception(f"[!] Failed to notify user {user.id}: {e}")


async def send_token(mint_address: str, telegram: str, metas: dict):
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
            logger.warning(f"Token {mint_address} sending to {user.chat_id}")
            await send_token_notice(user, mint_address, telegram, metas)





async def process_token(mint_address: str):
    await asyncio.sleep(60)
    meta = get_token_metadata(mint_address)
    if meta is not None:
        telegram = get_telegram_from_metas(meta)
        await send_token(mint_address, telegram, meta)


def get_telegram_from_metas(data):
    # ✅ Check for Telegram link in the 'links' field
    links = data.get("links", {})
    telegram = links.get("telegram")
    if telegram:
        return telegram

    # 🔄 Fallback: Check metadata URI if exists
    uri = data.get("metaplex", {}).get("metadataUri")
    if uri:
        try:
            meta_res = requests.get(uri)
            meta = meta_res.json()
            if "t.me/" in str(meta):
                match = re.search(r"https?://t\.me/\S+", str(meta))
                if match:
                    return match.group()
        except Exception as e:
            pass
    return None


def get_token_metadata(mint_address: str):
    url = f"https://solana-gateway.moralis.io/token/mainnet/{mint_address}/metadata"

    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            return None

        return response.json()
    except Exception as e:
        return None


# 🌍 Global commands for all users (DM with bot)
default_commands = [
    BotCommand("start", "Start interacting with the bot"),
    BotCommand("faq", "How does it works"),
    BotCommand("membership", "Manage your subscription"),
    BotCommand("referral", "Referral program settings"),
    BotCommand("link", "Your referral link"),
]


def _now():
    return datetime.datetime.now(datetime.UTC)


# Fires when user starts/unblocks (bot becomes MEMBER) or blocks (bot becomes KICKED) in PRIVATE
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    chat = cmu.chat
    if chat.type != "private":
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    # User started/unblocked bot
    if old_status == ChatMemberStatus.LEFT and new_status == ChatMemberStatus.MEMBER:
        with SessionLocal() as session:
            upsert_user_private(session, tg_user=cmu.from_user, private_chat_id=chat.id)
            session.commit()
        await context.bot.send_message(chat_id=chat.id, text="✅ Registered again.")

    # User blocked bot
    if new_status == ChatMemberStatus.LEFT:
        with SessionLocal() as session:
            session.execute(delete(User).where(User.id == chat.id))
            session.commit()


def upsert_user_private(session: Session, *, tg_user, private_chat_id: int, referral_id: int = None) -> User:
    res = session.execute(select(User).where(User.chat_id == private_chat_id))
    user = res.scalar_one_or_none()

    if user is None:
        user = User(
            id=tg_user.id,
            username=getattr(tg_user, "username", None),
            first_name=getattr(tg_user, "first_name", None),
            last_name=getattr(tg_user, "last_name", None),
            chat_id=private_chat_id,  # DM chat id (same as user id in private)
            is_subscribed=False,
            wallet_address="",  # default
            created_at=_now(),
            referral_id=referral_id,
            subscribed_expiry_at=_now() + datetime.timedelta(14)
        )
        session.add(user)
    else:
        logger.exception(f"User {tg_user.id} already exists")
    return user


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = f"🤖 *https://t.me/bottito_search_bot?start={update.effective_user.id}*\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        ("start", "Start interacting with the bot"),
        ("faq", "How does it works"),
        ("membership", "Manage your subscription"),
        ("referral", "Referral program settings"),
        ("link", "Your referral link"),
    ]

    message = "🤖 *Welcome! Here’s what I can do:*\n\n"
    for cmd, desc in commands:
        message += f"{cmd} — {desc}\n"

    await update.message.reply_text(message, parse_mode="Markdown")

    if update.effective_chat.type != "private":
        return

    ref = " ".join(context.args) if context.args else None
    referral_id = None
    if ref is not None and int(ref) != update.effective_user.id:
        referral_id = int(ref)

    with SessionLocal() as session:
        upsert_user_private(
            session,
            tg_user=update.effective_user,
            private_chat_id=update.effective_chat.id,
            referral_id=referral_id
        )
        session.commit()
    await update.message.reply_text("✅ Registered via /start.")


async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 *FAQ* - How does it work?\n\n"
        "Welcome! Here’s how our bot helps you:\n\n"
        "1️⃣ *Token Detection*: The bot monitors new Pump.fun, Raydium and Jupiter token launches in real-time.\n"
        "2️⃣ *Metadata Scanning*: We fetch token details (name, mint address, Telegram link).\n"
        "3️⃣ *Contacting Devs*: If a Telegram is found, we send you token info in this chat.\n"
        "💡 *Tip*: To receive alerts, make sure you’ve started the bot and allowed messages.\n"
        "📌 *Note*: Some Telegram users must message the bot first before it can DM them.\n"
    )

    await update.message.reply_text(help_text, parse_mode="Markdown")


async def membership_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        res = session.execute(select(User).where(User.id == update.effective_user.id))
        user: User = res.scalar_one_or_none()

        if user is not None:
            if user.is_subscribed:
                subscription = "Premium"
            else:
                subscription = "Trial"

            expiry = str(user.subscribed_expiry_at)
            keyboard = None

            if int(user.subscribed_expiry_at.timestamp()) > int(_now().timestamp()):
                message = f"🤖 *Your membership is: {subscription} is expiry {expiry} *\n\n"
            else:
                message = f"""🤖 *Your membership is: {subscription} is expired *\n
💰 Pay {EXPECTED_SOL} SOL fee for monthly subscription
to: *{WALLET_ADDRESS}*"""
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Enter your Transaction Hash", callback_data=f"edit_hash")]
                ])

            await update.message.reply_text(message, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(f"🤖 * Call /start first to register *\n", parse_mode="Markdown")


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        res = session.execute(select(User).where(User.id == update.effective_user.id))
        user: User = res.scalar_one_or_none()

        earned = 0
        un_payments = session.execute(select(ReferralPayment)
        .where(
            ReferralPayment.user_id == update.effective_user.id,
            ReferralPayment.payment_datetime.is_(None))).scalars().all()

        for payment in un_payments:
            earned = earned + payment.amount_lamports

        earned = round(Decimal(earned / 1_000_000_000), 9)

        if user is not None:
            help_text = f"""🤖 *Referral Program*\n\n*Your wallet: {user.wallet_address}*
Our referral program pays you 25% of each subscription registered upon your link.
You have earned: 💰 *{earned} SOL*            
            \n"""

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Edit Your SOL Wallet", callback_data=f"edit_wallet")],
                [InlineKeyboardButton(f"💰 Claim {earned} SOL", callback_data=f"claim")]
            ])

            await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(f"🤖 * Call /start first to register *\n", parse_mode="Markdown")


def check_transaction(signature):
    if not WALLET_ADDRESS:
        raise ValueError("WALLET_ADDRESS env var is required")

    tx = fetch_tx(signature)
    if not tx:
        logger.warning(f"Not tx")
        return False

    msg = (tx.get("transaction") or {}).get("message") or {}
    total_to_wallet = 0

    # Top-level parsed instructions
    for ix in msg.get("instructions", []):
        if ix.get("programId") != SYSTEM_PROGRAM:
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        dest = info.get("destination")
        lamports = int(info.get("lamports", 0))
        if dest == WALLET_ADDRESS:
            total_to_wallet += lamports

    ok = (total_to_wallet == EXPECTED_LAMPORTS)
    return ok


def payout_referral(referral_user: User, referred_user: User):
    with SessionLocal() as session:
        payment = ReferralPayment(
            user_id=referral_user.id,
            referred_user_id=referred_user.id,
            amount_lamports=REFERRAL_LAMPORTS
        )
        session.add(payment)
        session.commit()

async def receive_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text.strip()
    bot_id = str(context.bot.id)

    # --- Wallet Editing ---
    if pending_profile_edits.get(user_id):
        del pending_profile_edits[user_id]

        if len(message) < 25:
            await update.message.reply_text("⚠️ That doesn't look like a valid wallet address.")
            return

        try:
            with SessionLocal() as session:
                with SessionLocal() as session:
                    res = session.execute(select(User).where(User.id == update.effective_user.id))
                    user: User = res.scalar_one_or_none()

                    if user is not None:
                        user.wallet_address = message
                        session.commit()
            await update.message.reply_text("✅ Your wallet address has been updated.")
        except Exception as e:
            logging.exception("Failed to update wallet")
            await update.message.reply_text("⚠️ Failed to save your wallet address.")
        return

    if pending_hash_edits.get(user_id):
        del pending_hash_edits[user_id]

        # Solana signatures are base58: digits + upper/lowercase letters, excluding 0, O, I, l
        BASE58_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{87,88}$')

        if not BASE58_RE.match(message.strip()):
            await update.message.reply_text("⚠️ That doesn't look like a valid Solana transaction hash.")
            return

        try:
            with SessionLocal() as session:
                with SessionLocal() as session:
                    res = session.execute(select(User).where(User.id == update.effective_user.id))
                    user: User = res.scalar_one_or_none()
                    # Fast existence check
                    exists = session.execute(
                        select(TransactionSignature.id).where(
                            TransactionSignature.user_id == user.id,
                            TransactionSignature.signature == message
                        )
                    ).first()
                    if exists:
                        raise Exception("Transaction already exists")  # already recorded
                    if user is not None and check_transaction(message):
                        tx = TransactionSignature(
                            user_id=user.id,
                            signature=message
                        )
                        session.add(tx)
                        user.subscribed_expiry_at = _now() + datetime.timedelta(31)
                        user.is_subscribed = True
                        session.commit()
                        referral_user = session.execute(
                            select(User).where(
                                User.referral_id == user.referral_id
                            )
                        ).scalar_one_or_none()
                        if referral_user:
                            payout_referral(referral_user, user)
                        await update.message.reply_text("✅ Your transaction was checked successfully.")
                    else:
                        await update.message.reply_text("⚠️ Failed to check transaction.")
        except Exception as e:
            logging.exception(f"Failed to check transaction {message}: {e}")
            await update.message.reply_text("⚠️ Failed to check transaction.")
        return
    # --- No context found ---
    await update.message.reply_text("ℹ️ No pending action. Use a command or button to start editing.")


async def edit_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    pending_profile_edits[user_id] = True

    await query.message.reply_text("✍️ Send your new wallet address (Solana):")


async def edit_hash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    pending_hash_edits[user_id] = True

    await query.message.reply_text("✍️ Send transaction hash (Solana):")


def load_payer() -> Keypair:
    return Keypair.from_base58_string(WALLET_PK)




async def send_transaction(recipient: str, lamports: int) -> str:
    client: Client = Client(SOLANA_HTTP)
    rent_min = client.get_minimum_balance_for_rent_exemption(0).value

    if lamports <= rent_min:
        raise ValueError(f"lamports must be > {rent_min}")

    sender = load_payer()
    receiver = Pubkey.from_string(recipient)

    ix = transfer(TransferParams(
        from_pubkey=sender.pubkey(),
        to_pubkey=receiver,
        lamports=lamports
    ))

    # 3) Fetch a fresh recent blockhash and convert to solders.Hash
    lb = client.get_latest_blockhash().value  # has blockhash + lastValidBlockHeight
    # 4) Compile a v0 message (fills the "message")
    msg = MessageV0.try_compile(
        payer=sender.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=lb.blockhash,
    )

    # 5) Create + sign a VersionedTransaction (fills the "from_keypairs")
    tx = VersionedTransaction(msg, [sender])

    # 6) Send (either raw bytes, or pass the object — raw is the safest)
    sig = client.send_raw_transaction(bytes(tx)).value
    return str(sig)


async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    with SessionLocal() as session:
        res = session.execute(select(User).where(User.id == update.effective_user.id))
        user: User = res.scalar_one_or_none()

        if user is not None and user.wallet_address != "":
            earned = 0
            un_payments = session.execute(select(ReferralPayment)
            .where(
                ReferralPayment.user_id == update.effective_user.id,
                ReferralPayment.payment_datetime.is_(None))).scalars().all()

            for payment in un_payments:
                earned = earned + payment.amount_lamports
                payment.payment_datetime=_now()
                session.commit()
            try:
                tx_hash = await send_transaction(user.wallet_address, earned)
                earned = round(Decimal(earned / 1_000_000_000), 9)
                await query.message.reply_text(f"✅ Transaction sent {tx_hash} {earned} SOL.")
            except ValueError as e:
                await query.message.reply_text(f"✍ Error sending transaction: {e}")
        else:
            await query.message.reply_text("✍️ You does not have configured your Wallet Address yet.")


async def main():
    uri = "wss://pumpportal.fun/api/data"

    # Set default commands
    await bot_app.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    # Register command handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("faq", faq_command))
    bot_app.add_handler(CommandHandler("membership", membership_command))
    bot_app.add_handler(CommandHandler("referral", referral_command))
    bot_app.add_handler(CommandHandler("link", link_command))

    # Callback query handlers
    bot_app.add_handler(CallbackQueryHandler(edit_wallet_callback, pattern=r"^edit_wallet$"))
    bot_app.add_handler(CallbackQueryHandler(edit_hash_callback, pattern=r"^edit_hash$"))
    bot_app.add_handler(CallbackQueryHandler(claim_callback, pattern=r"^claim$"))
    # Message handler
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_input))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_member_left))
    bot_app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # Initialize and start bot polling
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.warning("🤖 Telegram bot started.")

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.warning("✅ Subscribed to PumpPortal...")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        mint = data.get("mint")
                        if mint:
                            asyncio.create_task(process_token(mint))
                    except Exception as e:
                        logger.exception(f"⚠️ Error processing message: {e}")

        except ConnectionClosedError as e:
            logger.exception(f"🔌 Connection closed unexpectedly: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except WebSocketException as e:
            logger.exception(f"❗ WebSocket exception: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception(f"🔥 Unexpected error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)


asyncio.run(main())
