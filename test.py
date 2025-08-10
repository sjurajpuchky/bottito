import asyncio
import os

import dotenv
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


dotenv.load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot_app = Application.builder().token(BOT_TOKEN).build()


asyncio.run(bot_app.bot.send_message("6371847519", "test"))
