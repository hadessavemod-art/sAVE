"""Standalone Telegram Business bot. Partial port of tgBotSaveMod, not full Laravel parity."""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
DB_PATH = Path(os.getenv('DB_PATH', 'bot.sqlite3'))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('business_bot')
dp = Dispatcher()


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS messages (connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sender_id INTEGER, text TEXT, content_type TEXT, raw_json TEXT NOT NULL, edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))''')
    connection.commit()
    return connection


async def alert(bot: Bot, message: str):
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, message[:4096])
        except Exception:
            logger.exception('Cannot notify admin')


@dp.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return
    with db() as con:
        con.execute('INSERT INTO users(id,username,first_name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',
                    (message.from_user.id, message.from_user.username, message.from_user.first_name))
    await message.answer('Привет! 👋 Я бот для Telegram Business. Подключи меня в настройках Telegram Business, чтобы сохранять историю сообщений.')


@dp.message(Command('help'))
async def help_command(message: Message):
    await message.answer('Команды: /start, /help. Администратору доступна /stats. Бот сохраняет Business-сообщения, их изменения и отметки об удалении.')


@dp.message(Command('stats'))
async def stats(message: Message):
    if not message.from_user or not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    with db() as con:
        users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        connections = con.execute('SELECT COUNT(*) FROM connections').fetchone()[0]
        messages = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0]
    await message.answer(f'📊 Пользователи: {users}\nBusiness-подключения: {connections}\nСообщения: {messages}\nУдалённые: {deleted}')


@dp.business_connection()
async def business_connection(event: BusinessConnection, bot: Bot):
    with db() as con:
        con.execute('''INSERT INTO connections(id,user_id,enabled) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP''',
                    (event.id, event.user.id, int(event.is_enabled)))
    await alert(bot, f'🔗 Business-подключение: {event.user.id}; включено: {event.is_enabled}')


def save_message(message: Message, edited=False):
    if not message.business_connection_id:
        return
    payload = message.model_dump_json(exclude_none=True)
    text = message.text or message.caption or ''
    with db() as con:
        con.execute('''INSERT INTO messages(connection_id,chat_id,message_id,sender_id,text,content_type,raw_json,edited) VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET sender_id=excluded.sender_id,text=excluded.text,content_type=excluded.content_type,raw_json=excluded.raw_json,edited=excluded.edited,updated_at=CURRENT_TIMESTAMP''',
          (message.business_connection_id, message.chat.id, message.message_id,
           message.from_user.id if message.from_user else None, text, message.content_type, payload, int(edited)))


@dp.business_message()
async def business_message(message: Message):
    save_message(message)


@dp.edited_business_message()
async def edited_business_message(message: Message, bot: Bot):
    save_message(message, edited=True)
    await alert(bot, f'✏️ Изменено Business-сообщение: чат {message.chat.id}, № {message.message_id}')


@dp.deleted_business_messages()
async def deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot):
    with db() as con:
        for message_id in event.message_ids:
            con.execute('''INSERT INTO messages(connection_id,chat_id,message_id,raw_json,deleted) VALUES(?,?,?,?,1)
            ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET deleted=1,updated_at=CURRENT_TIMESTAMP''',
            (event.business_connection_id, event.chat.id, message_id, '{}'))
    await alert(bot, f'🗑 Удалены сообщения: чат {event.chat.id}, номера {", ".join(map(str,event.message_ids))}')


async def main():
    if not TOKEN:
        raise SystemExit('Укажи BOT_TOKEN в переменных окружения')
    with db():
        pass
    bot = Bot(TOKEN)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
