import asyncio
import json
import os
import re

import dotenv
import requests


import telegram
import websockets
from telegram._bot import Bot
from websockets import ConnectionClosedError, WebSocketException
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsAdmins, Channel, User

dotenv.load_dotenv()

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")  # Set this in your environment
HEADERS = {
    "accept": "application/json",
    "X-API-Key": MORALIS_API_KEY,
}

api_id = int(os.getenv("TG_API_ID"))          # from https://my.telegram.org
api_hash = os.getenv("TG_API_HASH")
phone = os.getenv("TG_PHONE_NUMBER")          # e.g. +420123456789
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
client = TelegramClient("session", api_id, api_hash)

async def store_token(mint_address: str, symbol: str, name: str, telegram: str):
    # find owner of telegram group by https://t.me/username
    # send direct message to owner of group
    # store token in database and send info to telegram bot users in private chat with info that offer was sent
    print(f"New Token: {symbol}: {name} | Mint: {mint_address} | Telegram: {telegram}")

    username = telegram.replace("https://t.me/", "").strip("/@")
    try:
        chat = await bot.get_chat(username)

        if chat.type == "private":
            # DM user
            # await bot.send_message(
            #     chat_id=chat.id,
            #     text=f"👋 Hey {chat.first_name or ''}, we noticed your token *{symbol}* just launched. Want help promoting it?",
            #     parse_mode="Markdown"
            # )
            print(f"[+] DM sent to user @{username}")

        elif chat.type in ["channel", "supergroup", "group"]:
            username = get_channel_owner(username)
            chat = await bot.get_chat(username)
            # await bot.send_message(
            #     chat_id=chat.id,
            #     text=f"👋 Hey {chat.first_name or ''}, we noticed your token *{symbol}* just launched. Want help promoting it?",
            #     parse_mode="Markdown"
            # )
            print(f"[+] DM sent to user @{username}")

        else:
            print(f"[!] Unknown chat type for @{username}: {chat.type}")

    except:
        print(f"[!] Could not resolve @{username}")

async def process_token(mint_address: str, symbol: str, name: str):
    telegram = None
    t = 0
    while telegram is None and t < 4:
        telegram = await get_token_metadata(mint_address, symbol, name)
        if telegram is not None:
            break
        await asyncio.sleep(60*15)
        t = t + 1
    if telegram is not None and "https://t.me/" in str(telegram):
        await store_token(mint_address, symbol, name, telegram)

async def get_token_metadata(mint_address: str, symbol: str, name: str):
    url = f"https://solana-gateway.moralis.io/token/mainnet/{mint_address}/metadata"

    try:
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            return None

        data = response.json()

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

    except Exception as e:
        return None


async def main():
    uri = "wss://pumpportal.fun/api/data"

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                print("✅ Subscribed to PumpPortal...")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        mint = data.get("mint")
                        name = data.get("name")
                        symbol = data.get("symbol")

                        if mint and symbol and name:
                            asyncio.create_task(process_token(mint, symbol, name))
                    except Exception as e:
                        print(f"⚠️ Error processing message: {e}")

        except ConnectionClosedError as e:
            print(f"🔌 Connection closed unexpectedly: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except WebSocketException as e:
            print(f"❗ WebSocket exception: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"🔥 Unexpected error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)



async def get_channel_owner(username):
    await client.start(phone=phone)
    try:
        channel = await client.get_entity(username)
        if isinstance(channel, Channel):
            admins = await client.get_participants(channel, filter=ChannelParticipantsAdmins())
            for admin in admins:
                if admin.participant.creator:
                    print(f"✅ Creator found: {admin.username}")
                    return admin.username
            print("❌ No creator found in admin list.")
        else:
            print("Provided username is not a channel.")
    except Exception as e:
        print(f"Error: {e}")

    await client.disconnect()

asyncio.run(main())
