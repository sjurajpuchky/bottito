#!/usr/bin/env python3
import argparse
import asyncio
import json
import re
from telethon import TelegramClient
from telethon.tl.types import ChannelParticipantsAdmins

def normalize_chat(s: str) -> str:
    s = s.strip()
    # Accept: @group, groupname, https://t.me/groupname, t.me/groupname
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(t\.me|telegram\.me|telegram\.dog)/", "", s)
    s = s.lstrip("@")
    # NOTE: invite links like t.me/+HASH or joinchat/HASH require you to be a member first.
    return s

async def main():
    ap = argparse.ArgumentParser(description="List Telegram group admins (user client)")
    ap.add_argument("--api-id", type=int, required=True)
    ap.add_argument("--api-hash", required=True)
    ap.add_argument("--session", default="tg_session", help="Session file name")
    ap.add_argument("--chat", required=True, help="Group @username or t.me link")
    ap.add_argument("--out", default="admins.json", help="Output JSON file")
    args = ap.parse_args()

    chat = normalize_chat(args.chat)

    async with TelegramClient(args.session, args.api_id, args.api_hash) as client:
        # You must be able to access the group (usually: be a member).
        entity = await client.get_entity(chat)

        admins = []
        async for u in client.iter_participants(entity, filter=ChannelParticipantsAdmins):
            admins.append({
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "phone": getattr(u, "phone", None),  # often None unless in your contacts
                "bot": u.bot,
            })

        # Print
        for a in admins:
            handle = f"@{a['username']}" if a["username"] else "(no username)"
            name = " ".join([x for x in [a["first_name"], a["last_name"]] if x]) or "(no name)"
            print(f"{name} {handle}  id={a['id']}  bot={a['bot']}")

        # Save
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(admins, f, ensure_ascii=False, indent=2)

        print(f"\nSaved {len(admins)} admins to {args.out}")

if __name__ == "__main__":
    asyncio.run(main())
