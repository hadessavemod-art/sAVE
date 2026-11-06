"""Standalone Telegram bot. Partial port of tgBotSaveMod, not full Laravel parity."""
import asyncio
import logging
import time
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import json
import re
import secrets
from pathlib import Path
from contextlib import suppress
from urllib.parse import quote
from aiogram.types import (
    CopyTextButton, WebAppInfo, InputMediaPhoto, InputMediaVideo, FSInputFile
)
import shutil
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, BusinessConnection, BusinessMessagesDeleted, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery,
    Update, BotCommand, ReplyKeyboardRemove, MenuButtonCommands
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '7831720836'))
CAPTURE_UNMARKED_REPLIED_MEDIA = False

# Always resolve a relative database path from the script directory, not from
# the process working directory. This prevents a restart from silently opening
# a second, empty bot.sqlite3 and losing persistent MUTE state.
SCRIPT_DIR = Path(__file__).resolve().parent
_raw_db_path = os.getenv('DB_PATH', '').strip()
if _raw_db_path:
    _configured_db_path = Path(_raw_db_path).expanduser()
    DB_PATH = (_configured_db_path if _configured_db_path.is_absolute()
               else SCRIPT_DIR / _configured_db_path).resolve()
else:
    DB_PATH = (SCRIPT_DIR / 'bot.sqlite3').resolve()

# One-time safe migration for the previous default: ./bot.sqlite3 relative to
# the launch directory. SQLite's backup API includes committed WAL content.
_legacy_name = Path(_raw_db_path).expanduser() if _raw_db_path else Path('bot.sqlite3')
_legacy_candidate = (_legacy_name if _legacy_name.is_absolute()
                    else Path.cwd() / _legacy_name).resolve()
if (_legacy_candidate != DB_PATH and _legacy_candidate.is_file()
        and not DB_PATH.exists()):
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(_legacy_candidate), timeout=30) as _source_db:
            with sqlite3.connect(str(DB_PATH), timeout=30) as _target_db:
                _source_db.backup(_target_db)
        logging.getLogger('business_bot').info(
            'Migrated legacy SQLite database %s -> %s', _legacy_candidate, DB_PATH
        )
    except Exception:
        # Do not silently fall back to another path: the canonical path is
        # logged at startup so the deployment can be diagnosed safely.
        logging.getLogger('business_bot').exception(
            'Could not migrate legacy SQLite database %s -> %s',
            _legacy_candidate, DB_PATH
        )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('business_bot')
MOSCOW_TZ = ZoneInfo('Europe/Moscow')
BUILD_ID = 'HADES-MINIAPP-ARCHIVE-20261106-28'
dp = Dispatcher()

ADMIN_ACTIONS = {}
BOT_USERNAME = ''

SPAM_SEND_INTERVAL_SECONDS = 0.10  # Высокая скорость; при FloodWait Telegram бот автоматически ждёт.


def screen(title: str, subtitle: str = '') -> str:
    return f'<b>{title}</b>' + (f'\n<i>{subtitle}</i>' if subtitle else '')


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection_cols = {row[1] for row in connection.execute('PRAGMA table_info(connections)')}
    for column in ('can_delete_all_messages', 'can_delete_sent_messages', 'can_read_messages', 'rights_checked'):
        if column not in connection_cols:
            connection.execute(f'ALTER TABLE connections ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_connections_user_enabled ON connections(user_id,enabled)')
    connection.execute('''CREATE TABLE IF NOT EXISTS messages (connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sender_id INTEGER, text TEXT, content_type TEXT, raw_json TEXT NOT NULL, edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))''')
    connection.execute('CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, rewarded INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS keyboard_cleanup (user_id INTEGER PRIMARY KEY)')
    connection.execute('CREATE TABLE IF NOT EXISTS premium (user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
    connection.execute('CREATE TABLE IF NOT EXISTS notification_prefs (user_id INTEGER PRIMARY KEY, edits INTEGER DEFAULT 1, deletions INTEGER DEFAULT 1, deleted_media INTEGER DEFAULT 1)')
    connection.execute('''CREATE TABLE IF NOT EXISTS ui_design (section TEXT PRIMARY KEY, photo_id TEXT, custom_text TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS admin_edit_state (user_id INTEGER PRIMARY KEY, section TEXT NOT NULL, mode TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS muted_chats (
        business_owner_id INTEGER NOT NULL,
        connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        muted_by INTEGER,
        button_message_id INTEGER,
        button_token TEXT,
        PRIMARY KEY(business_owner_id,connection_id,chat_id)
    )''')
    connection.execute('''CREATE TABLE IF NOT EXISTS mute_delete_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT, chat_id INTEGER,
        message_id INTEGER, status TEXT NOT NULL, details TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_mute_delete_diag_conn ON mute_delete_diagnostics(connection_id,id)')

    muted_cols = {row[1] for row in connection.execute('PRAGMA table_info(muted_chats)')}
    for column, sql_type in (
        ('muted_by', 'INTEGER'), ('button_message_id', 'INTEGER'),
        ('button_token', 'TEXT'), ('business_owner_id', 'INTEGER'),
    ):
        if column not in muted_cols:
            connection.execute(f'ALTER TABLE muted_chats ADD COLUMN {column} {sql_type}')

    # Backfill old records with the owner from their exact Business connection.
    connection.execute('''
        UPDATE muted_chats
           SET business_owner_id=(SELECT c.user_id FROM connections c
                                  WHERE c.id=muted_chats.connection_id)
         WHERE business_owner_id IS NULL
    ''')

    # Older builds used PRIMARY KEY(connection_id, chat_id). Rebuild the
    # physical table so the account owner is part of the true primary key.
    pk_columns = [
        row[1] for row in sorted(
            (r for r in connection.execute('PRAGMA table_info(muted_chats)') if r[5]),
            key=lambda r: r[5],
        )
    ]
    desired_pk = ['business_owner_id', 'connection_id', 'chat_id']
    if pk_columns != desired_pk:
        connection.execute('DROP TRIGGER IF EXISTS trg_muted_chats_audit_insert')
        connection.execute('DROP TRIGGER IF EXISTS trg_muted_chats_audit_update')
        connection.execute('DROP TRIGGER IF EXISTS trg_muted_chats_audit_delete')
        connection.execute('DROP INDEX IF EXISTS idx_muted_owner_connection_chat')
        connection.execute('DROP TABLE IF EXISTS muted_chats_v21_migration')
        connection.execute('''CREATE TABLE muted_chats_v21_migration (
            business_owner_id INTEGER NOT NULL,
            connection_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            muted_by INTEGER,
            button_message_id INTEGER,
            button_token TEXT,
            PRIMARY KEY(business_owner_id,connection_id,chat_id)
        )''')
        connection.execute('''
            INSERT OR REPLACE INTO muted_chats_v21_migration
                (business_owner_id,connection_id,chat_id,muted_by,button_message_id,button_token)
            SELECT COALESCE(
                       m.business_owner_id,
                       (SELECT c.user_id FROM connections c WHERE c.id=m.connection_id),
                       0
                   ),
                   m.connection_id,m.chat_id,m.muted_by,m.button_message_id,m.button_token
              FROM muted_chats m
             ORDER BY m.rowid
        ''')
        connection.execute('DROP TABLE muted_chats')
        connection.execute('ALTER TABLE muted_chats_v21_migration RENAME TO muted_chats')

    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_muted_owner_connection_chat '
        'ON muted_chats(business_owner_id,connection_id,chat_id)'
    )
    connection.execute('''CREATE TABLE IF NOT EXISTS mute_control_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id TEXT, chat_id INTEGER, actor_id INTEGER,
        action TEXT NOT NULL, status TEXT NOT NULL,
        message_id INTEGER, details TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    connection.execute('''CREATE TABLE IF NOT EXISTS mute_state_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL, business_owner_id INTEGER,
        connection_id TEXT, chat_id INTEGER,
        muted_by INTEGER, button_message_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    audit_cols = {row[1] for row in connection.execute('PRAGMA table_info(mute_state_audit)')}
    if 'business_owner_id' not in audit_cols:
        connection.execute('ALTER TABLE mute_state_audit ADD COLUMN business_owner_id INTEGER')

    connection.execute('''CREATE TRIGGER IF NOT EXISTS trg_muted_chats_audit_insert
        AFTER INSERT ON muted_chats BEGIN
            INSERT INTO mute_state_audit(operation,business_owner_id,connection_id,chat_id,muted_by,button_message_id)
            VALUES('INSERT',NEW.business_owner_id,NEW.connection_id,NEW.chat_id,NEW.muted_by,NEW.button_message_id);
        END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS trg_muted_chats_audit_update
        AFTER UPDATE ON muted_chats BEGIN
            INSERT INTO mute_state_audit(operation,business_owner_id,connection_id,chat_id,muted_by,button_message_id)
            VALUES('UPDATE',NEW.business_owner_id,NEW.connection_id,NEW.chat_id,NEW.muted_by,NEW.button_message_id);
        END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS trg_muted_chats_audit_delete
        AFTER DELETE ON muted_chats BEGIN
            INSERT INTO mute_state_audit(operation,business_owner_id,connection_id,chat_id,muted_by,button_message_id)
            VALUES('DELETE',OLD.business_owner_id,OLD.connection_id,OLD.chat_id,OLD.muted_by,OLD.button_message_id);
        END''')
    connection.execute('CREATE TABLE IF NOT EXISTS spam_cooldowns (connection_id TEXT PRIMARY KEY, last_at REAL)')
    connection.execute('CREATE TABLE IF NOT EXISTS media_files (connection_id TEXT,chat_id INTEGER,message_id INTEGER,local_path TEXT,PRIMARY KEY(connection_id,chat_id,message_id))')
    connection.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, role TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY, reason TEXT, banned_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, stars INTEGER, days INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS deleted_notified (connection_id TEXT, chat_id INTEGER, message_id INTEGER, notified_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))')

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
    for column in ('sender_name', 'sender_username', 'chat_title', 'chat_username',
                   'reply_to_sender_name', 'reply_to_text', 'message_date'):
        if column not in cols:
            connection.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
    if 'reply_to_message_id' not in cols:
        connection.execute('ALTER TABLE messages ADD COLUMN reply_to_message_id INTEGER')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_messages_owner_chat_date ON messages(connection_id,chat_id,message_id)')
    connection.commit()
    return connection


def record_mute_control(connection_id, chat_id, actor_id, action, status,
                        message_id=None, details=''):
    try:
        with db() as con:
            con.execute(
                '''INSERT INTO mute_control_diagnostics
                   (connection_id,chat_id,actor_id,action,status,message_id,details)
                   VALUES(?,?,?,?,?,?,?)''',
                (connection_id, chat_id, actor_id, action, status,
                 message_id, str(details or '')[:350]),
            )
            con.commit()
    except Exception:
        logger.exception('Could not write MUTE control diagnostic: action=%s status=%s',
                         action, status)


def is_owner_message(message: Message):
    if not message.business_connection_id or not message.from_user:
        return False
    with db() as con:
        owner = con.execute('SELECT user_id FROM connections WHERE id=? AND enabled=1',
                            (message.business_connection_id,)).fetchone()
    return bool(owner and message.from_user.id == owner['user_id'])


def automated_sender_kind(message: Message) -> str | None:
    """Identify bot/automated attributions exposed by Telegram on a Message."""
    # sender_business_bot: a Business bot sent this message on behalf of an account.
    if getattr(message, 'sender_business_bot', None) is not None:
        return 'business_bot'
    # via_bot: a user sent the message through an inline bot (for example SaveBot).
    # The from_user can still be the human account owner, so checking only is_bot
    # incorrectly exempts this automated message from MUTE.
    via_bot = getattr(message, 'via_bot', None)
    if via_bot is not None:
        return 'via_bot'
    # Messages sent on behalf of a channel/chat are not typed by the account owner.
    if getattr(message, 'sender_chat', None) is not None:
        return 'sender_chat'
    sender = getattr(message, 'from_user', None)
    if sender is not None and bool(getattr(sender, 'is_bot', False)):
        return 'bot_account'
    return None


def is_human_sender(message: Message) -> bool:
    """True only when Telegram gives no bot/automated sender attribution."""
    return bool(getattr(message, 'from_user', None)) and automated_sender_kind(message) is None


def is_human_owner_message(message: Message) -> bool:
    """True only for a message manually typed by the connected account owner."""
    return is_human_sender(message) and is_owner_message(message)


def business_delete_rights(connection_id: str) -> tuple[bool, bool]:
    with db() as con:
        row = con.execute(
            'SELECT can_delete_all_messages,can_delete_sent_messages FROM connections WHERE id=?',
            (connection_id,),
        ).fetchone()
    if not row:
        return False, False
    return bool(row['can_delete_all_messages']), bool(row['can_delete_sent_messages'])


MEDIA_FIELD_NAMES = (
    'photo', 'video', 'video_note', 'voice', 'document', 'audio',
    'animation', 'sticker', 'live_photo',
)


def _message_has_media(message: Message) -> bool:
    return any(getattr(message, field, None) for field in MEDIA_FIELD_NAMES)


def media_restriction_reason(message: Message, saved_json=None):
    """Return why media must not be archived, without attempting to retrieve it."""
    if not _message_has_media(message):
        return None
    if getattr(message, 'has_protected_content', False):
        return 'telegram_protected_content'
    # Bot API ephemeral messages are a separate, recipient-only message type.
    if getattr(message, 'ephemeral_message_id', None) is not None:
        return 'telegram_ephemeral_message'
    try:
        detected, marker = view_once_marker(message, saved_json)
    except (NameError, AttributeError, TypeError, ValueError):
        detected, marker = False, None
    if detected:
        return f'ephemeral_marker:{marker}'
    return None


def _strip_media_from_payload(payload: dict):
    for field in MEDIA_FIELD_NAMES:
        payload.pop(field, None)


async def notify_media_issue(message: Message, bot: Bot, status: str, details: str = ''):
    """Notify the connected account owner about actionable archive failures."""
    # Missing file_id is expected for some Telegram ephemeral/unsupported media.
    # Keep the diagnostic in the database, but do not send the unwanted DM.
    if status == 'file_id_missing':
        return
    owner_id = await business_owner(message)
    if not owner_id:
        return
    chat_name = (message.chat.full_name or message.chat.title or
                 (f'чат {message.chat.id}'))
    if status == 'restricted_media_not_saved':
        body = (
            '🔒 <b>Медиа не сохранено</b>\n\n'
            'Telegram пометил вложение как защищённое или исчезающее. '
            'HadesSave не обходит этот режим и не сохраняет файл.\n\n'
            'Попроси отправителя прислать обычное фото или видео, '
            'только если он согласен.'
        )
    elif status == 'file_id_missing':
        body = (
            '⚠️ <b>Не удалось сохранить медиа</b>\n\n'
            'Telegram не передал боту идентификатор файла. '
            'Попроси отправителя переслать обычное фото или видео.'
        )
    elif status == 'file_id_only_size_limit':
        body = (
            '⚠️ <b>Медиа слишком большое для локального архива</b>\n\n'
            'Идентификатор Telegram сохранён, но локальная копия не создана.'
        )
    else:
        body = (
            '⚠️ <b>Не удалось создать локальную копию медиа</b>\n\n'
            'Архив записал ошибку. Попроси отправителя прислать файл повторно '
            'в обычном виде, если он согласен.'
        )
    body += f'\n\n💬 Чат: <b>{escape(str(chat_name))}</b>'
    if details:
        body += f'\n🧾 Причина: <code>{escape(str(details)[:220])}</code>'
    try:
        await bot.send_message(owner_id, body, parse_mode='HTML')
    except Exception:
        logger.warning('Could not notify owner %s about media status %s', owner_id, status,
                       exc_info=True)


def save_message(message: Message, edited=False):
    # Archive both sides of each Business conversation, including messages
    # manually sent by the connected account owner.
    if not message.business_connection_id:
        return
    payload_data = json.loads(message.model_dump_json(exclude_none=True))
    restriction = media_restriction_reason(message, payload_data)
    if restriction:
        # Do not leave a file_id in raw_json: later archive/deletion handlers
        # must not be able to send restricted or ephemeral media back out.
        _strip_media_from_payload(payload_data)
        payload_data.pop('caption', None)
        payload_data.pop('caption_entities', None)
        payload_data['media_archive_status'] = 'not_saved_restricted'
        payload_data['media_archive_reason'] = restriction
    payload = json.dumps(payload_data, ensure_ascii=False, separators=(',', ':'))
    text = ('🔒 Защищённое или исчезающее медиа — не сохранено'
            if restriction else
            message.text or message.caption or ('🎭 Стикер' + (f' {message.sticker.emoji}' if message.sticker.emoji else '') if message.sticker else ''))
    sender = message.from_user
    sender_id = sender.id if sender else None
    business_bot_sender = getattr(message, 'sender_business_bot', None)
    if sender_id is None and business_bot_sender is not None:
        # Business-bot replies are outgoing on behalf of the connected account.
        with db() as con:
            owner_row = con.execute('SELECT user_id FROM connections WHERE id=?',
                                    (message.business_connection_id,)).fetchone()
        sender_id = int(owner_row['user_id']) if owner_row else None
    name = (' '.join(filter(None, (sender.first_name, sender.last_name))).strip() if sender else
            (getattr(business_bot_sender, 'username', None) or 'Вы' if business_bot_sender else None))
    username = (sender.username if sender else None) or (message.chat.username if message.chat.type == 'private' else None)
    reply = getattr(message, 'reply_to_message', None)
    reply_sender = getattr(reply, 'from_user', None) if reply else None
    reply_sender_name = (' '.join(filter(None, (reply_sender.first_name, reply_sender.last_name))).strip()
                         if reply_sender else (getattr(getattr(reply, 'sender_business_bot', None), 'username', None) if reply else None))
    reply_text = (getattr(reply, 'text', None) or getattr(reply, 'caption', None) or
                  (f'📎 {getattr(reply, "content_type", "Сообщение")}' if reply else None))
    reply_to_id = getattr(reply, 'message_id', None) if reply else None
    message_date = message.date.isoformat() if getattr(message, 'date', None) else None
    with db() as con:
        con.execute('''INSERT INTO messages(
            connection_id,chat_id,message_id,sender_id,text,content_type,raw_json,edited,
            reply_to_message_id,reply_to_sender_name,reply_to_text,message_date
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET
            sender_id=excluded.sender_id,text=excluded.text,content_type=excluded.content_type,
            raw_json=excluded.raw_json,edited=MAX(messages.edited,excluded.edited),
            reply_to_message_id=COALESCE(excluded.reply_to_message_id,messages.reply_to_message_id),
            reply_to_sender_name=COALESCE(excluded.reply_to_sender_name,messages.reply_to_sender_name),
            reply_to_text=COALESCE(excluded.reply_to_text,messages.reply_to_text),
            message_date=COALESCE(messages.message_date,excluded.message_date),
            updated_at=CURRENT_TIMESTAMP''',
            (message.business_connection_id, message.chat.id, message.message_id,
             sender_id, text, message.content_type, payload, int(edited),
             reply_to_id, reply_sender_name, str(reply_text)[:1000] if reply_text else None, message_date))
        con.execute('''UPDATE messages SET sender_name=COALESCE(?,sender_name),
            sender_username=COALESCE(?,sender_username),chat_title=COALESCE(?,chat_title),
            chat_username=COALESCE(?,chat_username)
            WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (name,username,message.chat.full_name or message.chat.title, message.chat.username,
             message.business_connection_id,message.chat.id,message.message_id))


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
        has_file = int(any(getattr(obj, name, None) for name in media)) if kind in ('business_message', 'edited_business_message') else 0
        replied = getattr(obj, 'reply_to_message', None)
        details = ''
        mute_row = None
        is_owner = False
        is_mute_notice = False
        role = 'unknown'
        if kind == 'business_connection':
            rights = getattr(obj, 'rights', None)
            details = (
                'enabled=' + str(getattr(obj, 'is_enabled', '?')) +
                '; can_reply=' + str(getattr(rights, 'can_reply', '?')) +
                '; can_read_messages=' + str(getattr(rights, 'can_read_messages', '?')) +
                '; can_delete_all=' + str(getattr(rights, 'can_delete_all_messages', '?'))
            )
        elif kind == 'deleted_business_messages':
            details = 'deleted_count=' + str(len(getattr(obj, 'message_ids', []) or []))
        elif replied:
            details = ('reply_type=' + str(getattr(replied, 'content_type', '?')) +
                       '; reply_has_media=' + str(any(getattr(replied, name, None) for name in media)))

        # Record the sender role and whether this exact chat has persistent MUTE.
        # This lets /mutediag distinguish "Telegram sent no update" from "delete failed".
        if kind in ('business_message', 'edited_business_message') and obj is not None:
            sender_bot = automated_sender_kind(obj)
            is_owner = is_human_owner_message(obj)
            mute_row = None
            if connection_id and chat:
                with db() as con:
                    mute_row = get_scoped_mute_row(connection_id, chat.id)
            is_mute_notice = bool(
                mute_row and (
                    (mute_row['button_message_id'] and obj.message_id == mute_row['button_message_id'])
                    or ((getattr(obj, 'text', None) or '') == '🔇 Этот чат находится в муте!'
                        and getattr(obj, 'reply_markup', None)
                        and any(
                            button.callback_data and button.callback_data.startswith('muteoff:')
                            for row in obj.reply_markup.inline_keyboard for button in row
                        ))
                )
            )
            role = 'owner' if is_owner else (sender_bot or 'contact')
            via_bot_obj = getattr(obj, 'via_bot', None)
            via_bot_name = (getattr(via_bot_obj, 'username', None) if via_bot_obj else None)
            details = (details + f'; mute={"active" if mute_row else "off"}; sender={role}'
                       + (f'; via_bot=@{via_bot_name}' if via_bot_name else ''))[:250]

        # Persist the event BEFORE any early return, so /mutediag can prove
        # whether Telegram actually delivered the incoming Business update.
        try:
            with db() as con:
                con.execute(
                    "INSERT INTO business_update_diagnostics (connection_id,update_type,chat_id,message_id,content_type,has_reply,has_media_file,details) VALUES(?,?,?,?,?,?,?,?)",
                    (connection_id, kind, getattr(chat, 'id', None),
                     getattr(obj, 'message_id', None), content_type,
                     int(bool(replied)), has_file, details[:250]),
                )
                con.execute(
                    "DELETE FROM business_update_diagnostics WHERE id IN (SELECT id FROM business_update_diagnostics ORDER BY id DESC LIMIT -1 OFFSET 2000)"
                )
        except Exception:
            logger.exception('Cannot store Business update diagnostic')

        # Route MUTE controls only when typed by the OWNER of this exact
        # Business connection. A command sent by account A is mirrored as an
        # incoming CONTACT message in account B's Business connection. If every
        # human sender is accepted, the mirror executes .mute again for B and
        # makes both accounts look muted. Consume that mirrored command here.
        if kind == 'business_message' and obj is not None:
            raw_control_text = (getattr(obj, 'text', None) or
                                getattr(obj, 'caption', None) or '')
            normalized_control_text = _norm_cmd(raw_control_text)
            control_match = re.fullmatch(r'\.(mute|unmute)', normalized_control_text,
                                         re.IGNORECASE)
            if control_match:
                actor_id = getattr(getattr(obj, 'from_user', None), 'id', None)
                action = control_match.group(1).lower()
                if not is_human_owner_message(obj):
                    status = ('update_middleware_mirrored_contact_command_ignored'
                              if is_human_sender(obj)
                              else 'update_middleware_bot_sender_rejected')
                    record_mute_control(
                        connection_id, getattr(chat, 'id', None), actor_id,
                        action, status, getattr(obj, 'message_id', None),
                        f'raw_text={raw_control_text!r}; normalized={normalized_control_text!r}; sender={role}; '
                        'accepted only from this Business connection owner',
                    )
                    logger.info(
                        'Ignored mirrored/non-owner MUTE command: action=%s conn=%s chat=%s actor=%s role=%s',
                        action, connection_id, getattr(chat, 'id', None), actor_id, role,
                    )
                    # Do not let the mirrored command reach any normal handler.
                    return None

                record_mute_control(
                    connection_id, getattr(chat, 'id', None), actor_id,
                    action, 'update_middleware_owner_command_detected',
                    getattr(obj, 'message_id', None),
                    f'raw_text={raw_control_text!r}; normalized={normalized_control_text!r}; sender={role}',
                )
                logger.warning(
                    'MUTE CONTROL ROUTED FROM UPDATE MIDDLEWARE: action=%s conn=%s chat=%s message=%s actor=%s role=%s',
                    action, connection_id, getattr(chat, 'id', None),
                    getattr(obj, 'message_id', None), actor_id, role,
                )
                bot = data.get('bot')
                try:
                    if bot is None:
                        raise RuntimeError('Bot instance missing from Update middleware data')
                    handled = await handle_business_command(obj, bot)
                    record_mute_control(
                        connection_id, getattr(chat, 'id', None), actor_id,
                        action, 'update_middleware_handled' if handled else 'update_middleware_handler_returned_false',
                        getattr(obj, 'message_id', None),
                        f'handled={handled}; raw_text={raw_control_text!r}',
                    )
                    return None
                except Exception as exc:
                    record_mute_control(
                        connection_id, getattr(chat, 'id', None), actor_id,
                        action, 'update_middleware_exception',
                        getattr(obj, 'message_id', None),
                        f'{type(exc).__name__}: {exc}',
                    )
                    logger.exception(
                        'MUTE control failed in Update middleware: action=%s conn=%s chat=%s',
                        action, connection_id, getattr(chat, 'id', None),
                    )
                    with suppress(Exception):
                        await delete_business_command(bot, obj)
                    if actor_id and bot is not None:
                        with suppress(Exception):
                            await bot.send_message(
                                actor_id,
                                '⚠️ Не удалось выполнить команду MUTE. Подробности записаны в /mutediag.',
                            )
                    return None

        # Enforce MUTE at the Update boundary, before any regular message handler
        # or archive/media handler can process this update. The human account owner stays
        # exempt, and the MUTE notice itself is preserved.
        if mute_row and not is_owner and not is_mute_notice:
            bot = data.get('bot')
            if bot is not None:
                await delete_muted_business_message(bot, obj)
                logger.info(
                    'MUTE intercepted at Update middleware: type=%s conn=%s chat=%s message=%s sender=%s',
                    kind, connection_id, chat.id, obj.message_id, role,
                )
                # Do not pass this message into archive or media handlers.
                return None

        logger.info('Business update received: type=%s connection=%s media=%s reply=%s details=%s',
                    kind, connection_id, bool(has_file), bool(replied), details[:180])
    return await handler(event, data)


def remember_business_connection(connection: BusinessConnection):
    # Сохраняет владельца Business-подключения и возвращает прежний статус.
    owner_id = connection.user.id
    enabled = int(bool(connection.is_enabled))
    with db() as con:
        previous = con.execute(
            'SELECT enabled FROM connections WHERE id=?', (connection.id,)
        ).fetchone()
        previous_enabled = int(previous['enabled']) if previous else None
        con.execute(
            '''INSERT INTO connections(id,user_id,enabled,updated_at)
               VALUES(?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                   user_id=excluded.user_id,
                   enabled=excluded.enabled,
                   updated_at=CURRENT_TIMESTAMP''',
            (connection.id, owner_id, enabled),
        )
    rights = getattr(connection, 'rights', None)
    can_reply = getattr(rights, 'can_reply', None)
    if can_reply is None:
        can_reply = getattr(connection, 'can_reply', None)
    can_delete_all = int(bool(getattr(rights, 'can_delete_all_messages', False)))
    can_delete_sent = int(bool(getattr(rights, 'can_delete_sent_messages', False)))
    can_read_messages = int(bool(getattr(rights, 'can_read_messages', False)))
    with db() as con:
        con.execute(
            '''UPDATE connections SET can_delete_all_messages=?,
               can_delete_sent_messages=?, can_read_messages=?, rights_checked=1 WHERE id=?''',
            (can_delete_all, can_delete_sent, can_read_messages, connection.id),
        )
    logger.info(
        'Business connection stored: id=%s owner=%s enabled=%s can_reply=%s '
        'can_delete_all=%s can_delete_sent=%s can_read_messages=%s previous=%s',
        connection.id, owner_id, enabled, can_reply, can_delete_all,
        can_delete_sent, can_read_messages, previous_enabled,
    )
    return previous_enabled


async def ensure_business_connection(bot: Bot, connection_id: str):
    # Загружает права также для старых подключений, которые уже есть в БД,
    # но появились до добавления диагностики can_delete_all_messages.
    with db() as con:
        row = con.execute(
            'SELECT user_id,rights_checked FROM connections WHERE id=? AND enabled=1',
            (connection_id,),
        ).fetchone()
    if row and row['rights_checked']:
        return row['user_id']

    try:
        connection = await bot.get_business_connection(connection_id)
    except Exception:
        logger.exception('Cannot load Business connection: %s', connection_id)
        return None

    remember_business_connection(connection)
    return connection.user.id if connection.is_enabled else None


async def refresh_business_connection(bot: Bot, connection_id: str):
    'Always re-fetch current Business rights before enabling destructive MUTE.'
    connection = await bot.get_business_connection(connection_id)
    remember_business_connection(connection)
    return connection


@dp.business_connection()
async def business_connection_updated(connection: BusinessConnection, bot: Bot):
    previous_enabled = remember_business_connection(connection)
    if connection.is_enabled and previous_enabled != 1:
        notification = 'Спасибо, что выбрали нас!\nС уважением, @HadesSave_bot'
    elif not connection.is_enabled and previous_enabled == 1:
        notification = 'Вы отключились от нас, надеемся, ненадолго.'
    else:
        return

    # DM доступен только если пользователь ранее открыл диалог с ботом.
    try:
        await bot.send_message(connection.user.id, notification)
        logger.info('Business connection notification sent: owner=%s enabled=%s',
                    connection.user.id, connection.is_enabled)
    except Exception:
        logger.exception('Cannot send Business connection notification to owner=%s',
                         connection.user.id)


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
                            await message.bot.send_message(referrer, '👥 По вашей ссылке зарегистрировался новый пользователь!')
                        except Exception:
                            pass
        except ValueError:
            pass
    with db() as con:
        was_removed = con.execute('SELECT 1 FROM keyboard_cleanup WHERE user_id=?',
                                  (message.from_user.id,)).fetchone()
    if not was_removed:
        await message.answer('✅ Клавиатура убрана. Команда /start теперь доступна через «Меню».',
                             reply_markup=ReplyKeyboardRemove())
        with db() as con:
            con.execute('INSERT OR IGNORE INTO keyboard_cleanup(user_id) VALUES (?)',
                        (message.from_user.id,))
    await send_home(message)


@dp.message(Command('help'))
async def help_command(message: Message):
    await message.answer('Команды: /start, /help, /archive, /ref, /premium, /settings, /mediacheck, /mutediag, /clear. В подключённом чате: .info, .mute, .unmute, .spam. Администратору: /admin.')


@dp.message(Command('clear'))
async def clear_archive_cmd(message: Message):
    if not message.from_user: return
    with db() as con:
        con.execute('DELETE FROM messages WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (message.from_user.id,))
        con.execute('DELETE FROM media_files WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (message.from_user.id,))
        con.commit()
    await message.answer('🗑 <b>Архив полностью очищен!</b>', parse_mode='HTML')


@dp.message(Command('mediacheck'))
async def mediacheck_command(message: Message):
    if not message.from_user:
        return
    with db() as con:
        rows = con.execute('''SELECT d.chat_id, d.message_id, d.kind, d.status, d.details, d.created_at
            FROM media_diagnostics d
            JOIN connections c ON c.id=d.connection_id
            WHERE c.user_id=?
            ORDER BY d.id DESC LIMIT 12''', (message.from_user.id,)).fetchall()
    if not rows:
        return await message.answer(
            f'📁 <b>Диагностика медиа</b>\nВерсия: <code>{BUILD_ID}</code>\n\n'
            'Пока нет записей. Когда поступит фото или видео, здесь появится результат сохранения.',
            parse_mode='HTML')
    lines = [f'📁 <b>Последняя диагностика медиа</b>\nВерсия: <code>{BUILD_ID}</code>']
    for row in rows:
        status = escape(str(row['status'] or 'unknown'))
        kind = escape(str(row['kind'] or 'медиа'))
        details = escape(str(row['details'] or '')[:180])
        lines.append(
            f"\n• <b>{kind}</b> — <code>{status}</code>\n"
            f"  Чат: <code>{row['chat_id']}</code>, сообщение: <code>{row['message_id']}</code>\n"
            f"  {details}\n  <i>{escape(str(row['created_at'] or ''))}</i>"
        )
    await message.answer('\n'.join(lines)[:4096], parse_mode='HTML')


@dp.message(Command('mutediag'))
async def mutediag_command(message: Message, bot: Bot):
    if not message.from_user:
        return
    with db() as con:
        conn_ids = [row['id'] for row in con.execute(
            'SELECT id FROM connections WHERE user_id=? AND enabled=1',
            (message.from_user.id,),
        ).fetchall()]
    for connection_id in conn_ids:
        with suppress(Exception):
            await refresh_business_connection(bot, connection_id)
    with db() as con:
        connections = con.execute(
            """SELECT id,can_delete_all_messages,can_delete_sent_messages,can_read_messages,rights_checked
               FROM connections WHERE user_id=? AND enabled=1 ORDER BY updated_at DESC""",
            (message.from_user.id,),
        ).fetchall()
        recent_events = con.execute(
            """SELECT d.connection_id,d.update_type,d.chat_id,d.message_id,d.details,d.created_at
               FROM business_update_diagnostics d JOIN connections c ON c.id=d.connection_id
               WHERE c.user_id=? AND d.update_type IN ('business_message','edited_business_message')
               ORDER BY d.id DESC LIMIT 8""",
            (message.from_user.id,),
        ).fetchall()
        active_mutes = con.execute(
            """SELECT m.connection_id,m.chat_id,m.muted_by,m.button_message_id,m.business_owner_id
               FROM muted_chats m JOIN connections c ON c.id=m.connection_id
               WHERE c.user_id=? AND m.business_owner_id=c.user_id
               ORDER BY m.business_owner_id,m.connection_id,m.chat_id""",
            (message.from_user.id,),
        ).fetchall()
        failures = con.execute(
            """SELECT d.chat_id,d.message_id,d.status,d.details,d.created_at
               FROM mute_delete_diagnostics d JOIN connections c ON c.id=d.connection_id
               WHERE c.user_id=? ORDER BY d.id DESC LIMIT 8""",
            (message.from_user.id,),
        ).fetchall()
        control_events = con.execute(
            """SELECT d.connection_id,d.chat_id,d.actor_id,d.action,d.status,
                      d.message_id,d.details,d.created_at
               FROM mute_control_diagnostics d JOIN connections c ON c.id=d.connection_id
               WHERE c.user_id=? ORDER BY d.id DESC LIMIT 6""",
            (message.from_user.id,),
        ).fetchall()
        state_events = con.execute(
            """SELECT a.operation,a.business_owner_id,a.connection_id,a.chat_id,a.muted_by,
                      a.button_message_id,a.created_at
               FROM mute_state_audit a JOIN connections c ON c.id=a.connection_id
               WHERE c.user_id=? AND a.business_owner_id=c.user_id ORDER BY a.id DESC LIMIT 6""",
            (message.from_user.id,),
        ).fetchall()

    lines = [
        f'🔇 <b>Диагностика MUTE</b> · <code>{BUILD_ID}</code>',
        'MUTE действует отдельно для каждого Business-подключения и конкретного чата.',
        f'База данных: <code>{escape(str(DB_PATH))}</code>',
        f'Файл БД существует: {"✅" if DB_PATH.is_file() else "❌"}',
    ]
    if not connections:
        lines.append('\nНет активных Business-подключений.')
    for conn in connections:
        allow_all = bool(conn['can_delete_all_messages'])
        allow_sent = bool(conn['can_delete_sent_messages'])
        lines.append(
            '\nПодключение: <code>' + escape(str(conn['id'])[:18]) + '…</code>'
            + '\nУдалять все сообщения: ' + ('✅' if allow_all else '❌')
            + '\nУдалять сообщения бота: ' + ('✅' if allow_sent else '❌')
            + '\nЧитать сообщения: ' + ('✅' if conn['can_read_messages'] else '❌')
            + '\nПрава обновлены: ' + ('да' if conn['rights_checked'] else 'нет')
        )
    if active_mutes:
        lines.append('\n<b>Активные MUTE:</b>')
        for row in active_mutes:
            lines.append(
                f"\n• владелец <code>{row['business_owner_id']}</code> · чат <code>{row['chat_id']}</code>"
                f" · подключение <code>{escape(str(row['connection_id'])[:10])}…</code>"
                f" · включил <code>{row['muted_by']}</code> · кнопка <code>{row['button_message_id']}</code>"
            )
    else:
        lines.append('\n<b>Активные MUTE:</b> нет (в таблице нет активной записи).')
    if control_events:
        lines.append('\n<b>Последние команды MUTE:</b>')
        for row in control_events:
            detail = escape(str(row['details'] or '')[:90])
            lines.append(
                f"\n• {escape(str(row['action']))}: <b>{escape(str(row['status']))}</b>"
                f" · чат <code>{row['chat_id']}</code> · msg <code>{row['message_id']}</code>"
                + (f"\n  <code>{detail}</code>" if detail else '')
            )
    else:
        lines.append('\nКоманд .mute/.unmute в журнале нет — обработчик их не зарегистрировал.')
    if state_events:
        lines.append('\n<b>История таблицы MUTE:</b>')
        for row in state_events:
            lines.append(
                f"\n• {escape(str(row['operation']))} · владелец <code>{row['business_owner_id']}</code> · чат <code>{row['chat_id']}</code>"
                f" · muted_by <code>{row['muted_by']}</code> · кнопка <code>{row['button_message_id']}</code>"
                f" · {escape(str(row['created_at']))}"
            )
    if recent_events:
        lines.append('\n<b>Последние входящие Business-события:</b>')
        for row in recent_events:
            detail = escape(str(row['details'] or '')[:130])
            lines.append(
                f"\n• {escape(str(row['update_type']))} · чат <code>{row['chat_id']}</code>"
                f" · подключение <code>{escape(str(row['connection_id'])[:10])}…</code>"
                f" · сообщение <code>{row['message_id']}</code> · {escape(str(row['created_at']))}"
                + (f"\n  <code>{detail}</code>" if detail else '')
            )
    else:
        lines.append('\n⚠️ Входящие Business-события не поступали. Проверь, что этот чат разрешён HadesSave в Telegram → Business → Чат-боты → Управление чатами.')
    if failures:
        lines.append('\n<b>Последние попытки удаления:</b>')
        for row in failures:
            detail = escape(str(row['details'] or '')[:140])
            lines.append(
                f"\n• {escape(str(row['status']))} · чат <code>{row['chat_id']}</code>"
                f" · сообщение <code>{row['message_id']}</code>"
                + (f"\n  <code>{detail}</code>" if detail else '')
            )
    else:
        lines.append('\nПопыток удаления под MUTE пока нет.')
    await message.answer('\n'.join(lines)[:4096], parse_mode='HTML')


MEDIA_DIR = Path(os.getenv('MEDIA_DIR') or ('/app/data/media' if Path('/app/data').is_dir() else str(Path(__file__).resolve().parent / 'media'))).resolve()


async def store_media(message: Message, bot: Bot) -> bool:
    if not message.business_connection_id:
        return False

    kind = media_kind(message) or 'media'
    connection_id = message.business_connection_id
    chat_id = message.chat.id
    message_id = message.message_id

    restriction = media_restriction_reason(message)
    if restriction:
        # Also remove any stale cached copy if an edited update becomes restricted.
        old_path = None
        with db() as con:
            previous = con.execute(
                'SELECT local_path FROM media_files WHERE connection_id=? AND chat_id=? AND message_id=?',
                (connection_id, chat_id, message_id)).fetchone()
            old_path = previous['local_path'] if previous else None
            con.execute('DELETE FROM media_files WHERE connection_id=? AND chat_id=? AND message_id=?',
                        (connection_id, chat_id, message_id))
        if old_path and Path(old_path).is_file():
            with suppress(OSError):
                Path(old_path).unlink()
        media_diagnostic(message, kind, 'restricted_media_not_saved', restriction)
        await notify_media_issue(message, bot, 'restricted_media_not_saved', restriction)
        return False

    item = (message.photo[-1] if message.photo else message.video or message.document or
            message.audio or message.voice or message.video_note or message.animation or message.sticker)
    if not item or not getattr(item, 'file_id', None):
        media_diagnostic(message, kind, 'file_id_missing', 'Telegram update did not contain a downloadable file_id')
        await notify_media_issue(message, bot, 'file_id_missing')
        return False

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
        details = f'bytes={file_size}; download_limit_mb={max_download_mb}'
        media_diagnostic(message, kind, 'file_id_only_size_limit', details)
        await notify_media_issue(message, bot, 'file_id_only_size_limit', details)
        return False

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        details = f'{type(exc).__name__}: {exc}'
        media_diagnostic(message, kind, 'file_id_saved_disk_unavailable', details)
        await notify_media_issue(message, bot, 'download_failed', details)
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
    media_diagnostic(message, kind, 'download_failed', details)
    await notify_media_issue(message, bot, 'download_failed', details)
    logger.warning('Media download failed after retries: chat=%s message=%s; %s',
                   chat_id, message_id, details)
    return False


AUTO_FORWARD_MEDIA = False


def media_diagnostic(message: Message, kind: str, status: str, details: str = ''):
    with db() as con:
        con.execute('INSERT INTO media_diagnostics(connection_id,chat_id,message_id,kind,status,details) VALUES(?,?,?,?,?,?)',
                    (message.business_connection_id, message.chat.id, message.message_id, kind, status, str(details)[:350]))


async def _send_media_item(bot: Bot, owner_id: int, message: Message, source, caption: str):
    """Отправляет одно медиа по источнику (file_id или FSInputFile)."""
    if message.photo:
        await bot.send_photo(owner_id, source, caption=caption[:1024] if caption else None,
                             parse_mode='HTML' if caption else None)
    elif message.video:
        await bot.send_video(owner_id, source, caption=caption[:1024] if caption else None,
                             parse_mode='HTML' if caption else None)
    elif message.voice:
        await bot.send_voice(owner_id, source, caption=caption[:1024] if caption else None,
                             parse_mode='HTML' if caption else None)
    elif message.video_note:
        await bot.send_video_note(owner_id, source)
        if caption:
            await bot.send_message(owner_id, caption, parse_mode='HTML')
    elif message.sticker:
        await bot.send_sticker(owner_id, source)
        if caption:
            await bot.send_message(owner_id, caption, parse_mode='HTML')
    elif message.animation:
        await bot.send_animation(owner_id, source, caption=caption[:1024] if caption else None,
                                 parse_mode='HTML' if caption else None)
    elif message.audio:
        await bot.send_audio(owner_id, source, caption=caption[:1024] if caption else None,
                             parse_mode='HTML' if caption else None)
    else:
        await bot.send_document(owner_id, source, caption=caption[:1024] if caption else None,
                                parse_mode='HTML' if caption else None)


async def deliver_incoming_media_now(message: Message, bot: Bot, force: bool = False):
    """Быстрый путь: только file_id. Скачивание — отдельно, вне этого пути."""
    if (not AUTO_FORWARD_MEDIA and not force) or is_owner_message(message):
        return False
    restriction = media_restriction_reason(message)
    if restriction:
        media_diagnostic(message, media_kind(message) or 'media',
                         'restricted_media_not_saved', restriction)
        return False
    if message.sticker:
        return False
    kind = media_kind(message)
    if not kind:
        return False
    owner_id = await business_owner(message)
    if not owner_id:
        media_diagnostic(message, kind, 'no_business_owner')
        return False

    bot_username = escape((await bot.get_me()).username or 'HadesSave_bot')
    caption = (
        f'<blockquote>🕵️‍♂️ Сохранено с помощью '
        f'<a href="https://t.me/{bot_username}">@{bot_username}</a>'
        f'</blockquote>'
    )[:1000]

    item = (message.photo[-1] if message.photo else message.video or message.document or
            message.audio or message.voice or message.video_note or message.animation)
    if not item or not getattr(item, 'file_id', None):
        media_diagnostic(message, kind, 'missing_file_id')
        return False

    try:
        await _send_media_item(bot, owner_id, message, item.file_id, caption)
        media_diagnostic(message, kind, 'sent_file_id')
        return True
    except Exception as e:
        media_diagnostic(message, kind, 'file_id_failed', type(e).__name__ + ': ' + str(e))
        logger.warning('Business media file_id send failed: %s', e)
        return False


async def deliver_archived_media(bot: Bot, owner_id: int, local_path, file_id,
                                 caption: str, content_type: str = 'document'):
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
    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
        return True
    except Exception:
        logger.exception('Could not delete Business message: chat=%s message=%s',
                         message.chat.id, message.message_id)
        return False


async def delete_muted_business_message(bot: Bot, message: Message) -> bool:
    # Delete one incoming message under MUTE and persist exact API failures.
    connection_id = message.business_connection_id
    chat_id = message.chat.id
    try:
        result = await bot.delete_business_messages(
            business_connection_id=connection_id,
            message_ids=[message.message_id],
        )
        if result is False:
            raise RuntimeError('Telegram returned False for deleteBusinessMessages')
        with db() as con:
            con.execute(
                '''INSERT INTO mute_delete_diagnostics
                   (connection_id,chat_id,message_id,status,details)
                   VALUES(?,?,?,?,?)''',
                (connection_id, chat_id, message.message_id, 'deleted', ''),
            )
        logger.info('MUTE deleted incoming message: conn=%s chat=%s message=%s',
                    connection_id, chat_id, message.message_id)
        return True
    except Exception as exc:
        detail = f'{type(exc).__name__}: {exc}'[:350]
        with db() as con:
            con.execute(
                '''INSERT INTO mute_delete_diagnostics
                   (connection_id,chat_id,message_id,status,details)
                   VALUES(?,?,?,?,?)''',
                (connection_id, chat_id, message.message_id, 'failed', detail),
            )
        logger.exception('MUTE could not delete incoming message: conn=%s chat=%s message=%s',
                         connection_id, chat_id, message.message_id)

        owner_id = await business_owner(message)
        key = (owner_id, connection_id, chat_id)
        now = time.monotonic()
        if now - MUTE_FAILURE_ALERTS.get(key, 0.0) >= 60:
            MUTE_FAILURE_ALERTS[key] = now
            if owner_id:
                notice = (
                    '⚠️ <b>MUTE не смог удалить сообщение.</b>\n\n'
                    'Telegram отклонил удаление. Проверь право Business-подключения '
                    '«Удалять все сообщения» (can_delete_all_messages).\n\n'
                    f'<code>{escape(detail)}</code>\n'
                    f'Чат: <code>{chat_id}</code>, сообщение: <code>{message.message_id}</code>'
                )
                with suppress(Exception):
                    await bot.send_message(owner_id, notice, parse_mode='HTML')
        return False


async def business_owner(message: Message):
    with db() as con:
        row = con.execute('SELECT user_id FROM connections WHERE id=? AND enabled=1',
                          (message.business_connection_id,)).fetchone()
    return row['user_id'] if row else None


def business_owner_id_for_connection(connection_id: str | None):
    if not connection_id:
        return None
    with db() as con:
        row = con.execute(
            'SELECT user_id FROM connections WHERE id=? AND enabled=1',
            (connection_id,),
        ).fetchone()
    return row['user_id'] if row else None


def get_scoped_mute_row(connection_id: str | None, chat_id: int, owner_id=None):
    """Return MUTE only for this exact owner + Business connection + peer chat."""
    if not connection_id:
        return None
    if owner_id is None:
        owner_id = business_owner_id_for_connection(connection_id)
    if owner_id is None:
        return None
    with db() as con:
        return con.execute(
            '''SELECT * FROM muted_chats
               WHERE business_owner_id=? AND connection_id=? AND chat_id=?''',
            (owner_id, connection_id, chat_id),
        ).fetchone()


def is_chat_muted(connection_id: str | None, chat_id: int, owner_id=None) -> bool:
    'Persistent MUTE check for one exact chat.'
    return get_scoped_mute_row(connection_id, chat_id, owner_id) is not None


def _norm_cmd(text: str) -> str:
    if not text:
        return ''
    # NFKC normalizes full-width Latin characters; remove invisible Unicode
    # format controls (including zero-width separators) before parsing commands.
    import unicodedata
    normalized = unicodedata.normalize('NFKC', str(text)).replace('\u00A0', ' ')
    normalized = ''.join(ch for ch in normalized if unicodedata.category(ch) != 'Cf')
    return normalized.replace('\r\n', '\n').strip()


async def _notify_in_chat(bot: Bot, message: Message, text: str, ttl_sec: int = 10):
    """Пишет в бизнес-чат и удаляет через ttl_sec. Fallback — в DM владельцу."""
    try:
        sent = await bot.send_message(
            message.chat.id, text,
            business_connection_id=message.business_connection_id,
            parse_mode='HTML', disable_web_page_preview=True)
        if ttl_sec and getattr(sent, 'message_id', None):
            async def _del():
                await asyncio.sleep(ttl_sec)
                with suppress(Exception):
                    await bot.delete_business_messages(
                        business_connection_id=message.business_connection_id,
                        message_ids=[sent.message_id])
            asyncio.create_task(_del())
        return True
    except Exception as exc:
        logger.warning('In-chat notify failed: %s', exc)
    return await owner_alert(bot, message.business_connection_id, text, parse_mode='HTML')


async def handle_business_command(message: Message, bot: Bot):
    owner = await business_owner(message)
    # Every dot-command is executed only in the Business connection owned by
    # the sender. A counterpart may receive the same message as a mirrored
    # contact update; it must never run a second MUTE/UNMUTE there.
    if owner is None or not is_human_owner_message(message):
        return False
    command = _norm_cmd(message.text or '')
    if not command.startswith('.'):
        return False

    logger.info('cmd received: %r conn=%s chat=%s actor=%s owner=%s',
                command[:120], message.business_connection_id, message.chat.id,
                message.from_user.id if message.from_user else None, owner)

    # Единая регистронезависимая проверка известных команд
    known = re.match(r'^\.(info|mute|unmute|spam)(?:\s|$)', command, re.IGNORECASE)
    if not known:
        return False
    cmd_name = known.group(1).lower()
    if cmd_name in ('mute', 'unmute'):
        record_mute_control(
            message.business_connection_id, message.chat.id,
            message.from_user.id, cmd_name, 'received', message.message_id,
            f'text={command[:100]!r}',
        )

    await delete_business_command(bot, message)

    if cmd_name == 'info':
        user = ('@' + message.chat.username) if message.chat.username else 'не указан'
        await owner_alert(bot, message.business_connection_id,
                          f'👤 Собеседник\nID: {message.chat.id}\nUsername: {user}')
        return True

    if cmd_name in ('mute', 'unmute'):
        if cmd_name == 'mute':
            # Do not let another participant take over an already active MUTE.
            # Otherwise they could replace the button token and lock the original
            # muter out of their own control.
            with db() as con:
                existing_mute = get_scoped_mute_row(
                    message.business_connection_id, message.chat.id, owner
                )
            if existing_mute:
                status = ('already_active_same_actor'
                          if existing_mute['muted_by'] == message.from_user.id
                          else 'already_active_by_another_actor')
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'mute', status, message.message_id,
                    f'current_muted_by={existing_mute["muted_by"]}; button_message_id={existing_mute["button_message_id"]}',
                )
                return True

            # Never show a successful MUTE state unless Telegram confirms the
            # current permission to delete arbitrary incoming Business messages.
            try:
                refreshed = await refresh_business_connection(
                    bot, message.business_connection_id
                )
                fresh_owner_id = refreshed.user.id
                fresh_rights = getattr(refreshed, 'rights', None)
                can_delete_all = bool(getattr(fresh_rights, 'can_delete_all_messages', False))
                if not refreshed.is_enabled or not can_delete_all:
                    reason = (
                        'Business-подключение отключено.' if not refreshed.is_enabled else
                        'Не выдано право «Удалять все сообщения» '
                        '(can_delete_all_messages).'
                    )
                    # A stale MUTE row from an older build must not continue
                    # pretending that deletion is enforced after rights were lost.
                    with db() as con:
                        stale = con.execute(
                            '''SELECT button_message_id FROM muted_chats
                               WHERE business_owner_id=? AND connection_id=? AND chat_id=?''',
                            (owner, message.business_connection_id, message.chat.id),
                        ).fetchone()
                        con.execute(
                            '''DELETE FROM muted_chats
                               WHERE business_owner_id=? AND connection_id=? AND chat_id=?''',
                            (owner, message.business_connection_id, message.chat.id),
                        )
                    if stale and stale['button_message_id']:
                        with suppress(Exception):
                            await bot.delete_business_messages(
                                business_connection_id=message.business_connection_id,
                                message_ids=[stale['button_message_id']],
                            )
                        with suppress(Exception):
                            await bot.edit_message_reply_markup(
                                chat_id=message.chat.id,
                                message_id=stale['button_message_id'],
                                business_connection_id=message.business_connection_id,
                                reply_markup=None,
                            )
                    await bot.send_message(
                        fresh_owner_id,
                        '⛔ <b>MUTE не включён.</b>\n\n'
                        f'{escape(reason)}\n\n'
                        'Открой настройки Telegram → Telegram Business → Чат-боты → '
                        'HadesSave и разреши удаление всех сообщений. Затем повтори .mute.',
                        parse_mode='HTML',
                    )
                    record_mute_control(
                        message.business_connection_id, message.chat.id,
                        message.from_user.id, 'mute', 'refused_rights',
                        message.message_id, reason,
                    )
                    logger.error(
                        'MUTE activation refused: conn=%s chat=%s enabled=%s can_delete_all=%s',
                        message.business_connection_id, message.chat.id,
                        refreshed.is_enabled, can_delete_all,
                    )
                    return True
            except Exception as exc:
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'mute', 'rights_check_failed',
                    message.message_id, f'{type(exc).__name__}: {exc}',
                )
                logger.exception('Could not verify Business delete rights; MUTE not enabled')
                with suppress(Exception):
                    await bot.send_message(
                        owner,
                        '⛔ <b>MUTE не включён:</b> не удалось проверить права Telegram Business.\n'
                        f'<code>{escape(f"{type(exc).__name__}: {exc}")}</code>',
                        parse_mode='HTML',
                    )
                return True

            token = secrets.token_urlsafe(9)
            # Persist this MUTE under its exact owner + connection + chat key.
            with db() as con:
                old = con.execute(
                    """SELECT button_message_id FROM muted_chats
                       WHERE business_owner_id=? AND connection_id=? AND chat_id=?""",
                    (owner, message.business_connection_id, message.chat.id),
                ).fetchone()
                con.execute(
                    """INSERT INTO muted_chats
                       (connection_id,chat_id,muted_by,button_message_id,button_token,business_owner_id)
                       VALUES(?,?,?,NULL,?,?)
                       ON CONFLICT(business_owner_id,connection_id,chat_id) DO UPDATE SET
                           muted_by=excluded.muted_by,
                           button_message_id=NULL,
                           button_token=excluded.button_token,
                           business_owner_id=excluded.business_owner_id""",
                    (message.business_connection_id, message.chat.id,
                     message.from_user.id, token, owner),
                )
                committed_mute = con.execute(
                    """SELECT muted_by,button_token,business_owner_id FROM muted_chats
                       WHERE business_owner_id=? AND connection_id=? AND chat_id=?""",
                    (owner, message.business_connection_id, message.chat.id),
                ).fetchone()
                if (not committed_mute or committed_mute['button_token'] != token
                        or committed_mute['business_owner_id'] != owner):
                    raise RuntimeError('MUTE row was not persisted to the configured database')
                con.commit()

            # Verify the committed row through a separate SQLite connection.
            with db() as verify_con:
                persisted = verify_con.execute(
                    """SELECT muted_by,button_token,business_owner_id FROM muted_chats
                       WHERE business_owner_id=? AND connection_id=? AND chat_id=?""",
                    (owner, message.business_connection_id, message.chat.id),
                ).fetchone()
            if (not persisted or persisted['button_token'] != token
                    or persisted['business_owner_id'] != owner):
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'mute', 'verification_failed',
                    message.message_id,
                    f'db_path={DB_PATH}; expected_token={token}',
                )
                raise RuntimeError('MUTE row disappeared after SQLite COMMIT')
            record_mute_control(
                message.business_connection_id, message.chat.id,
                message.from_user.id, 'mute', 'persisted',
                message.message_id, f'db_path={DB_PATH}; token={token}',
            )
            logger.info('MUTE persisted: db=%s owner_id=%s conn=%s chat=%s actor=%s',
                        DB_PATH, owner, message.business_connection_id,
                        message.chat.id, message.from_user.id)

            can_delete_all, can_delete_sent = business_delete_rights(message.business_connection_id)
            if not can_delete_all:
                try:
                    await bot.send_message(
                        owner,
                        '⚠️ Для полноценного MUTE разреши HadesSave право '
                        '«Удалять все сообщения» в настройках подключения Telegram Business. '
                        'Без этого права Telegram не позволит удалять входящие сообщения '
                        'и сообщения, отправленные другими Business-ботами.'
                    )
                except Exception:
                    logger.warning('Could not notify owner about missing can_delete_all_messages',
                                   exc_info=True)

            # Если mute включали повторно, деактивируем старую кнопку.
            if old and old['button_message_id']:
                with suppress(Exception):
                    await bot.edit_message_reply_markup(
                        chat_id=message.chat.id,
                        message_id=old['button_message_id'],
                        business_connection_id=message.business_connection_id,
                        reply_markup=None,
                    )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text='Выключить mute',
                    callback_data=f'muteoff:{token}',
                    style='danger',
                )
            ]])
            try:
                # Пояснение о включённом mute показывается прямо над красной кнопкой.
                sent = await bot.send_message(
                    message.chat.id,
                    '🔇 Этот чат находится в муте!',
                    business_connection_id=message.business_connection_id,
                    reply_markup=keyboard,
                )
                with db() as con:
                    cur = con.execute(
                        '''UPDATE muted_chats SET button_message_id=?
                           WHERE business_owner_id=? AND connection_id=? AND chat_id=? AND button_token=?''',
                        (sent.message_id, owner, message.business_connection_id,
                         message.chat.id, token),
                    )
                    con.commit()
                    updated_rows = cur.rowcount
                if updated_rows != 1:
                    record_mute_control(
                        message.business_connection_id, message.chat.id,
                        message.from_user.id, 'mute', 'row_missing_after_notice',
                        message.message_id,
                        f'notice_message_id={sent.message_id}; token={token}',
                    )
                    with suppress(Exception):
                        await bot.delete_business_messages(
                            business_connection_id=message.business_connection_id,
                            message_ids=[sent.message_id],
                        )
                    logger.error('MUTE row vanished before button id save: conn=%s chat=%s',
                                 message.business_connection_id, message.chat.id)
                    return True
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'mute', 'button_sent',
                    message.message_id, f'button_message_id={sent.message_id}',
                )
            except Exception as exc:
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'mute', 'button_send_failed',
                    message.message_id, f'{type(exc).__name__}: {exc}',
                )
                logger.exception('Could not send mute-off button')
            return True

        # .unmute is a text fallback. Only the exact human who enabled
        # this chat's MUTE (muted_by) is allowed to disable it.
        with db() as con:
            current = con.execute(
                '''SELECT muted_by,button_message_id FROM muted_chats
                   WHERE business_owner_id=? AND connection_id=? AND chat_id=?''',
                (owner, message.business_connection_id, message.chat.id),
            ).fetchone()
            if current and current['muted_by'] != message.from_user.id:
                record_mute_control(
                    message.business_connection_id, message.chat.id,
                    message.from_user.id, 'unmute', 'rejected_not_muter',
                    message.message_id,
                    f'active_muted_by={current["muted_by"]}',
                )
                return True
            con.execute(
                '''DELETE FROM muted_chats WHERE business_owner_id=? AND connection_id=?
                   AND chat_id=? AND muted_by=?''',
                (owner, message.business_connection_id, message.chat.id, message.from_user.id),
            )
            con.commit()
        record_mute_control(
            message.business_connection_id, message.chat.id,
            message.from_user.id, 'unmute',
            'removed' if current else 'no_active_row', message.message_id,
            f'previous_button_message_id={current["button_message_id"] if current else None}',
        )
        if current and current['button_message_id']:
            try:
                # Удаляем не только кнопку, но и само уведомление о включённом mute.
                await bot.delete_business_messages(
                    business_connection_id=message.business_connection_id,
                    message_ids=[current['button_message_id']],
                )
            except Exception:
                logger.exception('Could not delete mute notice after .unmute')
                with suppress(Exception):
                    await bot.edit_message_reply_markup(
                        chat_id=message.chat.id,
                        message_id=current['button_message_id'],
                        business_connection_id=message.business_connection_id,
                        reply_markup=None,
                    )
        return True

    # ---- .spam ----
    usage = '⚠️ Формат: <code>.spam 5 текст сообщения</code>'
    if command.lower().strip() == '.spam':
        await _notify_in_chat(bot, message, usage)
        return True

    match = re.fullmatch(r'\.spam\s+(\d+)\s+(.+)', command, re.IGNORECASE | re.S)
    if not match:
        logger.warning('.spam parse failed: %r', command[:200])
        await _notify_in_chat(bot, message, usage)
        return True

    count = int(match.group(1))
    body  = match.group(2).strip()
    logger.info('.spam: count=%d len_body=%d', count, len(body))

    is_prem  = is_premium(owner)
    limit    = 50 if is_prem else 20
    cooldown = 180 if is_prem else 300

    if not (1 <= count <= limit):
        await _notify_in_chat(bot, message, f'⚠️ Лимит: 1–{limit} сообщений.')
        return True
    if len(body) > 3000:
        await _notify_in_chat(bot, message, '⚠️ Текст длиннее 3000 символов.')
        return True

    now_ts = datetime.now(timezone.utc).timestamp()
    is_admin_user = message.from_user.id == ADMIN_ID
    with db() as con:
        row = con.execute('SELECT last_at FROM spam_cooldowns WHERE connection_id=?',
                          (message.business_connection_id,)).fetchone()
        if not is_admin_user and row and now_ts - row['last_at'] < cooldown:
            remaining = int(cooldown - (now_ts - row['last_at'])) + 1
            logger.info('.spam: cooldown %ss', remaining)
            await _notify_in_chat(bot, message, f'⏳ Подожди {remaining} сек.')
            return True
        con.execute('INSERT OR REPLACE INTO spam_cooldowns VALUES(?,?)',
                    (message.business_connection_id, now_ts))

    sent = 0
    last_err = None
    for i in range(count):
        retry_count = 0
        while True:
            try:
                await bot.send_message(
                    message.chat.id, body,
                    business_connection_id=message.business_connection_id)
                sent += 1
                break
            except TelegramRetryAfter as exc:
                retry_count += 1
                if retry_count > 2:
                    last_err = exc
                    logger.exception('.spam: retry limit reached at %d/%d', i + 1, count)
                    break
                wait_seconds = max(1.0, float(exc.retry_after)) + 0.5
                logger.warning('.spam: Telegram rate limit, retry in %.1fs', wait_seconds)
                await asyncio.sleep(wait_seconds)
            except Exception as exc:
                last_err = exc
                logger.exception('.spam: send %d/%d failed', i + 1, count)
                break

        if last_err is not None:
            break
        # Telegram ограничивает частоту отправки в один и тот же личный чат.
        if i < count - 1:
            await asyncio.sleep(SPAM_SEND_INTERVAL_SECONDS)

    logger.info('.spam: sent %d/%d err=%s',
                sent, count, type(last_err).__name__ if last_err else None)

    if sent == 0 and last_err is not None:
        await _notify_in_chat(bot, message,
            f'⚠️ Не удалось отправить: {type(last_err).__name__}')
    elif sent < count:
        await _notify_in_chat(bot, message, f'⚠️ Отправлено {sent}/{count}')
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





async def _store_media_safely(message: Message, bot: Bot):
    try:
        await store_media(message, bot)
    except Exception as exc:
        logger.exception('Unexpected media archive error: chat=%s message=%s',
                         message.chat.id, message.message_id)
        with suppress(Exception):
            media_diagnostic(message, media_kind(message) or 'media',
                             'unexpected_archive_error', f'{type(exc).__name__}: {exc}')
        with suppress(Exception):
            await notify_media_issue(message, bot, 'download_failed',
                                     f'{type(exc).__name__}: {exc}')


@dp.business_message()
async def business_message(message: Message, bot: Bot):
    # Старые подключения могли быть добавлены до появления обработчика
    # business_connection. Подтягиваем владельца из Telegram один раз.
    if message.business_connection_id:
        await ensure_business_connection(bot, message.business_connection_id)

    logger.info('business_message: from=%s conn=%s text=%r',
                message.from_user.id if message.from_user else None,
                message.business_connection_id,
                (message.text or '')[:80])

    # MUTE applies to incoming contacts and Business-bot-generated echoes.
    # The human account owner who enabled MUTE is exempt; sender_business_bot
    # distinguishes messages actually sent by a connected Business bot.
    if message.business_connection_id:
        with db() as con:
            mute_row = get_scoped_mute_row(
                message.business_connection_id, message.chat.id
            )

        if mute_row:
            owner_message = is_human_owner_message(message)
            logger.info(
                'MUTE active: conn=%s chat=%s message=%s human_owner=%s automated_sender=%s via_bot=%s',
                message.business_connection_id, message.chat.id, message.message_id,
                owner_message, automated_sender_kind(message),
                getattr(getattr(message, 'via_bot', None), 'username', None),
            )
            is_notice = bool(
                (mute_row['button_message_id'] and
                 message.message_id == mute_row['button_message_id'])
                or (
                    (message.text or '') == '🔇 Этот чат находится в муте!'
                    and message.reply_markup
                    and any(
                        button.callback_data and button.callback_data.startswith('muteoff:')
                        for row in message.reply_markup.inline_keyboard
                        for button in row
                    )
                )
            )
            # Keep the MUTE control notice. All other Business-bot echoes are
            # deletable messages, not human-owner messages.
            if is_notice:
                return

            if owner_message:
                # The archive must include the owner's own side even while MUTE is active.
                save_message(message)
                if media_kind(message):
                    asyncio.create_task(_store_media_safely(message, bot))
                owner_text = _norm_cmd(message.text or '')
                if re.fullmatch(r'\.unmute', owner_text, re.IGNORECASE):
                    await handle_business_command(message, bot)
                    return
                # The human owner can continue chatting while MUTE is active.
                # Other dot-commands are ignored while MUTE is active.
                if owner_text.startswith(('.', '/')):
                    await delete_business_command(bot, message)
                return

            # Delete all non-human-owner messages while MUTE is active,
            # including sender_business_bot, via_bot (inline bot), sender_chat,
            # and regular bot-account messages. A bot-attributed message must
            # never inherit the human owner's MUTE exemption.
            await delete_muted_business_message(bot, message)
            return

    # Archive all conversation events, not only messages received from contacts.
    save_message(message)
    if media_kind(message):
        item = (message.photo[-1] if message.photo else message.video or message.voice or
                message.video_note or message.document or message.audio or
                message.animation or message.sticker)
        media_diagnostic(message, media_kind(message) or 'media', 'business_media_seen',
                         f'type={message.content_type}; file_id={bool(getattr(item,"file_id",None))}')
        asyncio.create_task(_store_media_safely(message, bot))
    if await handle_business_command(message, bot):
        return


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
        f'\n\n'
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
    # Keep MUTE enforcement for incoming/automated edits. Manual edits by
    # the connected account owner are exempt and still remain in the archive.
    if not is_human_owner_message(message):
        with db() as con:
            muted = get_scoped_mute_row(
                message.business_connection_id, message.chat.id
            )
        if muted:
            await delete_muted_business_message(bot, message)
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
        old_text = 'Не удалось получить исходное сообщение'
    with db() as con:
        con.execute('INSERT INTO message_versions(connection_id,chat_id,message_id,old_text,new_text) VALUES(?,?,?,?,?)',
                    (message.business_connection_id,message.chat.id,message.message_id,old_text,new_text))
        current = con.execute('''SELECT sender_id,sender_name,sender_username,chat_title,chat_username
            FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (message.business_connection_id,message.chat.id,message.message_id)).fetchone()
        owner = con.execute('SELECT user_id FROM connections WHERE id=?',(message.business_connection_id,)).fetchone()
    await store_media(message,bot)
    if owner and not is_owner_message(message) and prefs(owner['user_id'])['edits']:
        author = user_card(current, message.chat.id)
        body = (
            f'📝 <b>СООБЩЕНИЕ ИЗМЕНЕНО</b>\n'
            f'\n'
            f'👤 <b>Отправитель:</b> {author}\n\n'
            f'🔴 <b>Было:</b>\n{quote_block(old_text, "Исходный текст недоступен")}\n\n'
            f'🟢 <b>Стало:</b>\n{quote_block(new_text)}'
        )
        await owner_alert(bot,message.business_connection_id,body,parse_mode='HTML',
                          reply_markup=deleted_keyboard(message.chat.id,
                              (current['chat_username'] or current['sender_username']) if current else None))


def _row_source(row, use_local_first=False):
    """Возвращает источник медиа для строки (file_id или FSInputFile)."""
    fid = row['file_id'] or raw_message_file_id(row['raw_json'], row['content_type'])
    local = row['local_path'] if row['local_path'] and Path(row['local_path']).is_file() else None
    if use_local_first and local:
        return FSInputFile(local)
    return fid or (FSInputFile(local) if local else None)


def _deleted_header(row, chat_id):
    return (
        f'🚨 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n'
        f'\n'
        f'👤 <b>Отправитель:</b> {user_card(row, chat_id)}\n'
        f'🕒 <b>Время:</b> {datetime.now(MOSCOW_TZ).strftime("%H:%M") + " (мск)"}\n\n'
    )


async def _send_single_deleted(bot, owner_id, chat_id, row, prefs_dict):
    ct = row['content_type']
    restore_key = f"restore:{row['message_id']}:{chat_id}"
    keyboard = deleted_keyboard(chat_id, row['chat_username'] or row['sender_username'], restore_key)
    header = _deleted_header(row, chat_id)
    content = row['text'] or ({'sticker': '🎭 Стикер', 'photo': '📸 Фото', 'video': '🎬 Видео',
                               'voice': '🎤 Голосовое', 'video_note': '⭕ Видеокружок'}.get(
                               ct, '[' + (ct or 'сообщение') + ']'))
    body = header + quote_block(content, limit=2800)

    if ct in ('photo', 'video', 'voice', 'video_note', 'sticker', 'animation', 'document', 'audio') \
            and prefs_dict['deleted_media']:
        src = _row_source(row, use_local_first=False)
        if src:
            try:
                kw = dict(reply_markup=keyboard)
                if ct == 'photo':
                    await bot.send_photo(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'video':
                    await bot.send_video(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'voice':
                    await bot.send_voice(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'animation':
                    await bot.send_animation(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'document':
                    await bot.send_document(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'audio':
                    await bot.send_audio(owner_id, src, caption=body[:1024], parse_mode='HTML', **kw)
                elif ct == 'video_note':
                    await bot.send_video_note(owner_id, src)
                    await bot.send_message(owner_id, body, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await bot.send_sticker(owner_id, src)
                    await bot.send_message(owner_id, body, reply_markup=keyboard, parse_mode='HTML')
                return
            except Exception:
                logger.exception('Could not deliver deleted media; falling back to text')
                src_local = _row_source(row, use_local_first=True)
                if src_local and src_local != src:
                    try:
                        if ct == 'photo':
                            await bot.send_photo(owner_id, src_local, caption=body[:1024], parse_mode='HTML', reply_markup=keyboard)
                        elif ct == 'video':
                            await bot.send_video(owner_id, src_local, caption=body[:1024], parse_mode='HTML', reply_markup=keyboard)
                        else:
                            await bot.send_document(owner_id, src_local, caption=body[:1024], parse_mode='HTML', reply_markup=keyboard)
                        return
                    except Exception:
                        logger.exception('Local fallback also failed')
    try:
        await bot.send_message(owner_id, body, reply_markup=keyboard,
                               parse_mode='HTML', disable_web_page_preview=True)
    except Exception:
        logger.exception('Cannot notify about deletion')


async def _send_deleted_album(bot, owner_id, chat_id, rows):
    """Группирует 2+ фото/видео в один media_group."""
    if not rows or len(rows) < 2:
        return False
    ct = rows[0]['content_type']
    if ct not in ('photo', 'video'):
        return False
    items = []
    for r in rows[:10]:
        src = _row_source(r, use_local_first=False)
        if not src:
            continue
        if ct == 'photo':
            items.append(InputMediaPhoto(media=src))
        else:
            items.append(InputMediaVideo(media=src))
    if len(items) < 2:
        return False
    first = rows[0]
    header = (
        f'🚨 <b>УДАЛЁН АЛЬБОМ ({len(items)} {"фото" if ct=="photo" else "видео"})</b>\n'
        f'\n'
        f'👤 <b>Отправитель:</b> {user_card(first, chat_id)}\n'
        f'🕒 <b>Время:</b> {datetime.now(MOSCOW_TZ).strftime("%H:%M") + " (мск)"}'
    )
    items[0].caption = header[:1024]
    items[0].parse_mode = 'HTML'
    try:
        await bot.send_media_group(owner_id, items)
    except Exception:
        logger.exception('Album send via file_id failed, retrying local')
        items_local = []
        for r in rows[:10]:
            src = _row_source(r, use_local_first=True)
            if not src:
                continue
            if ct == 'photo':
                items_local.append(InputMediaPhoto(media=src))
            else:
                items_local.append(InputMediaVideo(media=src))
        if len(items_local) >= 2:
            items_local[0].caption = header[:1024]
            items_local[0].parse_mode = 'HTML'
            try:
                await bot.send_media_group(owner_id, items_local)
            except Exception:
                logger.exception('Album local send failed too')
                return False
        else:
            return False
    restore_key = f"restore:{first['message_id']}:{chat_id}"
    kb_first = deleted_keyboard(chat_id, first['chat_username'] or first['sender_username'], restore_key)
    try:
        await bot.send_message(owner_id,
            f'🗑 <i>Альбом из {len(items)} {"фото" if ct=="photo" else "видео"} удалён</i>',
            reply_markup=kb_first, parse_mode='HTML')
    except Exception:
        logger.exception('Could not send album summary')
    return True


@dp.deleted_business_messages()
async def deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot):
    logger.info("DELETED EVENT: connection=%s chat=%s msg_ids=%s",
                event.business_connection_id, event.chat.id, event.message_ids)

    # Не отправляем архивные уведомления о сообщениях из чата, пока он в MUTE.
    with db() as con:
        muted = get_scoped_mute_row(
            event.business_connection_id, event.chat.id
        )
    if muted:
        return

    new_ids = []
    with db() as con:
        for message_id in event.message_ids:
            con.execute('''UPDATE messages SET deleted=1,updated_at=CURRENT_TIMESTAMP
                WHERE connection_id=? AND chat_id=? AND message_id=?''',
                (event.business_connection_id, event.chat.id, message_id))
        owner = con.execute('SELECT user_id FROM connections WHERE id=?',
                            (event.business_connection_id,)).fetchone()
        for mid in event.message_ids:
            seen = con.execute('SELECT 1 FROM deleted_notified WHERE connection_id=? AND chat_id=? AND message_id=?',
                               (event.business_connection_id, event.chat.id, mid)).fetchone()
            if not seen:
                new_ids.append(mid)
                con.execute('INSERT OR IGNORE INTO deleted_notified(connection_id,chat_id,message_id) VALUES(?,?,?)',
                            (event.business_connection_id, event.chat.id, mid))

        if not new_ids:
            return
        rows = con.execute('''SELECT m.message_id,m.text,m.content_type,m.sender_id,m.sender_name,
            m.sender_username,m.chat_title,m.chat_username,m.raw_json, mf.local_path,mf.file_id
            FROM messages m LEFT JOIN media_files mf
            ON mf.connection_id=m.connection_id AND mf.chat_id=m.chat_id AND mf.message_id=m.message_id
            WHERE m.connection_id=? AND m.chat_id=? AND m.message_id IN (''' +
            ','.join('?' for _ in new_ids) + ') ORDER BY m.message_id',
            (event.business_connection_id, event.chat.id, *new_ids)).fetchall()

    if not owner:
        return
    user_prefs = prefs(owner['user_id'])
    if not user_prefs['deletions']:
        return

    rows = [r for r in rows if r['sender_id'] is not None and r['sender_id'] != owner['user_id']]
    if not rows:
        return

    chat_id = event.chat.id
    owner_id = owner['user_id']

    photos = [dict(r) for r in rows if r['content_type'] == 'photo' and user_prefs['deleted_media']]
    videos = [dict(r) for r in rows if r['content_type'] == 'video' and user_prefs['deleted_media']]
    handled_ids = set()

    if len(photos) >= 2:
        if await _send_deleted_album(bot, owner_id, chat_id, photos):
            handled_ids.update(r['message_id'] for r in photos)
    if len(videos) >= 2:
        if await _send_deleted_album(bot, owner_id, chat_id, videos):
            handled_ids.update(r['message_id'] for r in videos)

    for r in rows:
        if r['message_id'] in handled_ids:
            continue
        try:
            await _send_single_deleted(bot, owner_id, chat_id, dict(r), user_prefs)
        except Exception:
            logger.exception('Could not deliver single deleted message %s', r['message_id'])


@dp.callback_query(F.data.startswith('muteoff:'))
async def mute_off_button(callback: CallbackQuery, bot: Bot):
    token = (callback.data or '').partition(':')[2]
    msg = callback.message
    if not token or not msg or not hasattr(msg, 'message_id'):
        await callback.answer('Кнопка больше не активна.', show_alert=True)
        return

    with db() as con:
        row = con.execute(
            '''SELECT connection_id,chat_id,muted_by,button_message_id,business_owner_id
               FROM muted_chats WHERE button_token=?''',
            (token,),
        ).fetchone()

    message_connection_id = getattr(msg, 'business_connection_id', None)
    if (not row or row['chat_id'] != msg.chat.id
            or row['button_message_id'] != msg.message_id
            or (message_connection_id and row['connection_id'] != message_connection_id)
            or business_owner_id_for_connection(row['connection_id']) != row['business_owner_id']
            or not row['business_owner_id']):
        await callback.answer('Кнопка больше не активна.', show_alert=True)
        return

    if row['muted_by'] != callback.from_user.id:
        await callback.answer(
            'Только тот, кто включил mute, может его выключить.',
            show_alert=True,
        )
        return

    with db() as con:
        con.execute(
            '''DELETE FROM muted_chats
               WHERE button_token=? AND muted_by=? AND business_owner_id=?
                 AND connection_id=? AND chat_id=?''',
            (token, callback.from_user.id, row['business_owner_id'],
             row['connection_id'], row['chat_id']),
        )
        con.commit()
    record_mute_control(
        row['connection_id'], row['chat_id'], callback.from_user.id,
        'button_unmute', 'removed', msg.message_id,
        f'button_message_id={row["button_message_id"]}',
    )

    try:
        # После снятия mute полностью удаляем сообщение-уведомление из Business-чата.
        await bot.delete_business_messages(
            business_connection_id=row['connection_id'],
            message_ids=[msg.message_id],
        )
    except Exception:
        logger.exception('Could not delete mute notice after button click')
        # Если Telegram не разрешил удаление, хотя бы убираем красную кнопку.
        with suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                business_connection_id=row['connection_id'],
                reply_markup=None,
            )
    await callback.answer('Mute выключен.')


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
            (callback.from_user.id, chat_id, message_id)).fetchone()
    if not row or row['sender_id'] == callback.from_user.id:
        await callback.answer('Сообщение недоступно для восстановления', show_alert=True)
        return
    try:
        cid = row['connection_id']
        ct = row['content_type']
        cached_file_id = row['file_id'] or raw_message_file_id(row['raw_json'], row['content_type'])
        source = (row['local_path'] if row['local_path'] and Path(row['local_path']).is_file() else cached_file_id)
        if source and ct in ('photo','video','voice','video_note','sticker','animation','audio','document'):
            src = FSInputFile(source) if source == row['local_path'] else source
            caption = (row['text'] or '')[:1024] or None
            meth = {'photo': bot.send_photo, 'video': bot.send_video, 'voice': bot.send_voice,
                    'video_note': bot.send_video_note, 'sticker': bot.send_sticker,
                    'animation': bot.send_animation, 'audio': bot.send_audio, 'document': bot.send_document}[ct]
            arg = {'photo': 'photo', 'video': 'video', 'voice': 'voice', 'video_note': 'video_note',
                   'sticker': 'sticker', 'animation': 'animation', 'audio': 'audio', 'document': 'document'}[ct]
            kwargs = {'chat_id': chat_id, 'business_connection_id': cid, arg: src}
            if caption and ct not in ('sticker', 'video_note'):
                kwargs['caption'] = caption
            await meth(**kwargs)
        elif row['text']:
            await bot.send_message(chat_id, row['text'][:4096], business_connection_id=cid)
        else:
            await callback.answer('Нет доступной копии сообщения', show_alert=True)
            return
        await callback.answer('✅ Сообщение отправлено в чат', show_alert=True)
    except Exception:
        logger.exception('Failed to restore deleted message to chat')
        await callback.answer('Не удалось отправить. Проверь подключение и разрешения.', show_alert=True)


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
    'info': ('.info', 'Показывает информацию о собеседнике.'),
    'mute': ('.mute', 'Включает удаление новых входящих в этом чате.'),
    'unmute': ('.unmute', 'Выключает режим mute.'),
    'spam': ('.spam [N] [текст]', 'Отправляет N копий текста (20 / Premium 50).'),
    'archive': ('/archive', 'Последние 15 сообщений из архива.'),
    'ref': ('/ref', 'Реферальная ссылка и приглашённые.'),
    'settings': ('/settings', 'Настройки уведомлений.'),
    'mediacheck': ('/mediacheck', 'Диагностика медиа.'),
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
            f'<b>Условия:</b>\n1. Отправь ссылку другу.\n2. Друг запускает бота.\n'
            f'3. Друг <b>подключает бота</b> в настройках Telegram.\n4. <b>Вы оба</b> получаете <b>+1 день Premium</b>!')
    await message.answer(text, parse_mode='HTML')


@dp.message(Command('settings'))
async def settings(message: Message):
    if not message.from_user:
        return
    with db() as con:
        row = con.execute('SELECT alerts FROM settings WHERE user_id=?',(message.from_user.id,)).fetchone()
    state = row['alerts'] if row else 1
    await message.answer(f'🔔 Уведомления: {"включены" if state else "выключены"}',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Переключить',callback_data='settings:toggle')]]))


@dp.callback_query(F.data == 'settings:toggle')
async def toggle(callback: CallbackQuery):
    with db() as con:
        row = con.execute('SELECT alerts FROM settings WHERE user_id=?',(callback.from_user.id,)).fetchone()
        value = 0 if row is None or row['alerts'] else 1
        con.execute('INSERT INTO settings(user_id,alerts) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET alerts=excluded.alerts',(callback.from_user.id,value))
    await callback.answer('Настройки сохранены')
    if callback.message:
        await callback.message.answer('🔔 Уведомления '+('включены' if value else 'выключены'))


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
        f'\n'
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
            f'\n'
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
            '\n\nОтправь <b>любое сообщение</b> (текст, фото, видео, гиф, голосовое, кружок и т.д.), которое нужно разослать.'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action.startswith('run_broadcast_'):
        await call.answer('Запускаем!')
        msg_id = int(parts[2])
        ADMIN_ACTIONS.pop(call.from_user.id, None)
        await call.message.edit_text('⏳ <b>Рассылка запущена...</b>', parse_mode='HTML')
        with db() as con:
            user_ids = [r['id'] for r in con.execute('SELECT id FROM users').fetchall()]
        success = 0; blocked = 0; errors = 0
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
                    f'⏳ <b>Рассылка в процессе...</b>\n\nОтправлено: {i+1} из {len(user_ids)}\n'
                    f'✅ Успешно: {success}\n🚫 Заблокировали: {blocked}\n⚠️ Ошибок: {errors}',
                    parse_mode='HTML')
            await asyncio.sleep(0.05)
        final_text = (
            screen('РАССЫЛКА ЗАВЕРШЕНА') +
            f'\n\n📊 <b>Итоги:</b>\n✅ Доставлено: {success}\n🚫 Блок: {blocked}\n⚠️ Ошибок: {errors}'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await call.message.edit_text(final_text, parse_mode='HTML', reply_markup=markup)

    elif action == 'grant':
        await call.answer()
        ADMIN_ACTIONS[call.from_user.id] = {'action': 'waiting_grant'}
        text = (
            screen('ВЫДАЧА PREMIUM', 'Шаг 1 из 1') +
            '\n\nОтправь: <code>ID или @юзернейм Количество_дней</code>\n\nПримеры:\n'
            '<code>7831720836 30</code>\n<code>@nonLuks 7</code>'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action == 'roles':
        await call.answer()
        ADMIN_ACTIONS[call.from_user.id] = {'action': 'waiting_role'}
        text = (
            screen('УПРАВЛЕНИЕ РОЛЯМИ', 'Шаг 1 из 1') +
            '\n\nОтправь: <code>ID или @юзернейм роль</code>\n\n'
            'Роли: <code>owner</code>, <code>admin</code>, <code>aboba</code>\n'
            'Снять: <code>ID или @юзернейм none</code>'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='adm:home')]])
        await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)

    elif action == 'ui':
        await call.answer()
        await call.message.edit_text('🎨 <b>Выбери раздел для оформления:</b>', parse_mode='HTML', reply_markup=admin_ui_keyboard())

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
    await message.answer(f'✅ Фото для «{ADMIN_UI_SECTIONS[section]}» сохранено.', reply_markup=admin_ui_section_keyboard(section))


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
    await message.answer(f'✅ Текст для «{ADMIN_UI_SECTIONS[section]}» сохранён.', reply_markup=admin_ui_section_keyboard(section))


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
            return await message.answer('❌ Отправь текст: ID или @юзернейм Дни')
        ADMIN_ACTIONS.pop(message.from_user.id, None)
        parts = (message.text or '').split()
        if len(parts) != 2:
            return await message.answer('❌ Формат: <code>ID/Username Дни</code>', parse_mode='HTML')
        target, days_str = parts
        try:
            days = int(days_str)
            if days < 1 or days > 3650: raise ValueError
        except ValueError:
            return await message.answer('❌ Неверное количество дней (1–3650).')
        target_id = None
        if target.startswith('@'):
            with db() as con:
                row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
                if row: target_id = row['id']
        else:
            try: target_id = int(target)
            except ValueError: pass
        if not target_id:
            return await message.answer('❌ Пользователь не найден в базе.')
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        with db() as con:
            con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at', (target_id, expires))
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await message.answer(f'✅ <b>Premium выдан!</b>\n\nID: <code>{target_id}</code>\nСрок: <b>{days} дней</b>\nДо: <code>{expires}</code>', parse_mode='HTML', reply_markup=markup)
        try:
            await bot.send_message(target_id,
                f'👑 <b>ПОДПИСКА АКТИВИРОВАНА</b>\n\nPremium на <b>{days} дней</b>!',
                parse_mode='HTML')
        except Exception as e:
            logger.error(f"Notify premium: {e}")
        return

    elif action == 'waiting_role':
        if not message.text:
            return await message.answer('❌ Отправь: ID или @юзернейм Роль')
        ADMIN_ACTIONS.pop(message.from_user.id, None)
        parts = (message.text or '').split()
        if len(parts) != 2:
            return await message.answer('❌ Формат: <code>ID/Username роль</code>', parse_mode='HTML')
        target, role_str = parts
        role_str = role_str.lower()
        if role_str not in ('owner', 'admin', 'aboba', 'none'):
            return await message.answer('❌ Доступно: owner, admin, aboba, none')
        target_id = None
        if target.startswith('@'):
            with db() as con:
                row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
                if row: target_id = row['id']
        else:
            try: target_id = int(target)
            except ValueError: pass
        if not target_id:
            return await message.answer('❌ Пользователь не найден в базе.')
        with db() as con:
            if role_str == 'none':
                con.execute('DELETE FROM admins WHERE user_id=?', (target_id,))
                msg = f'✅ Роль снята с <code>{target_id}</code>.'
            else:
                con.execute('INSERT INTO admins(user_id, role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role', (target_id, role_str))
                msg = f'✅ Роль <b>{role_str}</b> выдана <code>{target_id}</code>.'
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='↩ В главное меню', callback_data='adm:home')]])
        await message.answer(msg, parse_mode='HTML', reply_markup=markup)
        try:
            if role_str == 'none':
                await bot.send_message(target_id, "⛔ <b>АДМИН-ДОСТУП ОТОЗВАН</b>", parse_mode='HTML')
            else:
                await bot.send_message(target_id, f"👑 <b>АДМИН-ДОСТУП ВЫДАН</b>\nРоль: <b>{role_str}</b>.", parse_mode='HTML')
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
                '\n\n1. Открой настройки Telegram.\n2. Перейди в «Чат-боты».'
                '\n3. Добавь <code>@' + escape(me.username or 'HadesSave_bot') + '</code>.'
                '\n4. Выдай разрешения.\n\nПосле подключения сообщения обрабатываются автоматически.')
        markup = setup_keyboard(me.username)
    elif section == 'setup_help':
        text = (screen('ПОМОЩЬ', 'Подключение HadesSave') +
                '\n\n👨‍💻 Контакт менеджера появится здесь после настройки.'
                '\n\nПока можно отправить: <code>/support Твой вопрос</code>')
        markup = kb([('🔌 Подключение', 'menu:setup')], [('🏠 Главное меню', 'menu:home')])
    elif section == 'setup_check':
        with db() as con:
            connected = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1', (uid,)).fetchone()[0]
        text = (screen('ПРОВЕРКА ПОДКЛЮЧЕНИЯ') +
                ('\n\n✅ Бот подключён!' if connected else '\n\n⚠️ Подключение пока не обнаружено.') +
                '\n\n🔌 Активных подключений: <b>' + str(connected) + '</b>')
        markup = kb([('🔄 Проверить ещё раз', 'menu:setup_check')], [('🔌 Как подключить', 'menu:setup')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('deleted','edited'):
        key = 'deletions' if section == 'deleted' else 'edits'
        p = prefs(uid)
        heading = '🗑 Удалённые' if section == 'deleted' else 'ℹ️ Изменённые'
        word = 'удалённых' if section == 'deleted' else 'изменённых'
        text = (f'<b>{heading}</b>\n\nБот присылает уведомления об {word} сообщениях.\n\n'
                'Выключение гасит только уведомления — сообщения сохраняются в архив.')
        markup = kb([(('✅ Включено' if p[key] else '❌ Выключено'), 'menu:deleted_toggle' if section=='deleted' else 'menu:edited_toggle')], [('↩ Назад', 'menu:home')])
    elif section == 'commands':
        text = (screen('💬 Список команд') +
                '\n\nТут вы можете ознакомиться со списком функций и их описанием.'
                '\n\n<blockquote>Команды с точкой (например .info) работают только в чате с собеседником.</blockquote>')
        markup = command_keyboard()
    elif section == 'features':
        text = '🛡 <b>ВОЗМОЖНОСТИ</b>\n\n\n📁 Архив сообщений\n🗑 Удаления и ✏️ правки в цитатах\n📸 Сохранение доступных медиа\n🔔 Уведомления владельцу\n👥 Рефералы и 💎 Premium'
        markup = kb([('🔌 Подключить', 'menu:setup'), ('📖 Команды', 'menu:commands')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('statistics','statistics_reset'):
        if section == 'statistics_reset':
            text = '📊 Сброс статистики пока не реализован.'
        else:
            with db() as con:
                count = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?',(uid,)).fetchone()[0]
                deleted = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.deleted=1',(uid,)).fetchone()[0]
                edited = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.edited=1',(uid,)).fetchone()[0]
            text = (screen('СТАТИСТИКА', 'Твой персональный обзор') + '\n\n'
                    f'💬 Сообщений: <b>{count}</b>\n🗑 Удалённых: <b>{deleted}</b>\n✏️ Изменённых: <b>{edited}</b>')
        markup = kb([('🔄 Обновить', 'menu:statistics')], [('📁 Архив', 'menu:archive'), ('👤 Профиль', 'menu:profile')], [('🏠 Главное меню', 'menu:home')])
    elif section == 'settings':
        text = screen('НАСТРОЙКИ', 'Управление уведомлениями') + '\n\n🔔 Управляй уведомлениями и медиа.\n\n👇 Нажми для переключения:'
        markup = settings_keyboard(uid)
    elif section == 'premium':
        with db() as con:
            row = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(uid,)).fetchone()
        text = (screen('PREMIUM', 'Особые возможности HadesSave') +
                f'\n\n⭐ <b>Подписка:</b> {escape(row["expires_at"] if row else "не активна")}\n\n'
                f'💫 <b>Покупка Premium доступна только в Mini App.</b>\n\n👇 Нажмите кнопку ниже.')
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🛒 Купить в Mini App', web_app=WebAppInfo(url="https://hadessavemod-art.github.io/hadesApp/#premium"))],
            [InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')]
        ])
    elif section == 'support':
        text = (screen('ПОДДЕРЖКА', 'Связь с администратором') +
                '\n\n💬 Нажмите на кнопку ниже.\n\n<i>Обычно отвечаем в течение дня.</i>')
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✍️ Написать в поддержку', url='https://t.me/Hades_save_men')],
            [InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:home')]
        ])
    elif section in ('invite','ref'):
        me = await bot.get_me()
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(uid,)).fetchone()[0]
        text = (screen('ПРИГЛАШЕНИЯ', 'Вместе удобнее') +
                f'\n\n👥 <b>Приглашено:</b> {count}\n\n'
                f'🔗 <b>Твоя ссылка:</b>\n<code>https://t.me/{escape(me.username or "")}?start=ref_{uid}</code>\n\n'
                f'<b>Условия:</b>\n1. Отправь ссылку.\n2. Друг запускает бота.\n'
                f'3. Друг <b>подключает бота</b>.\n4. <b>Вы оба</b> получаете <b>+1 день Premium</b>!')
    elif section == 'profile':
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1',(uid,)).fetchone()[0]
        text = (screen('МОЙ ПРОФИЛЬ', 'Твой аккаунт HadesSave') +
                f'\n\n🆔 <b>Telegram ID:</b> <code>{uid}</code>\n🔌 <b>Подключений:</b> {count}\n\n<i>Управляй через главное меню.</i>')
    elif section == 'archive':
        text = (screen('АРХИВ', 'Твоя история сообщений') + '\n\n📚 Открой последние записи командой:\n\n<code>/archive</code>')
    else:
        text = 'Раздел пока не перенесён.'
    await show_screen(callback, text, markup, section=section)


async def cleanup_mirrored_mute_rows(bot: Bot) -> int:
    """Remove old MUTE rows created when a mirrored contact .mute was accepted."""
    with db() as con:
        stale_rows = con.execute(
            """SELECT m.business_owner_id,m.connection_id,m.chat_id,
                      m.muted_by,m.button_message_id
                 FROM muted_chats m
                 JOIN connections c ON c.id=m.connection_id
                WHERE m.business_owner_id=c.user_id
                  AND m.muted_by IS NOT NULL
                  AND m.muted_by != c.user_id"""
        ).fetchall()

    removed = 0
    for row in stale_rows:
        if row['button_message_id']:
            try:
                await bot.delete_business_messages(
                    business_connection_id=row['connection_id'],
                    message_ids=[row['button_message_id']],
                )
            except Exception:
                logger.warning(
                    'Could not remove stale mirrored MUTE notice: owner=%s conn=%s chat=%s message=%s',
                    row['business_owner_id'], row['connection_id'], row['chat_id'],
                    row['button_message_id'], exc_info=True,
                )
        with db() as con:
            cur = con.execute(
                """DELETE FROM muted_chats
                    WHERE business_owner_id=? AND connection_id=? AND chat_id=?
                      AND muted_by=?""",
                (row['business_owner_id'], row['connection_id'], row['chat_id'], row['muted_by']),
            )
            removed += cur.rowcount
    if removed:
        logger.warning('Removed %s legacy mirrored MUTE rows from other Business accounts', removed)
    return removed


async def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit('Укажи BOT_TOKEN в переменных окружения')
    with db():
        pass
    logger.info('Starting HadesSave build %s; db_path=%s; media_dir=%s', BUILD_ID, DB_PATH, MEDIA_DIR)
    bot = Bot(TOKEN)
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await cleanup_mirrored_mute_rows(bot)

    mtproto_task = None
    try:
        from hades_mtproto_bridge import run_mtproto_bridge
        mtproto_task = asyncio.create_task(run_mtproto_bridge(bot, ADMIN_ID), name='hades-mtproto')
        def _mtproto_done(task):
            if not task.cancelled():
                try: task.result()
                except Exception: logger.exception('MTProto worker stopped')
        mtproto_task.add_done_callback(_mtproto_done)
    except ImportError:
        logger.info('MTProto bridge не установлен (telethon), пропускаем.')

    from admin_http import start_server
    admin_runner = await start_server(bot)
    from archive_retention import cleanup_loop
    retention_task = asyncio.create_task(cleanup_loop(db, MEDIA_DIR))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([
            BotCommand(command='start', description='🏠 Главное меню'),
            BotCommand(command='help', description='📖 Список команд'),
            BotCommand(command='mutediag', description='🔇 Диагностика MUTE'),
            BotCommand(command='mediacheck', description='📁 Диагностика медиа'),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        updates = sorted(set(dp.resolve_used_update_types()) | {
            'business_connection', 'business_message', 'edited_business_message',
            'deleted_business_messages', 'message', 'callback_query', 'pre_checkout_query'
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
