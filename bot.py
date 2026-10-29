import asyncio
import base64
import datetime
import json
import logging
import os
import re
import struct
from _decimal import Decimal
from zoneinfo import ZoneInfo

import dotenv
import requests
import websockets
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fernet import Fernet
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
from telethon import TelegramClient
from telethon import types
from telethon.errors import (
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from websockets import ConnectionClosedError, WebSocketException

from dbase import (
    Base,
    Channel,
    ReferralPayment,
    TransactionSignature,
    User,
    claim_token_mint,
    mark_token_sent,
    release_token_claim,
)

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
TZ = ZoneInfo("Europe/Prague")
BASE_URL = os.getenv("BASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOLANA_WSS = os.getenv("SOLANA_WSS")
SOLANA_HTTP = os.getenv("SOLANA_HTTP")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
WALLET_PK = os.getenv("WALLET_PK")
SECRET_KEY = bytes(os.getenv("SECRET_KEY").encode("utf8"))
EXPECTED_SOL = Decimal("0.05")
REFERRAL_LAMPORTS = int(EXPECTED_SOL * Decimal("0.25") * Decimal(1_000_000_000))
EXPECTED_LAMPORTS = int(EXPECTED_SOL * Decimal(1_000_000_000))
SYSTEM_PROGRAM = "11111111111111111111111111111111"
IPC_TOKEN = os.getenv("IPC_TOKEN")
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH")
PHONE = os.getenv("TG_PHONE")
REQUEST_TIMEOUT = 20
METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
TOKEN_PROCESS_CONCURRENCY = max(1, int(os.getenv("TOKEN_PROCESS_CONCURRENCY", "4")))
WS_PING_INTERVAL = float(os.getenv("WS_PING_INTERVAL", "20"))
WS_PING_TIMEOUT = float(os.getenv("WS_PING_TIMEOUT", "30"))
WS_OPEN_TIMEOUT = float(os.getenv("WS_OPEN_TIMEOUT", "20"))

if not WALLET_ADDRESS:
    raise SystemExit("Set WALLET_ADDRESS env var.")

if not WALLET_PK:
    raise SystemExit("Set WALLET_PK env var.")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

bot_app = Application.builder().token(BOT_TOKEN).build()
if not API_ID or not API_HASH or not PHONE:
    raise RuntimeError("Missing TG_API_ID, TG_API_HASH, TG_PHONE in .env")

tg_client: TelegramClient | None = None

# -------------------------------
# ✅ Database setup
# -------------------------------
DATABASE_URL = f"mysql+mysqlconnector://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@{os.getenv('MYSQL_HOST')}/{os.getenv('MYSQL_DATABASE')}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(engine)
token_process_semaphore = asyncio.Semaphore(TOKEN_PROCESS_CONCURRENCY)
background_tasks: set[asyncio.Task] = set()

pending_profile_edits = {}
pending_hash_edits = {}


def extract_private_invite_hash(link: str) -> str | None:
    """
    Supports:
    https://t.me/+abcdef
    https://t.me/joinchat/abcdef
    """
    patterns = [
        r"t\.me/\+([A-Za-z0-9_-]+)",
        r"t\.me/joinchat/([A-Za-z0-9_-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            return match.group(1)

    return None


def is_group_or_supergroup(entity) -> bool:
    """
    True only for:
      - normal Telegram groups: types.Chat
      - supergroups: types.Channel with megagroup=True

    False for:
      - broadcast channels
      - users
      - bots
      - unknown entities
    """
    if isinstance(entity, types.Chat):
        return True

    if isinstance(entity, types.Channel):
        return bool(getattr(entity, "megagroup", False))

    return False


def invite_info_is_group_or_supergroup(invite_info) -> bool:
    """
    CheckChatInviteRequest can return:
      - ChatInviteAlready with .chat
      - ChatInvite with flags like megagroup/broadcast/channel
    """
    chat = getattr(invite_info, "chat", None)

    if chat:
        return is_group_or_supergroup(chat)

    # Private invite, not joined yet.
    # Broadcast channel invite must be skipped.
    if getattr(invite_info, "broadcast", False):
        return False

    # Supergroup invite.
    if getattr(invite_info, "megagroup", False):
        return True

    # Some old/basic group invites may not have megagroup=True.
    # If it is not marked as a channel/broadcast, treat it as a group.
    if getattr(invite_info, "channel", None) is False:
        return True

    return False


async def join_group(client: TelegramClient, group_link: str):
    private_hash = extract_private_invite_hash(group_link)

    # Private invite link: https://t.me/+xxxx or https://t.me/joinchat/xxxx
    if private_hash:
        try:
            invite_info = await client(CheckChatInviteRequest(private_hash))

            if not invite_info_is_group_or_supergroup(invite_info):
                logger.warning("Skipping invite because it is not group/supergroup: %s", group_link)
                return None

            already_chat = getattr(invite_info, "chat", None)

            if already_chat:
                logger.warning("Already joined private group/supergroup.")
                return await client.get_entity(already_chat)

            result = await client(ImportChatInviteRequest(private_hash))

            chats = getattr(result, "chats", None) or []

            for chat in chats:
                if is_group_or_supergroup(chat):
                    logger.warning(
                        "Joined private group/supergroup: type=%s id=%s title=%s",
                        type(chat).__name__,
                        getattr(chat, "id", None),
                        getattr(chat, "title", None),
                    )
                    return await client.get_entity(chat)

            logger.warning("Joined invite but no group/supergroup entity found: %s", group_link)
            return None

        except UserAlreadyParticipantError:
            logger.warning("Already joined private group.")

            invite_info = await client(CheckChatInviteRequest(private_hash))
            chat = getattr(invite_info, "chat", None)

            if chat and is_group_or_supergroup(chat):
                return await client.get_entity(chat)

            logger.warning("Already joined, but target is not group/supergroup: %s", group_link)
            return None

        except InviteHashExpiredError:
            logger.warning("Telegram invite link expired: %s", group_link)
            return None

        except InviteHashInvalidError:
            logger.warning("Invalid Telegram invite link: %s", group_link)
            return None

    # Public group/channel
    entity = await client.get_entity(group_link)

    if not is_group_or_supergroup(entity):
        logger.warning(
            "Skipping public target because it is not group/supergroup: type=%s id=%s title=%s username=%s",
            type(entity).__name__,
            getattr(entity, "id", None),
            getattr(entity, "title", None),
            getattr(entity, "username", None),
        )
        return None

    try:
        result = await client(JoinChannelRequest(entity))
        logger.warning("Joined public group/supergroup.")

        chats = getattr(result, "chats", None) or []

        for chat in chats:
            if is_group_or_supergroup(chat) and getattr(chat, "id", None) == getattr(entity, "id", None):
                return await client.get_entity(chat)

    except UserAlreadyParticipantError:
        logger.warning("Already joined public group/supergroup.")

    return await client.get_entity(entity)


def fetch_tx(signature: str):
    return rpc_call(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
        ],
    )


async def on_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.left_chat_member:
        return

    left_user = update.message.left_chat_member
    if left_user.is_bot:
        return

    with SessionLocal() as session:
        session.execute(delete(User).where(User.id == left_user.id))
        session.commit()

    logger.warning(f"🗑 Removed user {left_user.id} from database (left {update.effective_chat.id})")


def generate_make_money_link(user_id: str, tg_link: str, mint: str):
    return f"{BASE_URL}/make-money?user_id={user_id}&tg_link={tg_link}&mint={mint}"


def generate_promote_link(user_id: str, mint: str, caption: str, logo: str | None, links: dict):
    # Create encryption object
    f = Fernet(SECRET_KEY)

    # Prepare parameters
    params = {
        "mint": mint,
        "caption": caption,
        "logo": logo,
        "links": links,
        "user_id": user_id
    }

    # Convert to JSON string
    json_data = json.dumps(params, separators=(",", ":"))

    # Encrypt the JSON data
    token = f.encrypt(json_data.encode()).decode()

    # Create final URL
    url = f"{BASE_URL}/create_promotion?data={token}"

    return url


async def _promote_to_chats(mint: str, caption: str, logo: str, telegram_url: str, links: dict,
                            chat_ids: list[int | str]):
    keyboard = _btns_for_token(mint, caption, logo, telegram_url, links, use_promote_button=False)

    for chat_id in chat_ids:
        try:
            if logo:
                await bot_app.bot.send_photo(
                    chat_id=chat_id,
                    photo=logo,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            logger.warning(f"[IPC] Posted token {mint} to {chat_id}")
        except TelegramError as e:
            logger.exception(f"[IPC] Failed posting to {chat_id}: {e}")


async def ipc_post_handler(request: web.Request):
    # Simple bearer token check
    auth = request.headers.get("Authorization", "")
    if not IPC_TOKEN or auth != f"Bearer {IPC_TOKEN}":
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
        print(payload)
        # expected keys
        mint = payload.get("mint") or ""
        telegram_url = payload.get("telegram")  # optional
        metas = payload.get("metas") or {}
        logo = metas.get("logo")
        caption = payload.get("caption")
        chat_ids = payload.get("chat_ids") or []  # list of channel IDs or @usernames

        if not mint or not metas or not chat_ids:
            return web.json_response({"ok": False, "error": "mint/metas/chat_ids required"}, status=400)

        # Fire and forget: post to chats
        schedule_background_task(
            _promote_to_chats(mint, caption, logo, telegram_url, metas.get('links') or {}, chat_ids)
        )
        return web.json_response({"ok": True})
    except Exception as e:
        logger.exception(f"[IPC] Bad request: {e}")
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)


def _btns_for_token(mint: str, caption: str, logo: str | None, telegram_url: str | None,
                    links: dict | None, use_promote_button=True, user_id="") -> InlineKeyboardMarkup:
    links = links or {}
    btns = []
    link_links = dict()

    # Always useful
    btn_row = [InlineKeyboardButton("View on Pump.fun", url=f"https://pump.fun/{mint}")]
    link_links["pumpfun"] = f"https://pump.fun/{mint}"
    # Add Dexscreener (Solana)
    btn_row.append(InlineKeyboardButton("Dexscreener", url=f"https://dexscreener.com/solana/{mint}"))
    link_links["dexscreener"] = f"https://dexscreener.com/solana/{mint}"
    btns.append(btn_row)

    row2 = []
    tg_link = telegram_url or links.get("telegram")
    if tg_link:
        row2.append(InlineKeyboardButton("Telegram", url=tg_link))
        link_links["telegram"] = tg_link
    if links.get("website"):
        row2.append(InlineKeyboardButton("Website", url=links["website"]))
        link_links["website"] = links["website"]
    if links.get("twitter"):
        row2.append(InlineKeyboardButton("Twitter/X", url=links["twitter"]))
        link_links["twitter"] = links["twitter"]

    if row2:
        btns.append(row2)

    if use_promote_button and user_id != "":
        row3 = [InlineKeyboardButton("Promote in Channels",
                                     url=generate_promote_link(user_id, mint, caption, logo, link_links)),
                InlineKeyboardButton("Make money", url=generate_make_money_link(user_id, tg_link, mint))
                ]
        btns.append(row3)
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

    return "\n".join(parts)


async def send_token_notice(user: str, mint_address: str, telegram: str | None, metas: dict):
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
    keyboard = _btns_for_token(mint_address, caption, logo, telegram, links, user_id=user)

    try:
        if logo:
            # Send as photo with caption
            await bot_app.bot.send_photo(
                chat_id=user,
                photo=logo,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            # Fallback: no image available
            await bot_app.bot.send_message(
                chat_id=user,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        return True
    except TelegramError as e:
        logger.warning("Failed sending token %s to user %s: %s", mint_address, user, e)
        return False


async def send_token(mint_address: str, telegram: str | None, metas: dict):
    logger.warning(f"Sending {mint_address}")
    # try:
    #     entity = await join_group(tg_client, telegram)
    #
    #     if entity is not None:
    #         await tg_client.send_message(entity, "Hi Everyone")
    # except Exception:
    #     logger.exception("Failed to join %s", telegram)

    with SessionLocal() as session:
        subscribed_user_ids = session.execute(select(User.id)).scalars().all()

    sent_count = 0
    for user_id in subscribed_user_ids:
        if await send_token_notice(user_id, mint_address, telegram, metas):
            sent_count += 1
    return sent_count


async def process_token(mint_address: str, immediate=True):
    mint_address = mint_address.strip()
    if not mint_address:
        return False

    with SessionLocal() as session:
        claimed = claim_token_mint(session, mint_address)
    if not claimed:
        logger.warning("Skipping duplicate token mint %s", mint_address)
        return False

    delivered = False
    try:
        async with token_process_semaphore:
            if not immediate:
                await asyncio.sleep(60 * 5)
            meta = await asyncio.to_thread(get_token_metadata, mint_address)
            if meta is not None:
                telegram = await asyncio.to_thread(get_telegram_from_metas, meta)
                if telegram is not None:
                    sent_count = await send_token(mint_address, telegram, meta)
                    delivered = sent_count > 0

        if delivered:
            with SessionLocal() as session:
                mark_token_sent(session, mint_address)
            logger.warning("Stored sent token mint %s", mint_address)
        return delivered
    finally:
        if not delivered:
            with SessionLocal() as session:
                release_token_claim(session, mint_address)


def _on_background_task_done(task: asyncio.Task):
    background_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Background task failed")


def schedule_background_task(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)
    return task


def get_telegram_from_metas(data):
    # ✅ Check for Telegram link in the 'links' field
    links = data.get("links")

    telegram = links.get("telegram") if links is not None else None

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


def rpc_call(method: str, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = requests.post(SOLANA_HTTP, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def fetch_json(url: str, timeout: int = REQUEST_TIMEOUT) -> dict | list | None:
    if not url:
        return None

    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned or None


def _first_value(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _to_decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _format_money(value) -> str | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    quantized = decimal_value.quantize(Decimal("0.01"))
    return f"${quantized:,.2f}"


def _extract_links_from_dexscreener(pair: dict | None) -> dict:
    if not pair:
        return {}

    info = pair.get("info") or {}
    links = {}

    for website in info.get("websites") or []:
        label = (website.get("label") or "website").lower()
        url = website.get("url")
        if url and label not in links:
            links[label] = url

    for social in info.get("socials") or []:
        social_type = (social.get("type") or "").lower()
        url = social.get("url")
        if url and social_type in {"telegram", "twitter", "x", "website"}:
            key = "twitter" if social_type == "x" else social_type
            links[key] = url

    return links


def _extract_links_from_offchain_metadata(metadata: dict | None) -> dict:
    if not isinstance(metadata, dict):
        return {}

    links = {}
    direct_fields = {
        "telegram": metadata.get("telegram"),
        "twitter": metadata.get("twitter"),
        "website": metadata.get("website"),
    }

    for key, value in direct_fields.items():
        if isinstance(value, str) and value:
            links[key] = value

    for container_key in ("links", "extensions"):
        container = metadata.get(container_key)
        if not isinstance(container, dict):
            continue
        for source_key in ("telegram", "twitter", "website"):
            value = container.get(source_key)
            if isinstance(value, str) and value:
                links[source_key] = value

    for social in metadata.get("socials") or []:
        if not isinstance(social, dict):
            continue
        social_type = (social.get("type") or "").lower()
        url = social.get("url")
        if url and social_type in {"telegram", "twitter", "x", "website"}:
            links["twitter" if social_type == "x" else social_type] = url

    return links


def _merge_links(*sources: dict | None) -> dict:
    merged = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("telegram", "website", "twitter"):
            value = source.get(key)
            if isinstance(value, str) and value and key not in merged:
                merged[key] = value
    return merged


def _pick_best_pair(pairs: list[dict] | None, mint_address: str) -> dict | None:
    if not pairs:
        return None

    mint_lower = mint_address.lower()

    def pair_score(pair: dict):
        base_address = ((pair.get("baseToken") or {}).get("address") or "").lower()
        liquidity = _to_decimal((pair.get("liquidity") or {}).get("usd")) or Decimal("0")
        volume = _to_decimal((pair.get("volume") or {}).get("h24")) or Decimal("0")
        primary = 1 if base_address == mint_lower else 0
        return primary, liquidity, volume

    return max(pairs, key=pair_score)


def get_dexscreener_token_data(mint_address: str) -> dict | None:
    try:
        response = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None

    pair = _pick_best_pair(data.get("pairs"), mint_address)
    if not pair:
        return None

    return {
        "pair": pair,
        "name": _clean_text((pair.get("baseToken") or {}).get("name")),
        "symbol": _clean_text((pair.get("baseToken") or {}).get("symbol")),
        "logo": (pair.get("info") or {}).get("imageUrl"),
        "links": _extract_links_from_dexscreener(pair),
        "fdv": _first_value(pair.get("fdv"), pair.get("marketCap")),
    }


def get_asset_data(mint_address: str) -> dict | None:
    try:
        return rpc_call("getAsset", {"id": mint_address})
    except Exception:
        return None


def _read_borsh_string(blob: bytes, offset: int) -> tuple[str | None, int]:
    length = struct.unpack_from("<I", blob, offset)[0]
    offset += 4
    value = blob[offset:offset + length].decode("utf-8", errors="ignore")
    offset += length
    return _clean_text(value), offset


def get_onchain_metadata(mint_address: str) -> dict | None:
    try:
        mint = Pubkey.from_string(mint_address)
        metadata_pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(METADATA_PROGRAM_ID), bytes(mint)],
            METADATA_PROGRAM_ID,
        )
        account_info = rpc_call(
            "getAccountInfo",
            [str(metadata_pda), {"encoding": "base64", "commitment": "confirmed"}],
        )
        value = (account_info or {}).get("value") or {}
        data = value.get("data") or []
        if not data or not data[0]:
            return None

        raw = base64.b64decode(data[0])
        offset = 1 + 32 + 32
        name, offset = _read_borsh_string(raw, offset)
        symbol, offset = _read_borsh_string(raw, offset)
        uri, _ = _read_borsh_string(raw, offset)
        return {"name": name, "symbol": symbol, "metadataUri": uri}
    except Exception:
        return None


def get_token_supply_data(mint_address: str) -> dict | None:
    try:
        supply = rpc_call("getTokenSupply", [mint_address, {"commitment": "confirmed"}]) or {}
    except Exception:
        return None

    value = supply.get("value") or {}
    return {
        "decimals": value.get("decimals"),
        "supply": _to_decimal(value.get("uiAmountString")),
    }


def get_token_metadata(mint_address: str):
    try:
        asset = get_asset_data(mint_address) or {}
        token_info = asset.get("token_info") or {}
        content = asset.get("content") or {}
        content_metadata = content.get("metadata") or {}
        onchain_metadata = get_onchain_metadata(mint_address) or {}
        supply_data = get_token_supply_data(mint_address) or {}
        dex_data = get_dexscreener_token_data(mint_address) or {}
        dex_pair = dex_data.get("pair") or {}

        metadata_uri = _first_value(content.get("json_uri"), onchain_metadata.get("metadataUri"))
        offchain_metadata = fetch_json(metadata_uri) if metadata_uri else None

        image = _first_value(
            (content.get("links") or {}).get("image"),
            ((content.get("files") or [{}])[0].get("uri") if content.get("files") else None),
            (offchain_metadata or {}).get("image"),
            dex_data.get("logo"),
        )

        links = _merge_links(
            _extract_links_from_offchain_metadata(offchain_metadata),
            dex_data.get("links"),
        )

        supply = _first_value(supply_data.get("supply"), _to_decimal(token_info.get("supply")))
        price = _to_decimal((token_info.get("price_info") or {}).get("price_per_token"))
        fdv = _first_value(
            dex_data.get("fdv"),
            supply * price if supply is not None and price is not None else None,
        )

        metadata = {
            "name": _first_value(
                _clean_text(content_metadata.get("name")),
                onchain_metadata.get("name"),
                _clean_text((offchain_metadata or {}).get("name")),
                dex_data.get("name"),
            ) or "Unknown",
            "symbol": _first_value(
                _clean_text(content_metadata.get("symbol")),
                onchain_metadata.get("symbol"),
                _clean_text((offchain_metadata or {}).get("symbol")),
                dex_data.get("symbol"),
            ) or "",
            "description": _first_value(
                _clean_text(content_metadata.get("description")),
                _clean_text((offchain_metadata or {}).get("description")),
            ) or "",
            "decimals": _first_value(token_info.get("decimals"), supply_data.get("decimals")),
            "fullyDilutedValue": _format_money(fdv),
            "logo": image,
            "links": links,
            "metaplex": {"metadataUri": metadata_uri} if metadata_uri else {},
            "priceUsd": str(price) if price is not None else None,
            "marketCap": _format_money(dex_pair.get("marketCap")),
        }

        if any(
            metadata.get(key)
            for key in ("name", "symbol", "description", "logo", "links", "metaplex", "decimals", "fullyDilutedValue")
        ):
            return metadata
    except Exception:
        logger.exception("Failed loading token metadata for %s", mint_address)
        return None
    return None


# 🌍 Global commands for all users (DM with bot)
default_commands = [
    BotCommand("start", "Start interacting with the bot"),
    BotCommand("faq", "How does it works"),
    BotCommand("howto", "How to use bot"),
    BotCommand("settings", "Setup your filters and behaviour of bot"),
    BotCommand("mint", "Display token by mint address")
]


def _now():
    return datetime.datetime.utcnow()


# Fires when user starts/unblocks (bot becomes MEMBER) or blocks (bot becomes KICKED) in PRIVATE
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    chat = cmu.chat

    logger.warning(f"{chat.title}: {chat.type}")

    if chat.type == "private":
        old_status = cmu.old_chat_member.status
        new_status = cmu.new_chat_member.status

        # User started/unblocked bot
        if new_status == ChatMemberStatus.MEMBER:
            with SessionLocal() as session:
                upsert_user_private(session, tg_user=cmu.from_user, private_chat_id=chat.id)
                session.commit()
            await context.bot.send_message(chat_id=chat.id, text="✅ Registered again.")

        # User blocked bot
        if new_status == ChatMemberStatus.LEFT:
            with SessionLocal() as session:
                session.execute(delete(User).where(User.id == cmu.from_user.id))
                session.commit()

    if chat.type == "channel" or chat.type == "supergroup":
        actor = cmu.from_user  # admin who added/removed the bot
        old_status = cmu.old_chat_member.status
        new_status = cmu.new_chat_member.status

        # Upsert/delete channel entry keyed by (user_id, chat_id)
        with SessionLocal() as session:
            if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
                # upsert Channel
                row = session.get(Channel, (actor.id, chat.id))  # composite PK order: (user_id, chat_id)
                if row is None:
                    upsert_user_private(session, tg_user=actor, private_chat_id=actor.id)
                    row = Channel(user_id=actor.id, chat_id=chat.id,
                                  chat_name=chat.title.encode("ascii", "ignore").decode())
                    session.add(row)
                else:
                    row.chat_name = chat.title.encode("ascii", "ignore").decode()
                session.commit()
                await context.bot.send_message(chat_id=actor.id,
                                               text=f"✅ Bot has been registered to channel {chat.title}.")

                # Optional: warn if no posting right
                can_post = getattr(cmu.new_chat_member, "can_post_messages", True)
                if not can_post:
                    logger.warning(
                        f"Bot lacks 'post messages' right in channel {chat.id} ({chat.title})"
                    )
            elif new_status in ("left", "kicked"):
                row = session.get(Channel, (actor.id, chat.id))
                if row:
                    session.delete(row)
                    session.commit()
    return


def upsert_user_private(session: Session, *, tg_user, private_chat_id: int, referral_id: int = None) -> User:
    res = session.execute(select(User).where(User.chat_id == private_chat_id))
    user = res.scalar_one_or_none()

    if user is None:
        user = User(
            id=tg_user.id,
            username=str(getattr(tg_user, "username", None)).encode("ascii", "ignore").decode(),
            first_name=str(getattr(tg_user, "first_name", None)).encode("ascii", "ignore").decode(),
            last_name=str(getattr(tg_user, "last_name", None)).encode("ascii", "ignore").decode(),
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


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = f"🤖 *https://t.me/bottito_search_bot?start={update.effective_user.id}*\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def mint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = f"🤖 *https://t.me/bottito_search_bot?start={update.effective_user.id}*\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def howto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = f"""
# *🤖 How to Use Bottito Bot*

https://www.bottito.fun

## 1️⃣ Start the Bot
- Open [@bottito_search_bot](https://t.me/bottito_search_bot)  
- Press *Start* 

---

## 2️⃣ Available Commands
- */start* → Register yourself (saves referral if any)  
- */faq* → How the bot works  
- */howto* → Quick usage guide  

---

## 3️⃣ Token Notifications  
  - The bot monitors **Pump.fun** tokens in real-time.  
  - Waits a few minutes to fetch metadata.  
  - If a Telegram link is found → you get a rich notification with:  
    - Token name, symbol, mint, FDV, description  
    - Buttons: Pump.fun | Dexscreener | Telegram | Website | Twitter  
    - (Optional) Promote button if you manage channels  

---

## 4️⃣ Promote in Channels
- Add the bot to your **Telegram channel** as Admin.  
- The bot registers your channel.  
- You’ll see a **Promote in Channels** button in token alerts.  
- Clicking it auto-posts token info into your channels.  

---

✅ **In summary:**  
Start → Free premium → Get token alerts → Make money → Earn SOL → Promote in your channels.  
"""

    await update.message.reply_text(message, parse_mode="Markdown")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        ("start", "Start interacting with the bot"),
        ("faq", "How does it works"),
        ("howto", "Howto use bot"),
    ]

    message = "🤖 *Welcome! Here’s what I can do, tokens are displayed all day:*\n\n"
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
        "Welcome! Here’s how our bot helps you https://www.bottito.fun\n\n"
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


def escape_md(text: str) -> str:
    specials = r"_*[]()~`>#+-=|{}.!<>"
    for ch in specials:
        text = text.replace(ch, f"\\{ch}")
    return text


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

        earned = float(round(Decimal(earned / 1_000_000_000), 9))

        if user is not None:
            help_text = f"""🤖 *Referral Program*\n\n*Your wallet: {user.wallet_address}*
Our referral program pays you 25% of each subscription registered upon your link.
You have earned: 💰 *{earned} SOL*            
            \n"""
            referrals = session.execute(
                select(User).where(User.referral_id == update.effective_user.id)).scalars().all()

            for referral in referrals:
                help_text = help_text + f"@{escape_md(referral.username)} expiry *{referral.subscribed_expiry_at}*\n"

            logger.warning(help_text)

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

    # --- send message to other users --- #3
    if "#message_to_all:" in message:
        logger.warning(f"Message to all: {message}")
        with SessionLocal() as session:
            res = session.execute(select(User).where(User.id == update.effective_user.id))
            user: User = res.scalar_one_or_none()
            if user is not None and user.is_super_admin:
                user_ids = session.execute(
                    select(User.id).where(User.id != update.effective_user.id)
                ).scalars().all()
            else:
                user_ids = []
        for user_id_to_message in user_ids:
            try:
                await bot_app.bot.send_message(user_id_to_message, message.split("#message_to_all:")[1])
            except Exception:
                pass
        if user_ids:
            return

    if "#message_to_expired:" in message:
        logger.warning(f"Message to all: {message}")
        with SessionLocal() as session:
            res = session.execute(select(User).where(User.id == update.effective_user.id))
            user: User = res.scalar_one_or_none()
            if user is not None and user.is_super_admin:
                expired_user_ids = [
                    user_to_message.id
                    for user_to_message in session.execute(
                        select(User).where(User.id != update.effective_user.id)
                    ).scalars().all()
                    if int(user_to_message.subscribed_expiry_at.timestamp()) < int(_now().timestamp())
                ]
            else:
                expired_user_ids = []
        for expired_user_id in expired_user_ids:
            try:
                await bot_app.bot.send_message(expired_user_id, message.split("#message_to_expired:")[1])
            except Exception:
                pass
        if expired_user_ids:
            return

    if "#pin_to_all:" in message:
        logger.warning(f"Pin to all: {message}")
        with SessionLocal() as session:
            res = session.execute(select(User).where(User.id == update.effective_user.id))
            user: User = res.scalar_one_or_none()
            if user is not None and user.is_super_admin:
                user_ids = session.execute(
                    select(User.id).where(User.id != update.effective_user.id)
                ).scalars().all()
            else:
                user_ids = []
        for user_id_to_message in user_ids:
            try:
                msg_id = (
                    await bot_app.bot.send_message(user_id_to_message, message.split("#pin_to_all:")[1])
                ).message_id
                await context.bot.pin_chat_message(
                    chat_id=user_id_to_message,
                    message_id=msg_id,
                    disable_notification=False
                )
            except Exception:
                pass
        if user_ids:
            return

    if "#mint_to_all:" in message:
        logger.warning(f"Mint to all: {message}")
        await process_token(message.split("#mint_to_all:")[1].strip(), immediate=True)
        return
    # --- Wallet Editing ---
    if pending_profile_edits.get(user_id):
        del pending_profile_edits[user_id]

        if len(message) < 25:
            await update.message.reply_text("⚠️ That doesn't look like a valid wallet address.")
            return

        try:
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
                    reply_text = "✅ Your transaction was checked successfully."
                else:
                    reply_text = "⚠️ Failed to check transaction."
            await update.message.reply_text(reply_text)
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

            try:
                if user.wallet_address != "":
                    tx_hash = await send_transaction(user.wallet_address, earned)
                    for payment in un_payments:
                        payment.payment_datetime = _now()
                        session.commit()
                    earned_x = round(Decimal(earned / 1_000_000_000), 9)
                    await query.message.reply_text(f"✅ Transaction sent {tx_hash} {earned_x} SOL.")
                else:
                    await query.message.reply_text(f"✍ You have to set your Wallet Address")
            except ValueError as e:
                await query.message.reply_text(f"✍ Error sending transaction: {e}")
        else:
            await query.message.reply_text("✍️ You does not have configured your Wallet Address yet.")


async def main():
    # global tg_client
    #
    # tg_client = TelegramClient("telegram_session", API_ID, API_HASH)
    # await tg_client.start(phone=PHONE)
    #
    # logger.warning("✅ Telethon user client started.")
    uri = "wss://pumpportal.fun/api/data"

    # Set default commands
    await bot_app.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    # Register command handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("faq", faq_command))
    bot_app.add_handler(CommandHandler("howto", howto_command))
    bot_app.add_handler(CommandHandler("settings", settings_command))
    bot_app.add_handler(CommandHandler("mint", mint_command))

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
    ipc_app = web.Application()
    ipc_app.add_routes([web.post("/ipc/post", ipc_post_handler)])
    runner = web.AppRunner(ipc_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8787)
    await site.start()
    logger.warning("📮 IPC server listening on http://127.0.0.1:8787/ipc/post")
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.start()
    logger.warning("✅ Scheduler started.")

    while True:
        try:
            async with websockets.connect(
                uri,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
                open_timeout=WS_OPEN_TIMEOUT,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.warning("✅ Subscribed to PumpPortal...")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        mint = data.get("mint")
                        if mint:
                            schedule_background_task(process_token(mint))
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
