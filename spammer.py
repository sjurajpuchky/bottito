import asyncio
import logging
import os

import dotenv
from telethon import TelegramClient, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest

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

api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

SESSION_NAME = "telegram_session"

MESSAGE = f"""
New meme coin on Solana join https://t.me/+-iZU3WiJ7JQwYjg0 Free Airdrop available :-) type Airdrop for more info
"""


def is_group_or_supergroup(entity) -> bool:
    if isinstance(entity, types.Chat):
        return True

    if isinstance(entity, types.Channel):
        return bool(getattr(entity, "megagroup", False))

    return False


async def get_group_members_count(client, entity) -> int | None:
    """
    Returns member count for:
      - normal group: types.Chat
      - supergroup: types.Channel with megagroup=True

    Returns None for channels/users/unknown.
    """

    if not is_group_or_supergroup(entity):
        return None

    # Normal small group
    if isinstance(entity, types.Chat):
        count = getattr(entity, "participants_count", None)

        if count is not None:
            return count

        full = await client(GetFullChatRequest(entity.id))
        participants = getattr(full.full_chat, "participants", None)

        if participants and getattr(participants, "participants", None):
            return len(participants.participants)

        return None

    # Supergroup
    if isinstance(entity, types.Channel) and getattr(entity, "megagroup", False):
        full = await client(GetFullChannelRequest(entity))
        return getattr(full.full_chat, "participants_count", None)

    return None


async def main():
    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    await client.start()
    print("Logged in.")
    sent = 0
    n = 0
    members = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        is_normal_group = isinstance(entity, types.Chat)
        is_supergroup = (
            isinstance(entity, types.Channel)
            and getattr(entity, "megagroup", False)
        )

        if is_normal_group or is_supergroup:
            group_type = "supergroup" if is_supergroup else "group"
            n = n + 1
            print("=" * 60)
            print(f"Title: {dialog.name}")
            print(f"Type: {group_type}")
            print(f"ID: {entity.id}")
            print(f"Peer ID: {dialog.id}")
            print(f"Username: {getattr(entity, 'username', None)}")
            print(f"Megagroup: {getattr(entity, 'megagroup', None)}")
            try:
                await client.send_message(
                    dialog.id,
                    MESSAGE,
                    link_preview=False,
                )
                sent = sent + 1
                members = members + await get_group_members_count(client, entity)
                print("Sent.")
            except Exception as e:
                print(e)

    print(f"{sent}/{n} sent {members} members")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())