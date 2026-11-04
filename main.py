"""Standalone Telegram bot. Partial port of tgBotSaveMod, not full Laravel parity."""
import asyncio
import logging
import time
from collections import deque
import os
import sqlite3
from datetime import datetime, timezone, timedelta
import json
import re
from pathlib import Path
from contextlib import suppress
from urllib.parse import quote
from aiogram.types import CopyTextButton, WebAppInfo, InputMediaPhoto
import shutil
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, BusinessConnection, BusinessMessagesDeleted, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, 
    Update, BotCommand, ReplyKeyboardRemove, MenuButtonCommands, ReplyParameters
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '7831720836'))
# Строгий режим: реагируем на ответ только если сообщение защищённое или одноразовое.
CAPTURE_UNMARKED_REPLIED_MEDIA = False 
DB_PATH = Path(os.getenv('DB_PATH', 'bot.sqlite3'))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('business_bot')
BUILD_ID = 'HADES-ANTIMUTE-20261024-10'
dp = Dispatcher()

ADMIN_ACTIONS = {} 
BOT_USERNAME = ''

ANTIMUTE_ACTIVE: dict[tuple[str, int], float] = {}
ANTIMUTE_SENDS: dict[tuple[str, int], deque] = {}
ANTIMUTE_COPIES: dict[tuple[str, int, int], float] = {}
ANTIMUTE_LOCK = asyncio.Lock()
ANTIMUTE_DURATION_SECONDS = 60 * 60
ANTIMUTE_LIMIT_PER_MINUTE = 30
ANTIMUTE_COPY_MARKER_TTL = 120


def screen(title: str, subtitle: str = '') -> str:
    return f'<b>{title}</b>' + (f'\n<i>{subtitle}</i>' if subtitle else '')


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS messages (connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sender_id INTEGER, text TEXT, content_type TEXT, raw_json TEXT NOT NULL, edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))''')
    connection.execute('CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, rewarded INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS keyboard_cleanup (user_id INTEGER PRIMARY KEY)')
    connection.execute('CREATE TABLE IF NOT EXISTS premium (user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
    connection.execute('CREATE TABLE IF NOT EXISTS notification_prefs (user_id INTEGER PRIMARY KEY, edits INTEGER DEFAULT 1, deletions INTEGER DEFAULT 1, deleted_media INTEGER DEFAULT 1)')
    connection.execute('''CREATE TABLE IF NOT EXISTS ui_design (section TEXT PRIMARY KEY, photo_id TEXT, custom_text TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS admin_edit_state (user_id INTEGER PRIMARY KEY, section TEXT NOT NULL, mode TEXT NOT NULL)''')
    connection.execute('CREATE TABLE IF NOT EXISTS muted_chats (connection_id TEXT, chat_id INTEGER, PRIMARY KEY(connection_id,chat_id))')
    connection.execute('CREATE TABLE IF NOT EXISTS spam_cooldowns (connection_id TEXT PRIMARY KEY, last_at REAL)')
    connection.execute('CREATE TABLE IF NOT EXISTS media_files (connection_id TEXT,chat_id INTEGER,message_id INTEGER,local_path TEXT,PRIMARY KEY(connection_id,chat_id,message_id))')
    
    connection.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, role TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY, reason TEXT, banned_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, stars INTEGER, days INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')

    media_cols = {row[1] for row in connection.execute('PRAGMA table_info(media_files)')}
    if 'file_id' not in media_cols:
        connection.execute('ALTER TABLE media_files ADD COLUMN file_id TEXT')
    connection.execute('''CREATE TABLE IF NOT EXISTS media_diagnostics (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, message_id INTEGER, kind TEXT, status TEXT, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    diagnostic_cols = {row[1] for row in connection.execute('PRAGMA table_info(media_diagnostics)')}
    if 'connection_id' not in diagnostic_cols:
        connection.execute('ALTER TABLE media_diagnostics ADD COLUMN connection_id TEXT')
    connection.execute('''CREATE TABLE IF NOT EXISTS business_update_diagnostics (id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT, update_type TEXT NOT NULL, chat_id INTEGER, message_id INTEGER, content_type TEXT, has_reply INTEGER, has_media_file INTEGER, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_business_update_diagnostics_conn ON business_update_diagnostics(connection_id,id)')
    connection.execute('CREATE TABLE IF NOT EXISTS reply_captures (connection_id TEXT, chat_id INTEGER, message_id INTEGER, captured_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))')
    connection.execute('CREATE TABLE IF NOT EXISTS message_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,connection_id TEXT,chat_id INTEGER,message_id INTEGER,old_text TEXT,new_text TEXT,changed_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    
    cols = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
    for column in ('sender_name', 'sender_username', 'chat_title', 'chat_username'):
        if column not in cols:
            connection.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
    connection.commit()
    return connection


def is_owner_message(message: Message):
    if not message.business_connection_id or not message.from_user:
        return False
    with db() as con:
        owner = con.execute('SELECT user_id FROM connections WHERE id=?', (message.business_connection_id,)).fetchone()
    return bool(owner and message.from_user.id == owner['user_id'])


def save_message(message: Message, edited=False):
    # Сохраняем только входящие (не от владельца).
    if not message.business_connection_id or is_owner_message(message):
        return
    payload = message.model_dump_json(exclude_none=True)
    text = message.text or message.caption or ('🎭 Стикер' + (f' {message.sticker.emoji}' if message.sticker.emoji else '') if message.sticker else '')
    with db() as con:
        con.execute('''INSERT INTO messages(connection_id,chat_id,message_id,sender_id,text,content_type,raw_json,edited) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET sender_id=excluded.sender_id,text=excluded.text,content_type=excluded.content_type,raw_json=excluded.raw_json,edited=MAX(messages.edited,excluded.edited),updated_at=CURRENT_TIMESTAMP''', (message.business_connection_id, message.chat.id, message.message_id, message.from_user.id if message.from_user else None, text, message.content_type, payload, int(edited)))
        sender = message.from_user
        name = (' '.join(filter(None, (sender.first_name, sender.last_name))).strip() if sender else (message.sender_business_bot.username if message.sender_business_bot else None))
        username = (sender.username if sender else None) or (message.chat.username if message.chat.type == 'private' else None)
        con.execute('''UPDATE messages SET sender_name=COALESCE(?,sender_name), sender_username=COALESCE(?,sender_username),chat_title=COALESCE(?,chat_title), chat_username=COALESCE(?,chat_username) WHERE connection_id=? AND chat_id=? AND message_id=?''', (name,username,message.chat.full_name or message.chat.title, message.chat.username,message.business_connection_id,message.chat.id,message.message_id))


def is_premium(user_id):
    with db() as con:
        row = con.execute('SELECT expires_at FROM premium WHERE user_id=?', (user_id,)).fetchone()
        if row:
            try:
                return datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc)
            except Exception:
                pass
    return False


@dp.update.outer_middleware()
async def trace_business_updates(handler, event: Update, data):
    kind = event.event_type
    if kind in ('business_connection', 'business_message',
                'edited_business_message', 'deleted_business_messages'):
        obj = getattr(event, kind, None)
        connection_id = getattr(obj, 'business_connection_id', None)
        if kind == 'business_connection':
            connection_id = getattr(obj, 'id', None)
        chat = getattr(obj, 'chat', None)
        media = ('photo', 'video', 'video_note', 'voice', 'animation',
                 'audio', 'document', 'sticker')
        content_type = getattr(obj, 'content_type', None)
        has_file = int(any(getattr(obj, name, None) for name in media)) if kind.endswith('business_message') else 0
        replied = getattr(obj, 'reply_to_message', None)
        details = ''
        if kind == 'business_connection':
            details = ('enabled=' + str(getattr(obj, 'is_enabled', '?')) +
                       '; can_reply=' + str(getattr(getattr(obj, 'rights', None), 'can_reply', '?')) +
                       '; chat_access=' + str(getattr(obj, 'can_reply', '?')))
        elif kind == 'deleted_business_messages':
            details = 'deleted_count=' + str(len(getattr(obj, 'message_ids', []) or []))
        elif replied:
            details = ('reply_type=' + str(getattr(replied, 'content_type', '?')) +
                       '; reply_has_media=' + str(any(getattr(replied, name, None) for name in media)) +
                       '; reply_has_file_id=' + str(bool(getattr((replied.photo[-1] if replied.photo else (replied.video or replied.voice or replied.video_note or replied.document or replied.audio or replied.animation or replied.sticker)), 'file_id', None))))
        try:
            with db() as con:
                con.execute('''INSERT INTO business_update_diagnostics
                    (connection_id,update_type,chat_id,message_id,content_type,
                     has_reply,has_media_file,details)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (connection_id, kind, getattr(chat, 'id', None),
                     getattr(obj, 'message_id', None), content_type,
                     int(bool(replied)), has_file, details[:250]))
                con.execute('''DELETE FROM business_update_diagnostics WHERE id IN
                    (SELECT id FROM business_update_diagnostics ORDER BY id DESC LIMIT -1 OFFSET 2000)''')
        except Exception:
            logger.exception('Cannot store Business update diagnostic')
        logger.info('Business update received: type=%s connection=%s media=%s reply=%s',
                    kind, connection_id, bool(has_file), bool(replied))
    return await handler(event, data)


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
    from admin_bridge import is_blocked
    if is_blocked(message.from_user.id):
        return await message.answer('⛔ Доступ к боту ограничен.')
        
    with db() as con:
        banned = con.execute('SELECT reason FROM bans WHERE user_id=?', (message.from_user.id,)).fetchone()
        if banned:
            return await message.answer(f'⛔ Вы забанены.\nПричина: {banned["reason"]}')
            
        con.execute('INSERT INTO users(id,username,first_name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',
                    (message.from_user.id, message.from_user.username, message.from_user.first_name))
                    
    payload = (message.text or '').split(maxsplit=1)
    if len(payload) > 1 and payload[1].startswith('ref_'):
        try:
            referrer = int(payload[1][4:])
            if referrer != message.from_user.id:
                with db() as con:
                    existing = con.execute('SELECT 1 FROM referrals WHERE referred_id=?',(message.from_user.id,)).fetchone()
                    if not existing:
                        con.execute('INSERT INTO referrals(referred_id,referrer_id) VALUES(?,?)',(message.from_user.id,referrer))
                        try:
                            await message.bot.send_message(referrer, '👥 По вашей ссылке зарегистрировался новый пользователь! Подключите бота в настройках Telegram, чтобы получить Premium.')
                        except Exception:
                            pass
        except ValueError:
            pass
    with db() as con:
        was_removed = con.execute('SELECT 1 FROM keyboard_cleanup WHERE user_id=?',
                                  (message.from_user.id,)).fetchone()
    if not was_removed:
        await message.answer('✅ Клавиатура убрана. Команда /start теперь доступна через «Меню» возле поля ввода.',
                             reply_markup=ReplyKeyboardRemove())
        with db() as con:
            con.execute('INSERT OR IGNORE INTO keyboard_cleanup(user_id) VALUES (?)',
                        (message.from_user.id,))
    await send_home(message)


@dp.message(Command('help'))
async def help_command(message: Message):
    await message.answer('Команды: /start, /help, /archive, /ref, /premium, /settings, /mediacheck, /clear. В подключённом чате: .antimute, .antimute on, .antimute off, .antimute status. Администратору: /admin.')


@dp.message(Command('clear'))
async def clear_archive_cmd(message: Message):
    if not message.from_user: return
    with db() as con:
        con.execute('DELETE FROM messages WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (message.from_user.id,))
        con.execute('DELETE FROM media_files WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (message.from_user.id,))
        con.commit()
    await message.answer('🗑 <b>Архив полностью очищен!</b>', parse_mode='HTML')


MEDIA_DIR = Path(os.getenv('MEDIA_DIR') or ('/app/data/media' if Path('/app/data').is_dir() else str(Path(__file__).resolve().parent / 'media'))).resolve()

async def store_media(message: Message, bot: Bot) -> bool:
    if not message.business_connection_id or is_owner_message(message):
        return False

    item = (message.photo[-1] if message.photo else message.video or message.document or
            message.audio or message.voice or message.video_note or message.animation or message.sticker)
    if not item or not getattr(item, 'file_id', None):
        return False

    connection_id = message.business_connection_id
    chat_id = message.chat.id
    message_id = message.message_id
    file_id = item.file_id
    suffix = ('.jpg' if message.photo else '.mp4' if message.video or message.video_note else
              '.ogg' if message.voice else '.gif' if message.animation else
              ('.tgs' if message.sticker.is_animated else '.webm' if message.sticker.is_video else '.webp') if message.sticker else
              Path(getattr(item, 'file_name', '') or '').suffix or '.bin')
    folder = MEDIA_DIR / str(connection_id) / str(chat_id)
    target = folder / f'{message_id}{suffix}'

    with db() as con:
        previous = con.execute('''SELECT local_path,file_id FROM media_files
            WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (connection_id, chat_id, message_id)).fetchone()
        old_path = previous['local_path'] if previous else None
        same_file = bool(previous and previous['file_id'] == file_id)
        if same_file and old_path and Path(old_path).is_file() and Path(old_path).stat().st_size > 0:
            media_diagnostic(message, media_kind(message) or 'media', 'already_cached_locally',
                             f'bytes={Path(old_path).stat().st_size}')
            return True
        if previous:
            con.execute('''UPDATE media_files SET file_id=?, local_path=?
                WHERE connection_id=? AND chat_id=? AND message_id=?''',
                (file_id, old_path if same_file else None, connection_id, chat_id, message_id))
        else:
            con.execute('''INSERT INTO media_files(connection_id,chat_id,message_id,local_path,file_id)
                VALUES(?,?,?,?,?)''', (connection_id, chat_id, message_id, None, file_id))

    file_size = getattr(item, 'file_size', None)
    
    owner_id = await business_owner(message)
    is_prem = is_premium(owner_id) if owner_id else False
    max_download_mb = 50 if is_prem else 20
    
    if file_size and file_size > max_download_mb * 1024 * 1024:
        media_diagnostic(message, media_kind(message) or 'media', 'file_id_only_size_limit',
                         f'bytes={file_size}; download_limit_mb={max_download_mb}')
        logger.info('Media exceeds configured Bot API download limit: chat=%s message=%s bytes=%s',
                    chat_id, message_id, file_size)
        return False

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        media_diagnostic(message, media_kind(message) or 'media', 'file_id_saved_disk_unavailable',
                         f'{type(exc).__name__}: {exc}')
        logger.exception('Cannot create media cache folder for chat=%s message=%s', chat_id, message_id)
        return False

    last_error = None
    for attempt in range(1, 4):
        temp = target.with_name(f'.{target.name}.{os.getpid()}.{id(asyncio.current_task())}.{attempt}.part')
        try:
            await bot.download(file_id, destination=temp, timeout=120, chunk_size=256 * 1024)
            if not temp.is_file() or temp.stat().st_size == 0:
                raise IOError('Telegram returned an empty media file')
            size = temp.stat().st_size
            os.replace(temp, target)
            with db() as con:
                con.execute('''UPDATE media_files SET local_path=?
                    WHERE connection_id=? AND chat_id=? AND message_id=? AND file_id=?''',
                    (str(target), connection_id, chat_id, message_id, file_id))
            if old_path and old_path != str(target) and Path(old_path).is_file():
                with suppress(OSError):
                    Path(old_path).unlink()
            media_diagnostic(message, media_kind(message) or 'media', 'cached_locally',
                             f'bytes={size}; attempt={attempt}')
            logger.info('Media cached locally: chat=%s message=%s bytes=%s attempt=%s',
                        chat_id, message_id, size, attempt)
            return True
        except Exception as exc:
            last_error = exc
            with suppress(OSError):
                if temp.exists():
                    temp.unlink()
            if attempt < 3:
                await asyncio.sleep(0.35 * attempt)

    details = f'{type(last_error).__name__}: {last_error}' if last_error else 'unknown download error'
    media_diagnostic(message, media_kind(message) or 'media', 'download_failed', details)
    logger.warning('Media download failed after retries: chat=%s message=%s; %s',
                   chat_id, message_id, details, exc_info=True)
    return False


AUTO_FORWARD_MEDIA = False

def media_diagnostic(message: Message, kind: str, status: str, details: str = ''):
    with db() as con:
        con.execute('INSERT INTO media_diagnostics(connection_id,chat_id,message_id,kind,status,details) VALUES(?,?,?,?,?,?)',
                    (message.business_connection_id, message.chat.id, message.message_id, kind, status, str(details)[:350]))


async def deliver_incoming_media_now(message: Message, bot: Bot, force: bool = False):
    if (not AUTO_FORWARD_MEDIA and not force) or is_owner_message(message):
        return
    if message.sticker:
        return False
    kind = media_kind(message)
    if not kind:
        return
    owner_id = await business_owner(message)
    if not owner_id:
        media_diagnostic(message, kind, 'no_business_owner')
        return

    bot_username = escape((await bot.get_me()).username or 'HadesSave_bot')
    caption = (
        f'<blockquote>🕵️‍♂️ Сохранено с помощью '
        f'<a href="https://t.me/{bot_username}">@{bot_username}</a>'
        f'</blockquote>'
    )

    item = (message.photo[-1] if message.photo else message.video or message.document or
            message.audio or message.voice or message.video_note or message.animation or message.sticker)
    if not item or not getattr(item, 'file_id', None):
        media_diagnostic(message, kind, 'missing_file_id')
        return
    try:
        fid = item.file_id
        if message.photo:
            await bot.send_photo(owner_id, fid, caption=caption, parse_mode='HTML')
        elif message.video:
            await bot.send_video(owner_id, fid, caption=caption, parse_mode='HTML')
        elif message.voice:
            await bot.send_voice(owner_id, fid, caption=caption, parse_mode='HTML')
        elif message.video_note:
            await bot.send_video_note(owner_id, fid)
            await bot.send_message(owner_id, caption, parse_mode='HTML')
        elif message.sticker:
            await bot.send_sticker(owner_id, fid)
            await bot.send_message(owner_id, caption, parse_mode='HTML')
        elif message.animation:
            await bot.send_animation(owner_id, fid, caption=caption, parse_mode='HTML')
        elif message.audio:
            await bot.send_audio(owner_id, fid, caption=caption, parse_mode='HTML')
        else:
            await bot.send_document(owner_id, fid, caption=caption, parse_mode='HTML')
        media_diagnostic(message, kind, 'sent_file_id')
        return True
    except Exception as e:
        media_diagnostic(message, kind, 'file_id_failed', type(e).__name__ + ': ' + str(e))
        logger.warning('Business media file_id send failed: %s', e)
    try:
        await store_media(message, bot)
    except Exception as exc:
        media_diagnostic(message, kind, 'download_failed', f'{type(exc).__name__}: {exc}')
        logger.exception('Business media fallback download failed')
    with db() as con:
        row = con.execute('SELECT local_path FROM media_files WHERE connection_id=? AND chat_id=? AND message_id=?',
                          (message.business_connection_id, message.chat.id, message.message_id)).fetchone()
    if row and row['local_path'] and Path(row['local_path']).is_file():
        from aiogram.types import FSInputFile
        file = FSInputFile(row['local_path'])
        try:
            if message.photo:
                await bot.send_photo(owner_id, file, caption=caption, parse_mode='HTML')
            elif message.video:
                await bot.send_video(owner_id, file, caption=caption, parse_mode='HTML')
            elif message.voice:
                await bot.send_voice(owner_id, file, caption=caption, parse_mode='HTML')
            elif message.video_note:
                await bot.send_video_note(owner_id, file)
                await bot.send_message(owner_id, caption, parse_mode='HTML')
            elif message.sticker:
                await bot.send_sticker(owner_id, file)
                await bot.send_message(owner_id, caption, parse_mode='HTML')
            elif message.animation:
                await bot.send_animation(owner_id, file, caption=caption, parse_mode='HTML')
            elif message.audio:
                await bot.send_audio(owner_id, file, caption=caption, parse_mode='HTML')
            else:
                await bot.send_document(owner_id, file, caption=caption, parse_mode='HTML')
            media_diagnostic(message, kind, 'sent_local_file')
            return True
        except Exception as e:
            media_diagnostic(message, kind, 'local_send_failed', type(e).__name__ + ': ' + str(e))
            logger.warning('Local Business media send failed: %s', e)
    media_diagnostic(message, kind, 'unavailable', 'No usable Bot API file')
    return False


async def deliver_archived_media(bot: Bot, owner_id: int, local_path, file_id,
                                 caption: str, content_type: str = 'document'):
    from aiogram.types import FSInputFile
    source = FSInputFile(Path(local_path)) if local_path and Path(local_path).is_file() else file_id
    if not source:
        return False
    try:
        kind = content_type or 'document'
        if kind == 'sticker':
            await bot.send_sticker(owner_id, source)
            await bot.send_message(owner_id, caption[:4096])
        elif kind == 'photo':
            await bot.send_photo(owner_id, source, caption=caption[:1024])
        elif kind == 'video':
            await bot.send_video(owner_id, source, caption=caption[:1024])
        elif kind == 'video_note':
            await bot.send_video_note(owner_id, source)
            await bot.send_message(owner_id, caption[:4096])
        elif kind == 'voice':
            await bot.send_voice(owner_id, source, caption=caption[:1024])
        elif kind == 'animation':
            await bot.send_animation(owner_id, source, caption=caption[:1024])
        elif kind == 'audio':
            await bot.send_audio(owner_id, source, caption=caption[:1024])
        else:
            await bot.send_document(owner_id, source, caption=caption[:1024])
        return True
    except Exception:
        logger.exception('Telegram could not send archived media (type=%s)', content_type)
        return False

def media_kind(message: Message):
    if message.sticker:
        return '🎭 Стикер'
    if message.photo:
        return '📸 Фото'
    if message.video_note:
        return '⭕ Видеокружок'
    if message.video:
        return '🎬 Видео'
    if message.animation:
        return '🎞 GIF'
    if message.document:
        return '📎 Документ'
    if message.voice:
        return '🎤 Голосовое'
    if message.audio:
        return '🎵 Аудио'
    return None


async def delete_business_command(bot: Bot, message: Message):
    with suppress(Exception):
        await bot.delete_business_messages(business_connection_id=message.business_connection_id,
                                           message_ids=[message.message_id])

async def business_owner(message: Message):
    with db() as con:
        row = con.execute('SELECT user_id FROM connections WHERE id=? AND enabled=1',
                          (message.business_connection_id,)).fetchone()
    return row['user_id'] if row else None

async def handle_business_command(message: Message, bot: Bot):
    owner = await business_owner(message)
    if owner is None or not message.from_user or message.from_user.id != owner:
        return False
    command = (message.text or '').strip()

    if re.match(r'^\.antimute(?:\s|$)', command, re.IGNORECASE):
        await delete_business_command(bot, message)
        parts = command.lower().split()
        action = parts[1] if len(parts) == 2 else ('' if len(parts) == 1 else 'invalid')
        if len(parts) > 2 or action not in ('', 'on', 'off', 'status'):
            notice = ('Использование:\n.antimute [on] — включить на 1 час\n'
                      '.antimute off — выключить\n.antimute status — проверить')
        else:
            key = (message.business_connection_id, message.chat.id)
            now = time.monotonic()
            async with ANTIMUTE_LOCK:
                expires = ANTIMUTE_ACTIVE.get(key, 0.0)
                if expires <= now:
                    ANTIMUTE_ACTIVE.pop(key, None)
                    expires = 0.0
                if action in ('', 'on'):
                    ANTIMUTE_ACTIVE[key] = now + ANTIMUTE_DURATION_SECONDS
                    ANTIMUTE_SENDS.pop(key, None)
                    notice = (
                        '🛡 <b>Антимут включён в этом чате.</b>\n\n'
                        '<code>.antimute</code> / <code>.antimute on</code> — включить на 1 час.\n'
                        '<code>.antimute off</code> — выключить; <code>.antimute status</code> — проверить.\n\n'
                        'Бот отправляет копию от вашего имени и удаляет оригинал только после успешной отправки.\n'
                        'Поддерживаются текст, одиночные фото, видео, GIF, файлы, аудио, войсы, кружки и стикеры.\n'
                        'Альбомы, пересланные и защищённые сообщения, другие типы и команды остаются как есть. '
                        'Ответы сохраняются, если исходное сообщение доступно.\n'
                        'Может помочь против некоторых ботов, но другой бот может удалить копию; гарантии нет.\n'
                        'Лимит — до 30 замен в минуту. После перезапуска режим выключается.'
                    )
                elif action == 'off':
                    ANTIMUTE_ACTIVE.pop(key, None)
                    ANTIMUTE_SENDS.pop(key, None)
                    notice = '🛡 Антимут выключен в этом чате.'
                else:
                    remaining = max(0, int(expires - now))
                    if remaining:
                        notice = f'🛡 Антимут включён в этом чате. Осталось примерно {max(1, (remaining + 59) // 60)} мин.'
                    else:
                        notice = '🛡 Антимут выключен в этом чате.'
        try:
            async with ANTIMUTE_LOCK:
                sent_notice = await bot.send_message(
                    message.chat.id, notice,
                    business_connection_id=message.business_connection_id,
                    parse_mode='HTML',
                )
                if getattr(sent_notice, 'message_id', None):
                    ANTIMUTE_COPIES[(message.business_connection_id, message.chat.id,
                                     sent_notice.message_id)] = time.monotonic() + ANTIMUTE_COPY_MARKER_TTL
        except Exception:
            logger.exception('Could not send antimute command result in chat=%s', message.chat.id)
        return True

    if command not in ('.info','.mute','.unmute') and not command.startswith('.spam '):
        return False
    await delete_business_command(bot,message)
    if command == '.info':
        user = ('@'+message.chat.username) if message.chat.username else 'не указан'
        await owner_alert(bot,message.business_connection_id,
                          f'👤 Данные собеседника\nTelegram ID: {message.chat.id}\nUsername: {user}')
    elif command in ('.mute','.unmute'):
        with db() as con:
            if command == '.mute':
                con.execute('INSERT OR IGNORE INTO muted_chats VALUES(?,?)',
                            (message.business_connection_id,message.chat.id))
            else:
                con.execute('DELETE FROM muted_chats WHERE connection_id=? AND chat_id=?',
                            (message.business_connection_id,message.chat.id))
        with suppress(Exception):
            await bot.send_message(message.chat.id,
                '🔕 Режим мута включён. Новые входящие будут удаляться.' if command=='.mute' else
                '🔔 Режим мута отключён.',business_connection_id=message.business_connection_id)
    else:
        match = re.fullmatch(r'\.spam\s+(\d+)\s+(.+)',command,re.S)
        if not match:
            return True
        count, body = int(match[1]),match[2].strip()
        is_prem = is_premium(owner)
        limit = 50 if is_prem else 20
        cooldown = 180 if is_prem else 300
        if not (1 <= count <= limit) or len(re.findall(r'\w+',body)) > 20:
            await owner_alert(bot,message.business_connection_id,
                              f'⚠️ Лимит .spam: до {limit} сообщений, не более 20 слов.')
            return True
        now = datetime.now(timezone.utc).timestamp()
        with db() as con:
            row = con.execute('SELECT last_at FROM spam_cooldowns WHERE connection_id=?',
                              (message.business_connection_id,)).fetchone()
            if row and now-row['last_at']<cooldown:
                remaining = int(cooldown-(now-row['last_at']))+1
                await owner_alert(bot,message.business_connection_id,
                                  f'⏳ Повторите .spam через {remaining} сек.')
                return True
            con.execute('INSERT OR REPLACE INTO spam_cooldowns VALUES(?,?)',
                        (message.business_connection_id,now))
        for i in range(count):
            try:
                await bot.send_message(message.chat.id,body,
                                       business_connection_id=message.business_connection_id)
            except Exception:
                logger.exception('Spam series interrupted after %s messages',i)
                break
            await asyncio.sleep(0.08)
    return True


def view_once_marker(message: Message, saved_json=None):
    ttl_keys = {
        'ttl', 'ttlseconds', 'ttlperiod', 'ttlperiodseconds', 'mediatl',
        'mediatlseconds', 'mediatlperiod', 'mediatlperiodseconds',
        'expireafter', 'expiresin', 'expireseconds', 'selfdestructtimer',
        'selfdestructseconds', 'selfdestructperiod', 'autodeleteperiod',
        'mediatimeout', 'viewonceexpiresin',
    }
    boolean_keys = {
        'viewonce', 'isviewonce', 'viewoncemedia', 'isonetime', 'onetime',
        'isselfdestructing', 'selfdestructing', 'isselfdestruct',
        'selfdestruct', 'selfdestructmedia', 'isephemeral', 'ephemeral',
    }

    def normalized(key) -> str:
        return re.sub(r'[^a-z0-9]', '', str(key).lower())

    def positive(value) -> bool:
        if value is True:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {'true', 'yes', 'on'}:
                return True
            try:
                return float(text) > 0
            except ValueError:
                return False
        return False

    seen = set()

    def scan(value, depth=0):
        if value is None or depth > 16:
            return None
        if not isinstance(value, (dict, list, tuple)):
            ident = id(value)
            if ident in seen:
                return None
            seen.add(ident)
            extra = getattr(value, 'model_extra', None)
            if isinstance(extra, dict):
                hit = scan(extra, depth + 1)
                if hit:
                    return hit
            dumper = getattr(value, 'model_dump', None)
            if callable(dumper):
                try:
                    value = dumper(exclude_none=True)
                except Exception:
                    return None
            else:
                return None
        if isinstance(value, dict):
            for key, child in value.items():
                key_norm = normalized(key)
                if key_norm in ttl_keys and positive(child):
                    return str(key)
                if key_norm in boolean_keys and positive(child):
                    return str(key)
            for child in value.values():
                if isinstance(child, (dict, list, tuple)) or hasattr(child, 'model_dump'):
                    hit = scan(child, depth + 1)
                    if hit:
                        return hit
            return None
        if isinstance(value, (list, tuple)):
            for child in value:
                hit = scan(child, depth + 1)
                if hit:
                    return hit
        return None

    candidates = [message]
    for field in ('photo', 'video', 'video_note', 'voice', 'document', 'audio', 'animation', 'sticker'):
        value = getattr(message, field, None)
        if value:
            candidates.extend(value if isinstance(value, (list, tuple)) else [value])
    for candidate in candidates:
        marker = scan(candidate)
        if marker:
            return True, marker
    if saved_json:
        try:
            payload = json.loads(saved_json) if isinstance(saved_json, str) else saved_json
            marker = scan(payload)
            if marker:
                return True, marker
        except (ValueError, TypeError, AttributeError):
            pass
    return False, None


def view_once_media(message: Message, saved_json=None) -> bool:
    detected, _marker = view_once_marker(message, saved_json)
    return detected


def raw_message_file_id(raw_json, content_type=None):
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = str(content_type or data.get('content_type') or '')
    value = data.get(kind)
    if kind == 'photo' and isinstance(value, list):
        photos = [x for x in value if isinstance(x, dict) and x.get('file_id')]
        if photos:
            photos.sort(key=lambda x: int(x.get('file_size') or 0))
            return photos[-1].get('file_id')
    if isinstance(value, dict) and value.get('file_id'):
        return value.get('file_id')
    for field in ('photo', 'video', 'video_note', 'voice', 'document', 'audio', 'animation', 'sticker'):
        candidate = data.get(field)
        if field == 'photo' and isinstance(candidate, list):
            photos = [x for x in candidate if isinstance(x, dict) and x.get('file_id')]
            if photos:
                photos.sort(key=lambda x: int(x.get('file_size') or 0))
                return photos[-1].get('file_id')
        elif isinstance(candidate, dict) and candidate.get('file_id'):
            return candidate.get('file_id')
    return None


async def capture_replied_media(message: Message, bot: Bot) -> bool:
    owner = await business_owner(message)
    replied = message.reply_to_message
    if not owner or not message.from_user or message.from_user.id != owner:
        return False
    if not replied:
        return False

    is_protected = getattr(replied, 'has_protected_content', False)
    item = (replied.photo[-1] if replied.photo else replied.video or replied.voice or
            replied.video_note or replied.document or replied.audio or replied.animation or replied.sticker)
    ttl = getattr(item, 'ttl_seconds', None)

    # Строгий режим: реагируем только на защищённые и одноразовые сообщения.
    if not is_protected and not ttl:
        media_diagnostic(message, 'reply', 'not_view_once_ignored',
                         f'type={replied.content_type}; protected={is_protected}; ttl={ttl}')
        return False

    if replied.sticker:
        return False
        
    connection_id = message.business_connection_id
    chat_id = message.chat.id
    replied_id = replied.message_id
    kind = media_kind(replied)
    with db() as con:
        saved = con.execute("""SELECT mf.local_path, mf.file_id, m.content_type, m.raw_json
            FROM messages m LEFT JOIN media_files mf
            ON mf.connection_id=m.connection_id AND mf.chat_id=m.chat_id
            AND mf.message_id=m.message_id
            WHERE m.connection_id=? AND m.chat_id=? AND m.message_id=?""",
            (connection_id, chat_id, replied_id)).fetchone()

    has_file_id = bool(getattr(item, 'file_id', None))
    has_local_copy = bool(saved and saved['local_path'] and Path(saved['local_path']).is_file())
    has_cached_file_id = bool(saved and saved['file_id'])
    
    media_diagnostic(message, 'reply', 'view_once_detected',
                     f'replied_id={replied_id}; protected={is_protected}; ttl={ttl}; file_id={has_file_id}')

    if kind and has_file_id and not has_local_copy:
        try:
            cache_message = replied.model_copy(update={'business_connection_id': connection_id})
            if not is_owner_message(cache_message):
                save_message(cache_message)
                await store_media(cache_message, bot)
                with db() as con:
                    saved = con.execute("""SELECT mf.local_path,mf.file_id,m.content_type,m.raw_json
                        FROM messages m LEFT JOIN media_files mf
                        ON mf.connection_id=m.connection_id AND mf.chat_id=m.chat_id
                        AND mf.message_id=m.message_id
                        WHERE m.connection_id=? AND m.chat_id=? AND m.message_id=?""",
                        (connection_id,chat_id,replied_id)).fetchone()
                has_local_copy = bool(saved and saved['local_path'] and Path(saved['local_path']).is_file())
                has_cached_file_id = bool(saved and saved['file_id'])
        except Exception:
            logger.exception('Could not cache media supplied by reply payload')

    known_media_types = {'photo', 'video', 'video_note', 'voice', 'document', 'audio', 'animation', 'sticker'}
    
    if not kind and not (saved and saved['content_type'] in known_media_types):
        media_diagnostic(message, 'reply', 'no_media_in_reply',
                         f'replied_id={replied_id}; type={replied.content_type}; cached={bool(saved)}')
        return False

    if saved and saved['content_type'] == 'sticker':
        return False

    with db() as con:
        completed = con.execute(
            'SELECT 1 FROM reply_captures WHERE connection_id=? AND chat_id=? AND message_id=?',
            (connection_id, chat_id, message.message_id)).fetchone()
    if completed:
        return True

    if kind:
        try:
            replied = replied.model_copy(update={'business_connection_id': connection_id})
            if not is_owner_message(replied):
                save_message(replied)
            
            delivered = await deliver_incoming_media_now(replied, bot, force=True)
            if not is_owner_message(replied):
                await store_media(replied, bot)
                
            if delivered:
                with db() as con:
                    con.execute('INSERT OR IGNORE INTO reply_captures(connection_id,chat_id,message_id) VALUES(?,?,?)',
                                (connection_id, chat_id, message.message_id))
        except Exception:
            logger.exception('Reply media capture failed')
        return True

    if saved and (saved['file_id'] or saved['local_path']):
        from aiogram.types import FSInputFile
        src = (FSInputFile(saved['local_path']) if saved['local_path']
               and Path(saved['local_path']).is_file() else saved['file_id'])
        if src:
            try:
                category = saved['content_type']
                if category == 'photo':
                    await bot.send_photo(owner, src)
                elif category == 'video':
                    await bot.send_video(owner, src)
                elif category == 'video_note':
                    await bot.send_video_note(owner, src)
                elif category == 'voice':
                    await bot.send_voice(owner, src)
                elif category == 'animation':
                    await bot.send_animation(owner, src)
                elif category == 'audio':
                    await bot.send_audio(owner, src)
                else:
                    await bot.send_document(owner, src)
                with db() as con:
                    con.execute('INSERT OR IGNORE INTO reply_captures(connection_id,chat_id,message_id) VALUES(?,?,?)',
                                (connection_id, chat_id, message.message_id))
                return True
            except Exception as exc:
                logger.warning('Unable to send replied media from cache: %s', exc)
    return True

def _antimute_supported(message: Message) -> bool:
    if getattr(message, 'media_group_id', None):
        return False
    if getattr(message, 'forward_origin', None) or getattr(message, 'forward_from', None) or getattr(message, 'forward_from_chat', None):
        return False
    if getattr(message, 'is_automatic_forward', False) or getattr(message, 'has_protected_content', False):
        return False
    content = (message.text or message.caption or '').lstrip()
    if content.startswith(('.', '/')):
        return False
    return bool(
        message.text or message.photo or message.video or message.animation or
        message.document or message.audio or message.voice or message.video_note or
        message.sticker
    )


async def _send_antimute_copy(message: Message, bot: Bot, *, include_reply: bool = True) -> Message:
    connection_id = message.business_connection_id
    common = {'chat_id': message.chat.id, 'business_connection_id': connection_id}
    if include_reply and message.reply_to_message:
        common['reply_parameters'] = ReplyParameters(
            message_id=message.reply_to_message.message_id,
            allow_sending_without_reply=False,
        )

    if message.text:
        return await bot.send_message(
            **common, text=message.text, entities=message.entities or None,
        )

    caption_args = {}
    if message.caption is not None:
        caption_args['caption'] = message.caption
    if message.caption_entities:
        caption_args['caption_entities'] = message.caption_entities

    if message.photo:
        return await bot.send_photo(**common, photo=message.photo[-1].file_id, **caption_args)
    if message.video:
        return await bot.send_video(**common, video=message.video.file_id, **caption_args)
    if message.animation:
        return await bot.send_animation(**common, animation=message.animation.file_id, **caption_args)
    if message.document:
        return await bot.send_document(**common, document=message.document.file_id, **caption_args)
    if message.audio:
        return await bot.send_audio(**common, audio=message.audio.file_id, **caption_args)
    if message.voice:
        return await bot.send_voice(**common, voice=message.voice.file_id, **caption_args)
    if message.video_note:
        return await bot.send_video_note(**common, video_note=message.video_note.file_id)
    if message.sticker:
        return await bot.send_sticker(**common, sticker=message.sticker.file_id)
    raise ValueError(f'Unsupported antimute message type: {message.content_type}')


async def _copy_owner_message_for_antimute(message: Message, bot: Bot) -> bool:
    if not message.business_connection_id or not is_owner_message(message):
        return False
    key = (message.business_connection_id, message.chat.id)
    message_key = (*key, message.message_id)

    async with ANTIMUTE_LOCK:
        now = time.monotonic()
        for old_key, expiry in list(ANTIMUTE_COPIES.items()):
            if expiry <= now:
                ANTIMUTE_COPIES.pop(old_key, None)
        if message_key in ANTIMUTE_COPIES:
            ANTIMUTE_COPIES.pop(message_key, None)
            return True

        expires = ANTIMUTE_ACTIVE.get(key, 0.0)
        if expires <= now:
            ANTIMUTE_ACTIVE.pop(key, None)
            return False
        if not _antimute_supported(message):
            return False

        recent = ANTIMUTE_SENDS.setdefault(key, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if len(recent) >= ANTIMUTE_LIMIT_PER_MINUTE:
            logger.warning('Antimute rate limit reached: connection=%s chat=%s', key[0], key[1])
            return True

        try:
            try:
                sent = await _send_antimute_copy(message, bot, include_reply=True)
            except Exception as exc:
                error = str(exc).lower()
                reply_missing = message.reply_to_message and any(
                    phrase in error for phrase in (
                        'reply message not found', 'message to be replied not found',
                        'replied message not found', 'message to reply not found',
                    )
                )
                if not reply_missing:
                    raise
                sent = await _send_antimute_copy(message, bot, include_reply=False)

            if not sent or not getattr(sent, 'message_id', None):
                logger.error('Antimute copy returned no confirmed Message: chat=%s id=%s', key[1], message.message_id)
                return True

            recent.append(time.monotonic())
            ANTIMUTE_COPIES[(*key, sent.message_id)] = time.monotonic() + ANTIMUTE_COPY_MARKER_TTL
            try:
                deleted = await bot.delete_business_messages(
                    business_connection_id=message.business_connection_id,
                    message_ids=[message.message_id],
                )
                if not deleted:
                    logger.warning('Antimute copy sent but original deletion was not confirmed: chat=%s id=%s',
                                   message.chat.id, message.message_id)
            except Exception:
                logger.exception('Antimute copy sent, but original deletion failed: chat=%s id=%s',
                                 message.chat.id, message.message_id)
            logger.info('Antimute replacement sent: connection=%s chat=%s original=%s replacement=%s',
                        key[0], key[1], message.message_id, sent.message_id)
            return True
        except Exception:
            logger.exception('Antimute replacement failed; original remains: connection=%s chat=%s id=%s',
                             key[0], key[1], message.message_id)
            return True


@dp.business_message()
async def business_message(message: Message, bot: Bot):
    if not is_owner_message(message):
        save_message(message)
        if not media_kind(message):
            hints = []
            for field in ('has_media_spoiler', 'ttl_seconds', 'view_once'):
                value = getattr(message, field, None)
                if value is not None:
                    hints.append(f'{field}={value}')
            media_diagnostic(message, 'incoming', 'no_media_payload',
                             f'type={message.content_type}; ' + ','.join(hints))
        if media_kind(message):
            item = (message.photo[-1] if message.photo else message.video or message.voice or
                    message.video_note or message.document or message.audio or message.animation or message.sticker)
            media_diagnostic(message, media_kind(message) or 'media', 'incoming_media_seen',
                             f'type={message.content_type}; file_id_available={bool(getattr(item, "file_id", None))}; '
                             f'file_size={getattr(item, "file_size", None)}')
            cached = await store_media(message, bot)
            if not cached:
                item = (message.photo[-1] if message.photo else message.video or message.voice or
                        message.video_note or message.document or message.audio or message.animation or message.sticker)
                media_diagnostic(message, media_kind(message) or 'media', 'incoming_media_not_cached',
                                 f'type={message.content_type}; file_id_available={bool(getattr(item, "file_id", None))}; '
                                 f'file_size={getattr(item, "file_size", None)}')
    if await handle_business_command(message, bot):
        return
    if is_owner_message(message) and await _copy_owner_message_for_antimute(message, bot):
        return
    if is_owner_message(message) and message.reply_to_message:
        await capture_replied_media(message, bot)
    with db() as con:
        muted = con.execute('SELECT 1 FROM muted_chats WHERE connection_id=? AND chat_id=?',
                            (message.business_connection_id,message.chat.id)).fetchone()
    owner = await business_owner(message)
    if muted and owner and (not message.from_user or message.from_user.id != owner):
        await delete_business_command(bot,message)


def quote_block(value, placeholder='Сообщение без текста', limit=1500):
    text = str(value or placeholder).strip()[:limit]
    return '<blockquote>' + escape(text) + '</blockquote>'


def user_card(row, fallback_chat=None):
    label = identity_label(row, fallback_chat)
    uid = row['sender_id'] if row is not None and 'sender_id' in row.keys() else None
    username = (row['sender_username'] or row['chat_username']) if row is not None else None
    name = escape(label)
    if username:
        username = str(username).lstrip('@')
        return f'<b>{name}</b> · <a href="https://t.me/{escape(username, quote=True)}">Открыть профиль ↗</a>'
    if uid and int(uid) > 0:
        return f'<a href="tg://user?id={int(uid)}">{name}</a>'
    return f'<b>{name}</b>'


def get_design(section):
    with db() as con:
        row = con.execute('SELECT photo_id,custom_text FROM ui_design WHERE section=?', (section,)).fetchone()
    return dict(row) if row else {'photo_id': None, 'custom_text': None}


def render_design(section, default_text):
    design = get_design(section)
    return design, design['custom_text'] if design['custom_text'] is not None else default_text


async def show_screen(callback, text, markup, section=None):
    if not callback.message:
        return
    design, shown = render_design(section, text) if section else ({'photo_id': None}, text)
    msg = callback.message
    has_photo = bool(getattr(msg, 'photo', None))
    photo_id = design['photo_id']
    try:
        if photo_id:
            caption = shown[:1024]
            if has_photo:
                await msg.edit_media(media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode='HTML'), reply_markup=markup)
            else:
                await msg.delete()
                await msg.answer_photo(photo_id, caption=caption, parse_mode='HTML', reply_markup=markup)
        elif has_photo:
            await msg.edit_caption(caption=shown[:1024], parse_mode='HTML', reply_markup=markup)
        else:
            await msg.edit_text(shown[:4096], reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower():
            logger.exception('Cannot edit menu screen')


async def send_home(message):
    name = escape(message.from_user.first_name or 'друг') if message.from_user else 'друг'
    default = (
        f'<b>HADES SAVE</b> <i>— Твой цифровой сейф</i>\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'👋 Привет, <b>{name}</b>!\n'
        f'Я сохраняю историю переписок, даже если собеседник удалил сообщение.\n\n'
        f'<blockquote>🔸 <b>Управляй ботом</b> через кнопки ниже\n'
        f'🔸 <b>Смотри архив</b> в красивом Mini App</blockquote>\n\n'
        f'<i>Выбери действие:</i>'
    )
    design, text = render_design('home', default)
    if design['photo_id']:
        await message.answer_photo(design['photo_id'], caption=text[:1024], parse_mode='HTML', reply_markup=menu(message.from_user.id))
    else:
        await message.answer(text[:4096], reply_markup=menu(message.from_user.id), parse_mode='HTML')


def identity_label(row, fallback_chat=None):
    if row is None:
        return f'собеседник (чат {fallback_chat})' if fallback_chat else 'неизвестен'
    name = row['sender_name'] or (str(row['sender_id']) if row['sender_id'] else None)
    username = row['sender_username'] or row['chat_username']
    if not name:
        name = row['chat_title'] or (str(fallback_chat) if fallback_chat else 'неизвестен')
    if username:
        mention = '@' + username.lstrip('@')
        return name if mention in name else f'{name} ({mention})'
    return name


def deleted_keyboard(chat_id, username=None, restore=None):
    if username:
        url = 'https://t.me/' + str(username).lstrip('@')
    elif chat_id and int(chat_id) > 0:
        url = f'tg://user?id={int(chat_id)}'
    else:
        url = None
    row = []
    if url:
        row.append(InlineKeyboardButton(text='💬 Перейти в чат', url=url))
    if restore:
        row.append(InlineKeyboardButton(text='↩️ Вернуть в чат', callback_data=restore))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def describe_content(message: Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    if message.sticker:
        sticker = message.sticker
        return '🎭 Стикер' + (f' {sticker.emoji}' if sticker.emoji else '')
    names = {
        'photo': '📸 Фотография', 'video': '🎬 Видео',
        'video_note': '⭕ Видеокружок', 'voice': '🎤 Голосовое сообщение',
        'animation': '🎞 GIF', 'audio': '🎵 Аудио',
        'document': '📎 Документ', 'contact': '👤 Контакт',
        'location': '📍 Геолокация', 'poll': '📊 Опрос',
    }
    return names.get(str(message.content_type), f'📩 Сообщение ({message.content_type})')


@dp.edited_business_message()
async def edited_business_message(message: Message, bot: Bot):
    if is_owner_message(message):
        return
    with db() as con:
        old = con.execute('''SELECT text,sender_id,sender_name,sender_username,chat_title,chat_username
            FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (message.business_connection_id,message.chat.id,message.message_id)).fetchone()
    old_text = old['text'] if old else None
    old_type = None
    if old is not None:
        with db() as con:
            previous = con.execute('SELECT content_type FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?',
                (message.business_connection_id, message.chat.id, message.message_id)).fetchone()
        old_type = previous['content_type'] if previous else None
    save_message(message, edited=True)
    new_text = describe_content(message)
    if old is not None and not old_text:
        old_text = {'sticker': '🎭 Стикер', 'photo': '📸 Фотография',
                    'video': '🎬 Видео', 'video_note': '⭕ Видеокружок',
                    'voice': '🎤 Голосовое сообщение', 'animation': '🎞 GIF',
                    'document': '📎 Документ', 'audio': '🎵 Аудио'}.get(old_type, None)
    if old is None:
        old_text = 'Не удалось получить исходное сообщение: бот не видел его до редактирования'
    with db() as con:
        con.execute('INSERT INTO message_versions(connection_id,chat_id,message_id,old_text,new_text) VALUES(?,?,?,?,?)',
                    (message.business_connection_id,message.chat.id,message.message_id,old_text,new_text))
        current = con.execute('''SELECT sender_id,sender_name,sender_username,chat_title,chat_username
            FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (message.business_connection_id,message.chat.id,message.message_id)).fetchone()
        owner = con.execute('SELECT user_id FROM connections WHERE id=?',(message.business_connection_id,)).fetchone()
    await store_media(message,bot)
    if owner and prefs(owner['user_id'])['edits']:
        author = user_card(current, message.chat.id)
        body = (
            f'📝 <b>СООБЩЕНИЕ ИЗМЕНЕНО</b>\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'👤 <b>Отправитель:</b> {author}\n\n'
            f'🔴 <b>Было:</b>\n{quote_block(old_text, "Исходный текст недоступен")}\n\n'
            f'🟢 <b>Стало:</b>\n{quote_block(new_text)}'
        )
        await owner_alert(bot,message.business_connection_id,body,parse_mode='HTML',
                          reply_markup=deleted_keyboard(message.chat.id,
                              (current['chat_username'] or current['sender_username']) if current else None))


@dp.deleted_business_messages()
async def deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot):
    logger.info(f"!!! DELETED EVENT RECEIVED: connection={event.business_connection_id}, chat={event.chat.id}, msg_ids={event.message_ids}")
    with db() as con:
        for message_id in event.message_ids:
            con.execute('''UPDATE messages SET deleted=1,updated_at=CURRENT_TIMESTAMP
                WHERE connection_id=? AND chat_id=? AND message_id=?''',
                (event.business_connection_id,event.chat.id,message_id))
        owner = con.execute('SELECT user_id FROM connections WHERE id=?',(event.business_connection_id,)).fetchone()
        rows = con.execute('''SELECT m.message_id,m.text,m.content_type,m.sender_id,m.sender_name,
            m.sender_username,m.chat_title,m.chat_username,m.raw_json, mf.local_path,mf.file_id
            FROM messages m LEFT JOIN media_files mf
            ON mf.connection_id=m.connection_id AND mf.chat_id=m.chat_id AND mf.message_id=m.message_id
            WHERE m.connection_id=? AND m.chat_id=? AND m.message_id IN ('''+
            ','.join('?' for _ in event.message_ids)+')',
            (event.business_connection_id,event.chat.id,*event.message_ids)).fetchall() if event.message_ids else []
    if not owner or not prefs(owner['user_id'])['deletions']:
        return
    
    for row in rows:
        # Не отправляем уведомление, если удалил сам владелец.
        if row['sender_id'] is None or row['sender_id'] == owner['user_id']:
            continue
        sender = identity_label(row,event.chat.id)
        author = user_card(row,event.chat.id)
        content = row['text'] or ({'sticker': '🎭 Стикер', 'photo': '📸 Фото', 'video': '🎬 Видео', 'voice': '🎤 Голосовое', 'video_note': '⭕ Видеокружок'}.get(row['content_type'], '[' + (row['content_type'] or 'сообщение') + ']'))
        restore_key = f"restore:{row['message_id']}:{event.chat.id}"
        keyboard = deleted_keyboard(event.chat.id, row['chat_username'] or row['sender_username'], restore_key)
        ct = row['content_type']
        available_file_id = row['file_id'] or raw_message_file_id(row['raw_json'], ct)
        media_present = bool((row['local_path'] and Path(row['local_path']).is_file()) or available_file_id)
        
        body = (
            f'🚨 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'👤 <b>Отправитель:</b> {author}\n'
            f'🕒 <b>Время:</b> {datetime.now().strftime("%H:%M")}\n\n'
            f'{quote_block(content,limit=2800)}'
        )
        
        if ct in ('photo','video','voice','video_note','sticker','animation','document','audio') and media_present:
            from aiogram.types import FSInputFile
            src = (FSInputFile(row['local_path']) if row['local_path'] and Path(row['local_path']).is_file() else available_file_id)
            if src and prefs(owner['user_id'])['deleted_media']:
                try:
                    kw = dict(reply_markup=keyboard)
                    if ct == 'photo':
                        await bot.send_photo(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'video':
                        await bot.send_video(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'voice':
                        await bot.send_voice(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'animation':
                        await bot.send_animation(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'document':
                        await bot.send_document(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'audio':
                        await bot.send_audio(owner['user_id'],src,caption=body,parse_mode='HTML',**kw)
                    elif ct == 'video_note':
                        await bot.send_video_note(owner['user_id'],src)
                        await bot.send_message(owner['user_id'],body,reply_markup=keyboard,parse_mode='HTML')
                    else:
                        await bot.send_sticker(owner['user_id'],src)
                        await bot.send_message(owner['user_id'],body,reply_markup=keyboard,parse_mode='HTML')
                    continue
                except Exception:
                    logger.exception('Could not deliver deleted media; using text notification')
        try:
            await bot.send_message(owner['user_id'],body,reply_markup=keyboard,
                                   parse_mode='HTML',disable_web_page_preview=True)
        except Exception:
            logger.exception('Cannot notify about deletion')


@dp.callback_query(F.data.startswith('restore:'))
async def restore_deleted_message(callback: CallbackQuery, bot: Bot):
    try:
        _, message_id_raw, chat_id_raw = (callback.data or '').split(':', 2)
        message_id, chat_id = int(message_id_raw), int(chat_id_raw)
    except (ValueError, TypeError):
        await callback.answer('Неверные данные сообщения', show_alert=True)
        return
    with db() as con:
        row = con.execute("""SELECT m.connection_id,m.text,m.content_type,m.deleted,m.sender_id,m.raw_json,
            mf.local_path,mf.file_id FROM messages m
            JOIN connections c ON c.id=m.connection_id
            LEFT JOIN media_files mf ON mf.connection_id=m.connection_id
                AND mf.chat_id=m.chat_id AND mf.message_id=m.message_id
            WHERE c.user_id=? AND c.enabled=1 AND m.chat_id=? AND m.message_id=?
            AND m.deleted=1 ORDER BY m.updated_at DESC LIMIT 1""",
            (callback.from_user.id,chat_id,message_id)).fetchone()
    if not row or row['sender_id'] == callback.from_user.id:
        await callback.answer('Сообщение недоступно для восстановления',show_alert=True)
        return
    try:
        cid=row['connection_id']
        ct=row['content_type']
        cached_file_id = row['file_id'] or raw_message_file_id(row['raw_json'], row['content_type'])
        source = (row['local_path'] if row['local_path'] and Path(row['local_path']).is_file() else cached_file_id)
        if source and ct in ('photo','video','voice','video_note','sticker','animation','audio','document'):
            from aiogram.types import FSInputFile
            src=FSInputFile(source) if source == row['local_path'] else source
            caption=(row['text'] or '')[:1024] or None
            meth={'photo':bot.send_photo,'video':bot.send_video,'voice':bot.send_voice,
                  'video_note':bot.send_video_note,'sticker':bot.send_sticker,
                  'animation':bot.send_animation,'audio':bot.send_audio,'document':bot.send_document}[ct]
            arg={'photo':'photo','video':'video','voice':'voice','video_note':'video_note',
                 'sticker':'sticker','animation':'animation','audio':'audio','document':'document'}[ct]
            kwargs={'chat_id':chat_id,'business_connection_id':cid,arg:src}
            if caption and ct not in ('sticker','video_note'):
                kwargs['caption']=caption
            await meth(**kwargs)
        elif row['text']:
            await bot.send_message(chat_id,row['text'][:4096],business_connection_id=cid)
        else:
            await callback.answer('Нет доступной копии сообщения',show_alert=True)
            return
        await callback.answer('✅ Сообщение отправлено в чат',show_alert=True)
    except Exception:
        logger.exception('Failed to restore deleted message to chat')
        await callback.answer('Не удалось отправить. Проверь подключение и разрешения.',show_alert=True)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action) for label, action in row] for row in rows])


def menu(uid=None):
    rows = []
    if uid is not None:
        with db() as con:
            connected = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1', (uid,)).fetchone()[0]
        if not connected:
            rows.append([InlineKeyboardButton(text='🚀 Подключить HadesSave', callback_data='menu:setup')])
    
    web_app_url = "https://hadessavemod-art.github.io/hadesApp/" 
    
    rows.extend([
        [InlineKeyboardButton(text='📱 Открыть HadesSave', web_app=WebAppInfo(url=web_app_url))],
        [InlineKeyboardButton(text='🗑 Удалённые', callback_data='menu:deleted'), InlineKeyboardButton(text='ℹ️ Изменённые', callback_data='menu:edited')],
        [InlineKeyboardButton(text='💬 Команды', callback_data='menu:commands'), InlineKeyboardButton(text='📈 Статистика', callback_data='menu:statistics')],
        [InlineKeyboardButton(text='🎁 Пригласить друга', callback_data='menu:invite')],
        [InlineKeyboardButton(text='⚙️ Настройки', callback_data='menu:settings')],
        [InlineKeyboardButton(text='💎 HadesSave Premium', callback_data='menu:premium')],
        [InlineKeyboardButton(text='💡 Поддержка', callback_data='menu:support')],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def setup_keyboard(username):
    username = username or 'HadesSave_bot'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📋 Скопировать', copy_text=CopyTextButton(text='@' + username))],
        [InlineKeyboardButton(text='✅ Подключить', url='tg://settings')],
        *[([InlineKeyboardButton(text='❔ Помощь', url='https://t.me/' + os.getenv('MANAGER_USERNAME', '').strip().lstrip('@'))] if os.getenv('MANAGER_USERNAME', '').strip() else [])],
        [InlineKeyboardButton(text='✅ Я подключил', callback_data='menu:setup_check')],
        [InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')],
    ])


def command_keyboard():
    commands = ['info', 'mute', 'unmute', 'spam', 'archive', 'ref', 'settings', 'mediacheck']
    rows = []
    for i in range(0, len(commands), 3):
        rows.append([InlineKeyboardButton(text=('.' if cmd in ('info','mute','unmute','spam') else '/') + cmd,
                                        callback_data='cmd:' + cmd) for cmd in commands[i:i+3]])
    rows.append([InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


COMMAND_INFO = {
    'info': ('.info', 'Показывает информацию о собеседнике. Используй в переписке.'),
    'mute': ('.mute', 'Включает удаление новых входящих сообщений в выбранном чате.'),
    'unmute': ('.unmute', 'Выключает режим mute в выбранном чате.'),
    'spam': ('.spam [N] [текст]', 'Отправляет N копий текста. Лимит: 20 сообщений (Premium — 50), не более 20 слов; задержка перед повтором.'),
    'archive': ('/archive', 'Показывает последние 15 сообщений из твоего архива.'),
    'ref': ('/ref', 'Показывает реферальную ссылку и число приглашённых.'),
    'settings': ('/settings', 'Настройки уведомлений.'),
    'mediacheck': ('/mediacheck', 'Диагностика получения и отправки медиа.'),
}


@dp.callback_query(F.data.startswith('cmd:'))
async def command_detail(callback: CallbackQuery):
    await callback.answer()
    if not callback.message:
        return
    key = callback.data.partition(':')[2]
    if key not in COMMAND_INFO:
        return
    command, description = COMMAND_INFO[key]
    await show_screen(callback,
        screen('💬 ' + command.split()[0], 'Использование и описание') +
        '\n\n⌨️ <code>' + escape(command) + '</code>\n\n' + escape(description),
        kb([('↩ Назад к командам', 'menu:commands')], [('🏠 Главное меню', 'menu:home')]), section='commands')


def back():
    return kb([('↩ Назад', 'menu:home')])


def prefs(user_id):
    with db() as con:
        con.execute('INSERT OR IGNORE INTO notification_prefs(user_id) VALUES(?)', (user_id,))
        return dict(con.execute('SELECT edits,deletions,deleted_media FROM notification_prefs WHERE user_id=?', (user_id,)).fetchone())


def settings_keyboard(user_id):
    p = prefs(user_id)
    return kb(
        [(('✅' if p['edits'] else '❌') + ' Изменения', 'menu:toggle_edits')],
        [(('✅' if p['deletions'] else '❌') + ' Удаления', 'menu:toggle_deletions')],
        [(('✅' if p['deleted_media'] else '❌') + ' Фото и видео', 'menu:toggle_deleted_media')],
        [('🏠 Главное меню', 'menu:home')],
    )


async def owner_alert(bot, connection_id, body, **kwargs):
    with db() as con:
        row = con.execute('SELECT user_id FROM connections WHERE id=? AND enabled=1', (connection_id,)).fetchone()
        enabled = con.execute('SELECT alerts FROM settings WHERE user_id=?', (row['user_id'],)).fetchone() if row else None
    if row and (enabled is None or enabled['alerts']):
        try:
            await bot.send_message(row['user_id'], body[:4096], **kwargs)
        except Exception:
            logger.exception('Cannot send owner alert')


@dp.message(Command('archive'))
async def archive(message: Message):
    if not message.from_user:
        return
    with db() as con:
        rows = con.execute("SELECT m.chat_id,m.text,m.content_type,m.edited,m.deleted,m.updated_at FROM messages m JOIN connections c ON m.connection_id=c.id WHERE c.user_id=? ORDER BY m.updated_at DESC LIMIT 15",(message.from_user.id,)).fetchall()
    if not rows:
        return await message.answer('Архив пока пуст. Подключи бота в Telegram.')
    lines = ['📁 Последние 15 сообщений:']
    for r in rows:
        label = '🗑' if r['deleted'] else '✏️' if r['edited'] else '💬'
        lines.append(f"{label} {r['chat_id']}: {(r['text'] or '['+str(r['content_type'])+']')[:90]}")
    await message.answer('\n'.join(lines)[:4096])


@dp.message(Command('ref'))
async def referral(message: Message, bot: Bot):
    if not message.from_user:
        return
    with db() as con:
        count = con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(message.from_user.id,)).fetchone()[0]
    me = await bot.get_me()
    text = (screen('ПРИГЛАШЕНИЯ', 'Вместе удобнее') + 
            f'\n\n👥 <b>Приглашено друзей:</b> {count}\n\n'
            f'🔗 <b>Твоя персональная ссылка:</b>\n<code>https://t.me/{escape(me.username or "")}?start=ref_{message.from_user.id}</code>\n\n'
            f'<b>Условия:</b>\n'
            f'1. Отправь ссылку другу.\n'
            f'2. Друг запускает бота.\n'
            f'3. Друг <b>подключает бота</b> в настройках Telegram.\n'
            f'4. <b>Вы оба</b> получаете <b>+1 день Premium</b>!\n\n'
            f'<i>Нажми на ссылку, чтобы скопировать её.</i>')
    await message.answer(text, parse_mode='HTML')


@dp.message(Command('settings'))
async def settings(message: Message):
    if not message.from_user:
        return
    with db() as con:
        row = con.execute('SELECT alerts FROM settings WHERE user_id=?',(message.from_user.id,)).fetchone()
    state = row['alerts'] if row else 1
    await message.answer(f'🔔 Уведомления: {"включены" if state else "выключены"}',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Переключить уведомления',callback_data='settings:toggle')]]))


@dp.callback_query(F.data == 'settings:toggle')
async def toggle(callback: CallbackQuery):
    with db() as con:
        row = con.execute('SELECT alerts FROM settings WHERE user_id=?',(callback.from_user.id,)).fetchone()
        value = 0 if row is None or row['alerts'] else 1
        con.execute('INSERT INTO settings(user_id,alerts) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET alerts=excluded.alerts',(callback.from_user.id,value))
    await callback.answer('Настройки сохранены')
    if callback.message:
        await callback.message.answer('🔔 Уведомления '+('включены' if value else 'выключены'))


# --- АДМИН-ПАНЕЛЬ БОТА ---

def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📊 Общая статистика', callback_data='adm:stats')],
        [InlineKeyboardButton(text='📢 Сделать рассылку', callback_data='adm:broadcast')],
        [InlineKeyboardButton(text='💎 Выдать Premium', callback_data='adm:grant')],
        [InlineKeyboardButton(text='🛡 Управление ролями', callback_data='adm:roles')],
        [InlineKeyboardButton(text='🎨 Оформление UI', callback_data='adm:ui')],
    ])

ADMIN_UI_SECTIONS = {
    'home': '🏠 Главное меню', 'commands': '💬 Команды',
    'support': '💡 Поддержка', 'statistics': '📈 Статистика',
    'settings': '⚙️ Настройки', 'premium': '💎 Premium',
    'deleted': '🗑 Удалённые', 'edited': 'ℹ️ Изменённые',
    'setup': '🚀 Подключение', 'profile': '🤖 Мой HadesSave',
    'invite': '🎁 Пригласить друга',
}

def admin_ui_keyboard():
    buttons = [InlineKeyboardButton(text=label, callback_data='adm:ui_section:' + section)
               for section, label in ADMIN_UI_SECTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0, len(buttons), 2)] + [[InlineKeyboardButton(text='↩ Назад', callback_data='adm:home')]])

def admin_ui_section_keyboard(section):
    return kb([('🖼 Загрузить фото', 'adm:photo:'+section)],
              [('✏️ Изменить текст', 'adm:text:'+section)],
              [('👁 Предпросмотр', 'adm:preview:'+section)],
              [('🗑 Удалить фото', 'adm:remove_photo:'+section)],
              [('🧹 Сбросить текст', 'adm:remove_text:'+section)],
              [('↩ Назад', 'adm:ui')])


@dp.message(Command('admin'))
async def admin_panel(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    ADMIN_ACTIONS.pop(message.from_user.id, None) 
    
    text = (
        f'👑 <b>HADES SAVE — ADMIN PANEL</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<i>Добро пожаловать, Создатель.</i>\n\n'
        f'<blockquote>Здесь ты управляешь всей системой.</blockquote>'
    )
    
    await message.answer(text, parse_mode='HTML', reply_markup=admin_main_keyboard())


@dp.callback_query(F.data.startswith('adm:'))
async def admin_callbacks(call: CallbackQuery, bot: Bot):
    if not call.from_user or call.from_user.id != ADMIN_ID:
        return await call.answer('Нет доступа', show_alert=True)
    
    parts = call.data.split(':')
    action = parts[1] if len(parts) > 1 else 'home'
    
    if action == 'home':
        await call.answer()
        ADMIN_ACTIONS.pop(call.from_user.id, None)
        text = (
            f'👑 <b>HADES SAVE — ADMIN PANEL</b>\n'
            f'━━━━━━━━━━━━━━━━━━\n'
            f'<i>Добро пожаловать, Создатель.</i>\n\n'
            f'<blockquote>Здесь ты управляешь всей системой.</blockquote>'
        )
        return await call.message.edit_text(text, parse_mode='HTML', reply_markup=admin_main_keyboard())

    elif action == 'stats':
        await call.answer('Собираем данные...')
        with db() as con:
            users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            conns = con.execute('SELECT COUNT(*) FROM connections WHERE enabled=1').fetchone()[0]
            msgs = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            deleted = con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0]
            prem = con.execute('SELECT COUNT(*) FROM premium WHERE expires_at > ?', (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
            media = con.execute('SELECT COUNT(*) FROM media_files').fetchone()[0]
        
        text = (
            screen('ОБЩАЯ СТАТИСТИКА', f'Обновлено: {datetime.now().strftime("%H:%M:%S")}') +
            f'\n\n👥 <b>Пользователи:</b> {users}' +
            f'\n🔌 <b>Активных подключений:</b> {conns}' +
            f'\n💎 <b>Premium подписок:</b> {prem}' +
            f'\n\n💬 <b>Всего сообщений:</b> {msgs}' +
            f'\n🗑 <b>Удалено:</b> {deleted}' +
            f'\n📎 <b>Медиа в кэше:</b> {media}'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🔄 Обновить', callback_data='adm:stats')],
            [InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]
        ])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action == 'broadcast':
        await call.answer()
        ADMIN_ACTIONS[call.from_user.id] = {'action': 'waiting_broadcast'}
        text = (
            screen('РАССЫЛКА', 'Шаг 1 из 2') +
            '\n\nОтправь мне <b>любое сообщение</b> (текст, фото, видео, гиф, голосовое, кружок и т.д.), которое нужно разослать всем пользователям.'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action.startswith('run_broadcast_'):
        await call.answer('Запускаем!')
        msg_id = int(parts[2])
        ADMIN_ACTIONS.pop(call.from_user.id, None)
        
        await call.message.edit_text('⏳ <b>Рассылка запущена...</b>\n\nПожалуйста, подожди.', parse_mode='HTML')
        
        with db() as con:
            user_ids = [r['id'] for r in con.execute('SELECT id FROM users').fetchall()]
        
        success = 0
        blocked = 0
        errors = 0
        
        for i, uid in enumerate(user_ids):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=call.from_user.id, message_id=msg_id)
                success += 1
            except TelegramForbiddenError:
                blocked += 1
            except Exception:
                errors += 1
            
            if (i + 1) % 20 == 0:
                await call.message.edit_text(
                    f'⏳ <b>Рассылка в процессе...</b>\n\n'
                    f'Отправлено: {i+1} из {len(user_ids)}\n'
                    f'✅ Успешно: {success}\n'
                    f'🚫 Заблокировали: {blocked}\n'
                    f'⚠️ Ошибок: {errors}', 
                    parse_mode='HTML'
                )
            await asyncio.sleep(0.05)
            
        final_text = (
            screen('РАССЫЛКА ЗАВЕРШЕНА') +
            f'\n\n📊 <b>Итоги:</b>'
            f'\n✅ Успешно доставлено: {success}'
            f'\n🚫 Заблокировали бота: {blocked}'
            f'\n⚠️ Ошибок: {errors}'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await call.message.edit_text(final_text, parse_mode='HTML', reply_markup=markup)

    elif action == 'grant':
        await call.answer()
        ADMIN_ACTIONS[call.from_user.id] = {'action': 'waiting_grant'}
        text = (
            screen('ВЫДАЧА PREMIUM', 'Шаг 1 из 1') +
            '\n\nОтправь мне сообщение в формате:\n'
            '<code>ID или @юзернейм Количество_дней</code>\n\n'
            'Примеры:\n'
            '<code>7831720836 30</code>\n'
            '<code>@nonLuks 7</code>'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action == 'roles':
        await call.answer()
        ADMIN_ACTIONS[call.from_user.id] = {'action': 'waiting_role'}
        text = (
            screen('УПРАВЛЕНИЕ РОЛЯМИ', 'Шаг 1 из 1') +
            '\n\nОтправь мне сообщение в формате:\n'
            '<code>ID или @юзернейм роль</code>\n\n'
            'Доступные роли: <code>owner</code>, <code>admin</code>, <code>aboba</code>\n'
            'Чтобы снять роль, напиши: <code>ID или @юзернейм none</code>\n\n'
            'Примеры:\n'
            '<code>7831720836 owner</code>\n'
            '<code>@nonLuks admin</code>'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action == 'ui':
        await call.answer()
        text = '🎨 <b>Выбери раздел для оформления:</b>'
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=admin_ui_keyboard())
        
    elif action == 'ui_section':
        section = parts[2]
        if section not in ADMIN_UI_SECTIONS:
            return await call.answer('Раздел не найден', show_alert=True)
        await call.answer()
        design = get_design(section)
        text = (f'🎨 <b>{ADMIN_UI_SECTIONS[section]}</b>\n\n🖼 Фото: '+('установлено' if design['photo_id'] else 'не установлено')+
                '\n✏️ Текст: '+('свой' if design['custom_text'] is not None else 'стандартный'))
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=admin_ui_section_keyboard(section))

    elif action in ('photo', 'text'):
        section = parts[2]
        with db() as con:
            con.execute('INSERT INTO admin_edit_state(user_id,section,mode) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET section=excluded.section,mode=excluded.mode',
                        (call.from_user.id, section, action))
        await call.answer()
        await call.message.edit_text(f'🎨 <b>{ADMIN_UI_SECTIONS[section]}</b>\n\n'+
                                 ('Отправь мне фотографию следующим сообщением.' if action == 'photo' else
                                  'Отправь новый текст следующим сообщением (можно HTML-разметку).'),
                                 parse_mode='HTML', reply_markup=kb([('↩ Отмена', 'adm:ui_section:'+section)]))
                                 
    elif action in ('remove_photo', 'remove_text'):
        section = parts[2]
        with db() as con:
            con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)', (section,))
            col = 'photo_id' if action == 'remove_photo' else 'custom_text'
            con.execute(f'UPDATE ui_design SET {col}=NULL WHERE section=?', (section,))
        await call.answer('Сохранено')
        design = get_design(section)
        text = (f'🎨 <b>{ADMIN_UI_SECTIONS[section]}</b>\n\n🖼 Фото: '+('установлено' if design['photo_id'] else 'не установлено')+
                '\n✏️ Текст: '+('свой' if design['custom_text'] is not None else 'стандартный'))
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=admin_ui_section_keyboard(section))

    elif action == 'preview':
        section = parts[2]
        await call.answer()
        design = get_design(section)
        txt = design['custom_text'] or f'{ADMIN_UI_SECTIONS[section]}\n\nТекст раздела по умолчанию.'
        if design['photo_id']:
            await call.message.answer_photo(design['photo_id'], caption=txt[:1024], parse_mode='HTML')
        else:
            await call.message.answer(txt[:4096], parse_mode='HTML')


@dp.message(F.chat.type == 'private', F.photo)
async def admin_photo_upload(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    with db() as con:
        row = con.execute('SELECT section,mode FROM admin_edit_state WHERE user_id=?',(message.from_user.id,)).fetchone()
        if not row or row['mode'] != 'photo':
            return
        section=row['section']
        con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)',(section,))
        con.execute('UPDATE ui_design SET photo_id=? WHERE section=?',(message.photo[-1].file_id, section))
        con.execute('DELETE FROM admin_edit_state WHERE user_id=?',(message.from_user.id,))
    await message.answer(f'✅ Фото для раздела «{ADMIN_UI_SECTIONS[section]}» сохранено.', reply_markup=admin_ui_section_keyboard(section))


@dp.message(F.chat.type == 'private', F.text)
async def admin_text_upload(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    with db() as con:
        row = con.execute('SELECT section,mode FROM admin_edit_state WHERE user_id=?',(message.from_user.id,)).fetchone()
        if not row or row['mode'] != 'text':
            return
        section=row['section']
        con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)',(section,))
        con.execute('UPDATE ui_design SET custom_text=? WHERE section=?',(escape(message.text[:4000]),section))
        con.execute('DELETE FROM admin_edit_state WHERE user_id=?',(message.from_user.id,))
    await message.answer(f'✅ Текст для раздела «{ADMIN_UI_SECTIONS[section]}» сохранён.', reply_markup=admin_ui_section_keyboard(section))


@dp.message(F.chat.type == 'private')
async def admin_state_handler(message: Message, bot: Bot):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    
    state = ADMIN_ACTIONS.get(message.from_user.id)
    if not state:
        return 

    action = state.get('action')

    if action == 'waiting_broadcast':
        ADMIN_ACTIONS[message.from_user.id]['action'] = 'confirm_broadcast'
        ADMIN_ACTIONS[message.from_user.id]['msg_id'] = message.message_id
        
        text = '👀 <b>Предпросмотр рассылки:</b>\n\nВот что получат пользователи. Всё верно?'
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✅ Начать рассылку', callback_data=f'adm:run_broadcast_{message.message_id}')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]
        ])
        await message.answer(text, parse_mode='HTML', reply_markup=markup)
        return

    elif action == 'waiting_grant':
        if not message.text:
            return await message.answer('❌ Пожалуйста, отправь текст в формате: ID или @юзернейм Дни')
        
        ADMIN_ACTIONS.pop(message.from_user.id, None)
        parts = (message.text or '').split()
        if len(parts) != 2:
            return await message.answer('❌ Неверный формат. Нужно: <code>ID/Username Дни</code>', parse_mode='HTML')
        
        target, days_str = parts
        try:
            days = int(days_str)
            if days < 1 or days > 3650: raise ValueError
        except ValueError:
            return await message.answer('❌ Неверное количество дней (от 1 до 3650).')

        target_id = None
        if target.startswith('@'):
            username = target[1:]
            with db() as con:
                row = con.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
                if row: target_id = row['id']
        else:
            try:
                target_id = int(target)
            except ValueError:
                pass
        
        if not target_id:
            return await message.answer('❌ Пользователь с таким ID/юзернеймом не найден в базе.')

        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        with db() as con:
            con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at', (target_id, expires))
        
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await message.answer(f'✅ <b>Premium успешно выдан!</b>\n\nПользователь: <code>{target_id}</code>\nСрок: <b>{days} дней</b>\nДействует до: <code>{expires}</code>', parse_mode='HTML', reply_markup=markup)
        
        try:
            await bot.send_message(
                target_id,
                f'👑 <b>ПОДПИСКА АКТИВИРОВАНА</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n'
                f'Администратор выдал тебе <b>Premium на {days} дней</b>!\n\n'
                f'<blockquote>Теперь тебе доступен безлимитный архив, расширенные лимиты и уникальная корона в профиле.</blockquote>\n'
                f'<i>Наслаждайся возможностями HadesSave ✦</i>',
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление о премиуме пользователю {target_id}: {e}")
        
        return

    elif action == 'waiting_role':
        if not message.text:
            return await message.answer('❌ Пожалуйста, отправь текст в формате: ID или @юзернейм Роль')
        
        ADMIN_ACTIONS.pop(message.from_user.id, None)
        parts = (message.text or '').split()
        if len(parts) != 2:
            return await message.answer('❌ Неверный формат. Нужно: <code>ID/Username роль</code>', parse_mode='HTML')
        
        target, role_str = parts
        role_str = role_str.lower()
        
        if role_str not in ('owner', 'admin', 'aboba', 'none'):
            return await message.answer('❌ Неверная роль. Доступно: owner, admin, aboba, none')

        target_id = None
        if target.startswith('@'):
            username = target[1:]
            with db() as con:
                row = con.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
                if row: target_id = row['id']
        else:
            try:
                target_id = int(target)
            except ValueError:
                pass
        
        if not target_id:
            return await message.answer('❌ Пользователь с таким ID/юзернеймом не найден в базе.')

        with db() as con:
            if role_str == 'none':
                con.execute('DELETE FROM admins WHERE user_id=?', (target_id,))
                msg = f'✅ Роль снята с пользователя <code>{target_id}</code>.'
            else:
                con.execute('INSERT INTO admins(user_id, role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role', (target_id, role_str))
                msg = f'✅ Пользователю <code>{target_id}</code> выдана роль <b>{role_str}</b>.'
        
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await message.answer(msg, parse_mode='HTML', reply_markup=markup)
        
        try:
            if role_str == 'none':
                await bot.send_message(target_id, "⛔ <b>АДМИН-ДОСТУП ОТОЗВАН</b>\nВаш доступ к админ-панели HadesSave отозван.", parse_mode='HTML')
            else:
                await bot.send_message(target_id, f"👑 <b>АДМИН-ДОСТУП ВЫДАН</b>\nВам выдан доступ с префиксом <b>{role_str}</b>.\nЗайдите в Mini App -> Профиль -> Админ-панель.", parse_mode='HTML')
        except Exception:
            pass
        return


@dp.callback_query(F.data.startswith('menu:'))
async def menu_callback(callback: CallbackQuery, bot: Bot):
    section = callback.data.split(':', 1)[1]
    uid = callback.from_user.id
    if not callback.message:
        return await callback.answer()
    if section in ('toggle_edits', 'toggle_deletions', 'toggle_deleted_media', 'edited_toggle', 'deleted_toggle'):
        col = {'toggle_edits':'edits','edited_toggle':'edits', 'toggle_deletions':'deletions', 'deleted_toggle':'deletions','toggle_deleted_media':'deleted_media'}[section]
        prefs(uid)
        with db() as con:
            con.execute(f'UPDATE notification_prefs SET {col}=1-{col} WHERE user_id=?', (uid,))
        await callback.answer('Настройка сохранена')
        section = 'edited' if section == 'edited_toggle' else 'deleted' if section == 'deleted_toggle' else 'settings'
    else:
        await callback.answer()
    name = callback.from_user.first_name or 'друг'
    text = ''
    markup = back()
    if section == 'home':
        text = f'Привет, {escape(name)}!\n\nВыберите раздел:'
        markup = menu(uid)
    elif section == 'setup':
        me = await bot.get_me()
        text = (screen('ПОДКЛЮЧЕНИЕ', 'Telegram') +
                '\n\n🔌 <b>Подключи HadesSave к профилю</b>'
                '\n\n1. Открой настройки Telegram.'
                '\n2. Перейди в раздел «Чат-боты».'
                '\n3. Добавь <code>@' + escape(me.username or 'HadesSave_bot') + '</code>.'
                '\n4. Выдай необходимые разрешения.'
                '\n\nПосле подключения сообщения будут обрабатываться автоматически.')
        markup = setup_keyboard(me.username)
    elif section == 'setup_help':
        text = (screen('ПОМОЩЬ', 'Подключение HadesSave') +
                '\n\n👨‍💻 Контакт менеджера появится здесь после настройки.'
                '\n\nПока можно отправить обращение: <code>/support Твой вопрос</code>')
        markup = kb([('🔌 Подключение', 'menu:setup')], [('🏠 Главное меню', 'menu:home')])
    elif section == 'setup_check':
        with db() as con:
            connected = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1', (uid,)).fetchone()[0]
        text = (screen('ПРОВЕРКА ПОДКЛЮЧЕНИЯ') +
                ('\n\n✅ Бот подключён!' if connected else
                 '\n\n⚠️ Подключение пока не обнаружено.') +
                '\n\n🔌 Активных подключений: <b>' + str(connected) + '</b>')
        markup = kb([('🔄 Проверить ещё раз', 'menu:setup_check')],
                    [('🔌 Как подключить', 'menu:setup')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('deleted','edited'):
        key = 'deletions' if section == 'deleted' else 'edits'
        p = prefs(uid)
        heading = '🗑 Удалённые' if section == 'deleted' else 'ℹ️ Изменённые'
        word = 'удалённых' if section == 'deleted' else 'изменённых'
        text = (f'<b>{heading}</b>\n\n'
                f'Бот присылает все уведомления об {word} сообщениях.\n\n'
                'Выключение гасит только уведомления — сообщения всё равно сохраняются в архив.')
        markup = kb([(('✅ Включено' if p[key] else '❌ Выключено'), 'menu:deleted_toggle' if section=='deleted' else 'menu:edited_toggle')], [('↩ Назад', 'menu:home')])
    elif section == 'commands':
        text = (screen('💬 Список команд') +
                '\n\nТут вы можете ознакомиться со списком функций и их описанием.'
                '\n\n<blockquote>Команды с точкой (например .info) работают только в чате с собеседником.</blockquote>')
        markup = command_keyboard()
    elif section == 'features':
        text = '🛡 <b>ВОЗМОЖНОСТИ</b>\n━━━━━━━━━━━━━━\n\n📁 Архив сообщений\n🗑 Удаления и ✏️ правки в цитатах\n📸 Сохранение доступных медиа\n🔔 Уведомления владельцу\n👥 Рефералы и 💎 Premium' 
        markup = kb([('🔌 Подключить', 'menu:setup'), ('📖 Команды', 'menu:commands')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('statistics','statistics_reset'):
        if section == 'statistics_reset':
            text = '📊 Сброс статистики пока не реализован: архив не изменён.'
        else:
            with db() as con:
                count = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?',(uid,)).fetchone()[0]
                deleted = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.deleted=1',(uid,)).fetchone()[0]
                edited = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.edited=1',(uid,)).fetchone()[0]
            text = (screen('СТАТИСТИКА', 'Твой персональный обзор') + '\n\n'
                    f'💬 Сообщений: <b>{count}</b>\n🗑 Удалённых: <b>{deleted}</b>\n✏️ Изменённых: <b>{edited}</b>')
        markup = kb([('🔄 Обновить', 'menu:statistics')], [('📁 Архив', 'menu:archive'), ('👤 Профиль', 'menu:profile')], [('🏠 Главное меню', 'menu:home')])
    elif section == 'settings':
        text = screen('НАСТРОЙКИ', 'Управление уведомлениями') + '\n\n🔔 Управляй уведомлениями и медиа.\n\n👇 Нажми кнопку для переключения:'
        markup = settings_keyboard(uid)
    elif section == 'premium':
        with db() as con:
            row = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(uid,)).fetchone()
        text = (screen('PREMIUM', 'Особые возможности HadesSave') + 
                f'\n\n⭐ <b>Подписка:</b> {escape(row["expires_at"] if row else "не активна")}\n\n'
                f'💫 <b>Покупка Premium доступна только в Mini App.</b>\n\n'
                f'👇 Нажмите кнопку ниже, чтобы перейти.')
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🛒 Купить в Mini App', web_app=WebAppInfo(url="https://hadessavemod-art.github.io/hadesApp/#premium"))],
            [InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')]
        ])
    elif section == 'support':
        text = (screen('ПОДДЕРЖКА', 'Связь с администратором') + 
                '\n\n💬 Нажмите на кнопку ниже, чтобы написать нам.\n\n'
                '<i>Обычно отвечаем в течение дня.</i>')
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✍️ Написать в поддержку', url='https://t.me/Hades_save_men')],
            [InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')]
        ])
    elif section in ('invite','ref'):
        me = await bot.get_me()
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(uid,)).fetchone()[0]
        text = (screen('ПРИГЛАШЕНИЯ', 'Вместе удобнее') + 
                f'\n\n👥 <b>Приглашено друзей:</b> {count}\n\n'
                f'🔗 <b>Твоя персональная ссылка:</b>\n<code>https://t.me/{escape(me.username or "")}?start=ref_{uid}</code>\n\n'
                f'<b>Условия:</b>\n'
                f'1. Отправь ссылку другу.\n'
                f'2. Друг запускает бота.\n'
                f'3. Друг <b>подключает бота</b> в настройках Telegram (Настройки → Чат-боты).\n'
                f'4. <b>Вы оба</b> получаете <b>+1 день Premium</b>!\n\n'
                f'<i>Нажми на ссылку, чтобы скопировать её.</i>')
    elif section == 'profile':
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1',(uid,)).fetchone()[0]
        text = (screen('МОЙ ПРОФИЛЬ', 'Твой аккаунт HadesSave') + f'\n\n🆔 <b>Telegram ID:</b> <code>{uid}</code>\n🔌 <b>Подключений:</b> {count}\n\n<i>Управляй функциями через главное меню.</i>')
    elif section == 'archive':
        text = (screen('АРХИВ', 'Твоя история сообщений') + '\n\n📚 Чтобы открыть последние сохранённые записи, отправь команду:\n\n<code>/archive</code>')
    else:
        text = 'Раздел пока не перенесён.'
    await show_screen(callback, text, markup, section=section)


async def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit('Укажи BOT_TOKEN в переменных окружения')
    with db():
        pass
    logger.info('Starting HadesSave build %s; media_dir=%s; unmarked_reply_capture=%s',
                BUILD_ID, MEDIA_DIR, CAPTURE_UNMARKED_REPLIED_MEDIA)
    bot = Bot(TOKEN)
    me = await bot.get_me()
    BOT_USERNAME = me.username
    
    mtproto_task = None
    try:
        from hades_mtproto_bridge import run_mtproto_bridge
        mtproto_task = asyncio.create_task(run_mtproto_bridge(bot, ADMIN_ID), name='hades-mtproto')
        def _mtproto_done(task):
            if not task.cancelled():
                try:
                    task.result()
                except Exception:
                    logger.exception('MTProto worker stopped; Bot API remains active')
        mtproto_task.add_done_callback(_mtproto_done)
    except ImportError:
        logger.exception('MTProto requires telethon. Install it in requirements.txt')
    from admin_http import start_server
    admin_runner = await start_server(bot)
    from archive_retention import cleanup_loop
    retention_task = asyncio.create_task(cleanup_loop(db, MEDIA_DIR))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([
            BotCommand(command='start', description='🏠 Главное меню'),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        updates = sorted(set(dp.resolve_used_update_types()) | {
            'business_connection', 'business_message', 'edited_business_message',
            'deleted_business_messages', 'message', 'callback_query',
            'pre_checkout_query'
        })
        logger.info('Subscribed to update types: %s', ', '.join(updates))
        await dp.start_polling(bot, allowed_updates=updates)
    finally:
        if mtproto_task is not None:
            mtproto_task.cancel()
            with suppress(asyncio.CancelledError):
                await mtproto_task
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        await admin_runner.cleanup()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
