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
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, Update, BotCommand, ReplyKeyboardRemove, MenuButtonCommands, ReplyParameters

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
# Установлено True, чтобы сохранять медиа из ответов без обязательного маркера view-once.
CAPTURE_UNMARKED_REPLIED_MEDIA = False
DB_PATH = Path(os.getenv('DB_PATH', 'bot.sqlite3'))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('business_bot')
BUILD_ID = 'HADES-ANTIMUTE-20261024-01'
dp = Dispatcher()

# .antimute is deliberately in-memory: it expires after one hour and is off
# again automatically after a process restart, as requested.
ANTIMUTE_ACTIVE: dict[tuple[str, int], float] = {}
ANTIMUTE_SENDS: dict[tuple[str, int], deque] = {}
ANTIMUTE_COPIES: dict[tuple[str, int, int], float] = {}
ANTIMUTE_LOCK = asyncio.Lock()
ANTIMUTE_DURATION_SECONDS = 60 * 60
ANTIMUTE_LIMIT_PER_MINUTE = 30
ANTIMUTE_COPY_MARKER_TTL = 120


def screen(title: str, subtitle: str = '') -> str:
    """Clean headings without decorative frames."""
    return f'<b>{title}</b>' + (f'\n<i>{subtitle}</i>' if subtitle else '')


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS messages (connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sender_id INTEGER, text TEXT, content_type TEXT, raw_json TEXT NOT NULL, edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))''')
    connection.execute('CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS keyboard_cleanup (user_id INTEGER PRIMARY KEY)')
    connection.execute('CREATE TABLE IF NOT EXISTS premium (user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS support (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, body TEXT NOT NULL, status TEXT DEFAULT "open", created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
    connection.execute('CREATE TABLE IF NOT EXISTS notification_prefs (user_id INTEGER PRIMARY KEY, edits INTEGER DEFAULT 1, deletions INTEGER DEFAULT 1, deleted_media INTEGER DEFAULT 1)')
    # Tables for bot-side visual editor. CREATE IF NOT EXISTS preserves existing design.
    connection.execute('''CREATE TABLE IF NOT EXISTS ui_design (
        section TEXT PRIMARY KEY,
        photo_id TEXT,
        custom_text TEXT
    )''')
    connection.execute('''CREATE TABLE IF NOT EXISTS admin_edit_state (
        user_id INTEGER PRIMARY KEY,
        section TEXT NOT NULL,
        mode TEXT NOT NULL
    )''')
    connection.execute('CREATE TABLE IF NOT EXISTS muted_chats (connection_id TEXT, chat_id INTEGER, PRIMARY KEY(connection_id,chat_id))')
    connection.execute('CREATE TABLE IF NOT EXISTS spam_cooldowns (connection_id TEXT PRIMARY KEY, last_at REAL)')
    connection.execute('CREATE TABLE IF NOT EXISTS media_files (connection_id TEXT,chat_id INTEGER,message_id INTEGER,local_path TEXT,PRIMARY KEY(connection_id,chat_id,message_id))')
    media_cols = {row[1] for row in connection.execute('PRAGMA table_info(media_files)')}
    if 'file_id' not in media_cols:
        connection.execute('ALTER TABLE media_files ADD COLUMN file_id TEXT')
    connection.execute('''CREATE TABLE IF NOT EXISTS media_diagnostics (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, message_id INTEGER, kind TEXT, status TEXT, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    diagnostic_cols = {row[1] for row in connection.execute('PRAGMA table_info(media_diagnostics)')}
    if 'connection_id' not in diagnostic_cols:
        connection.execute('ALTER TABLE media_diagnostics ADD COLUMN connection_id TEXT')
    connection.execute('''CREATE TABLE IF NOT EXISTS business_update_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id TEXT, update_type TEXT NOT NULL, chat_id INTEGER,
        message_id INTEGER, content_type TEXT, has_reply INTEGER,
        has_media_file INTEGER, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_business_update_diagnostics_conn ON business_update_diagnostics(connection_id,id)')
    connection.execute('CREATE TABLE IF NOT EXISTS reply_captures (connection_id TEXT, chat_id INTEGER, message_id INTEGER, captured_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(connection_id,chat_id,message_id))')
    connection.execute('CREATE TABLE IF NOT EXISTS message_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,connection_id TEXT,chat_id INTEGER,message_id INTEGER,old_text TEXT,new_text TEXT,changed_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    # Migration for existing databases on Bothost: preserve old messages.
    cols = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
    for column in ('sender_name', 'sender_username', 'chat_title', 'chat_username'):
        if column not in cols:
            connection.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
    connection.commit()
    return connection


# Low-risk event tracing: no message bodies, media bytes, file IDs or tokens stored.
# Registered on raw Updates, BEFORE business-message handlers can early-return.
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
            # Replied message's media TYPE only, never its text or file ID.
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
                # Keep logs bounded on low-cost hosts.
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
    # Удаляем старую закреплённую Reply-клавиатуру только при первом запуске
    # обновлённой версии. Кнопка «Меню» — системная кнопка Telegram.
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
    await message.answer('Команды: /start, /help, /archive, /ref, /premium, /support, /settings, /mediacheck. В подключённом чате: .antimute, .antimute on, .antimute off, .antimute status. Администратору: /admin, /stats, /broadcast, /grant.')


@dp.message(Command('mediacheck'))
async def mediacheck(message: Message):
    if not message.from_user:
        return
    with db() as con:
        connected = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1',
                                (message.from_user.id,)).fetchone()[0]
        # Do NOT join against archived messages: owners' replies are intentionally
        # excluded from the archive and view-once messages may lack a payload.
        sql = ("SELECT d.kind,d.status,d.details,d.created_at FROM media_diagnostics d "
               "JOIN connections c ON c.id=d.connection_id WHERE c.user_id=? "
               "ORDER BY d.id DESC LIMIT 12")
        records = con.execute(sql, (message.from_user.id,)).fetchall()
    lines = [f'🔎 <b>ПРОВЕРКА МЕДИА</b>', f'🧩 Версия кода: <code>{BUILD_ID}</code>', f'🔌 Активных Подключений: <b>{connected}</b>']
    if not records:
        lines.append('Нет попыток обработки медиа. Пока нет данных для проверки.')
    for r in records:
        lines.append(f'\n{escape(r["kind"])} · <code>{escape(r["status"])}</code>\n{escape((r["details"] or "")[:180])}')
    lines.append('\nРежим ответов на медиа: ' + ('максимальный (может пересылать и обычное медиа)' if CAPTURE_UNMARKED_REPLIED_MEDIA else 'строгий (только при распознанном признаке одноразовости)'))
    if any(r['status'] in ('not_view_once_no_payload', 'not_view_once_payload_available') for r in records):
        lines.append('\n⚠️ В этом ответе Telegram не передал распознаваемый признак одноразовости. В строгом режиме медиа не отправляется в личный чат бота.')
    await message.answer('\n'.join(lines), parse_mode='HTML')


@dp.message(Command('mtstatus'))
async def mtstatus(message: Message):
    """Only the configured owner sees MTProto runtime diagnostics."""
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    if os.getenv('HADES_MT_ENABLED', 'false').lower() not in {'true', '1', 'yes', 'on'}:
        return await message.answer('MTProto выключен. Установи HADES_MT_ENABLED=true.')
    root = Path(os.getenv('HADES_MT_DATA') or '/app/data/mtproto')
    status_file = root / 'status.json'
    if not status_file.is_file():
        return await message.answer('MTProto: статус отсутствует. Проверь telethon, настройки подключения и логи запуска.')
    try:
        data = json.loads(status_file.read_text(encoding='utf-8'))
        ts = int(data.get('updated_at', 0))
        age = max(0, int(__import__('time').time()) - ts)
        count = int(data.get('watch_count', 0))
        state = escape(str(data.get('state', 'unknown')))
        await message.answer(f'🔎 MTProto: <b>{state}</b>\nЧатов в списке: <b>{count}</b>\nСтатус обновлён {age} сек. назад.', parse_mode='HTML')
    except (OSError, ValueError, TypeError):
        await message.answer('MTProto: не удалось прочитать статус. Проверь логи.')


@dp.message(Command('eventcheck'))
async def eventcheck(message: Message):
    # Show Telegram update delivery status, without message content.
    if not message.from_user:
        return
    with db() as con:
        conn = con.execute('SELECT id,enabled FROM connections WHERE user_id=?',
                           (message.from_user.id,)).fetchall()
        ids = [r['id'] for r in conn]
        if ids:
            placeholders = ','.join('?' for _ in ids)
            records = con.execute(f'''SELECT update_type,content_type,has_reply,has_media_file,
                details,created_at FROM business_update_diagnostics
                WHERE connection_id IN ({placeholders}) ORDER BY id DESC LIMIT 15''', ids).fetchall()
            counts = con.execute(f'''SELECT update_type,COUNT(*) AS cnt FROM business_update_diagnostics
                WHERE connection_id IN ({placeholders}) GROUP BY update_type''', ids).fetchall()
        else:
            records, counts = [], []
        media_rows = con.execute('''SELECT d.kind,d.status,d.details,d.created_at
            FROM media_diagnostics d JOIN connections c ON c.id=d.connection_id
            WHERE c.user_id=? ORDER BY d.id DESC LIMIT 12''',
            (message.from_user.id,)).fetchall()
    out = ['📡 <b>ДИАГНОСТИКА ОБНОВЛЕНИЙ</b>',
           f'🔌 Подключений: <b>{len(conn)}</b> (активных: {sum(int(r["enabled"]) for r in conn)})',
           '🧪 Это техническая проверка, а не содержимое файлов.']
    if counts:
        out.append('\n<b>Получено:</b>')
        out.extend(f'• <code>{escape(r["update_type"])}</code>: {r["cnt"]}' for r in counts)
    else:
        out.append('\n⚠️ Ни одного обновления для твоих подключений после установки этой версии.')
    if records:
        out.append('\n<b>Последние обновления:</b>')
        for row in records:
            status = ('reply=' + str(row['has_reply']) + ', media=' + str(row['has_media_file']))
            out.append('• ' + escape(row['created_at']) + ' <code>' + escape(row['update_type']) + '</code>'
                       + (' / ' + escape(row['content_type']) if row['content_type'] else '')
                       + ' (' + status + ')' + (' ' + escape(row['details']) if row['details'] else ''))
    out.append('\n<b>Попытки отправки медиа:</b>')
    if media_rows:
        for row in media_rows:
            out.append('• ' + escape(row['created_at']) + ' <code>' + escape(row['status'])
                       + '</code> [' + escape(row['kind']) + '] '
                       + escape((row['details'] or '')[:120]))
    else:
        out.append('• Нет записей об отправке. Если видишь reply=1 выше, это может означать, что ответ не дошёл до обработчика медиа.')
    await message.answer('\n'.join(out)[:3900], parse_mode='HTML')


@dp.message(Command('stats'))
async def stats(message: Message):
    if not message.from_user or not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    with db() as con:
        users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        connections = con.execute('SELECT COUNT(*) FROM connections').fetchone()[0]
        messages = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0]
    await message.answer(f'📊 Пользователи: {users}\nПодключения: {connections}\nСообщения: {messages}\nУдалённые: {deleted}')


@dp.business_connection()
async def business_connection(event: BusinessConnection, bot: Bot):
    """Tell the actual Business owner about new connections and disconnections."""
    with db() as con:
        previous = con.execute('SELECT enabled FROM connections WHERE id=?', (event.id,)).fetchone()
        con.execute("""INSERT INTO connections(id,user_id,enabled) VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,
            enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
            (event.id, event.user.id, int(event.is_enabled)))
    # A change in permissions may also produce an update; do not spam the owner.
    if previous is not None and bool(previous['enabled']) == bool(event.is_enabled):
        logger.info('Business connection settings updated: %s', event.id)
        return
    username = (await bot.get_me()).username or ''
    if event.is_enabled:
        notice = (
            screen('HADES SAVE · ONLINE', 'Подключение активировано') +
            '\n\n✅ <b>Подключение выполнено!</b>'
            '\n\n🛡 <b>Теперь HadesSave умеет:</b>'
            '\n├ 🗑 Уведомлять об удалениях'
            '\n├ ✏️ Показывать изменения в цитатах'
            '\n╰ 📸 Обрабатывать доступные медиа'
            '\n\n⚙️ Настроить уведомления: /settings'
            f'\n🤖 <b>Бот:</b> @{escape(username)}'
            '\n\n<i>Спасибо, что выбрал HadesSave ✦</i>'
        )
    else:
        notice = (
            '🔌 <b>HADES SAVE ОТКЛЮЧЁН</b>\n'
            '━━━━━━━━━━━━━━\n\n'
            'Telegram отключён от бота.\n'
            'Подключение отключено.\n\n'
            'Для повторного подключения открой Telegram → Настройки → Чат-боты.'
        )
    targets = [event.user_chat_id]
    if event.user.id not in targets:
        targets.append(event.user.id)
    for target in targets:
        try:
            await bot.send_message(target, notice, parse_mode='HTML', reply_markup=menu(event.user.id))
            logger.info('Business status notification sent to owner chat=%s', target)
            break
        except Exception as exc:
            logger.warning('Failed to notify Business owner chat=%s: %s', target, exc)
    else:
        logger.error('Business owner notification not delivered; owner should open /start')


def is_owner_message(message: Message):
    """Never archive messages sent by the connected Business account itself."""
    if not message.business_connection_id or not message.from_user:
        return False
    with db() as con:
        owner = con.execute('SELECT user_id FROM connections WHERE id=?',
                            (message.business_connection_id,)).fetchone()
    return bool(owner and message.from_user.id == owner['user_id'])


def save_message(message: Message, edited=False):
    if not message.business_connection_id or is_owner_message(message):
        return
    payload = message.model_dump_json(exclude_none=True)
    text = message.text or message.caption or (
        '🎭 Стикер' + (f' {message.sticker.emoji}' if message.sticker.emoji else '')
        if message.sticker else '')
    with db() as con:
        con.execute('''INSERT INTO messages(connection_id,chat_id,message_id,sender_id,text,content_type,raw_json,edited) VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET sender_id=excluded.sender_id,text=excluded.text,content_type=excluded.content_type,raw_json=excluded.raw_json,edited=MAX(messages.edited,excluded.edited),updated_at=CURRENT_TIMESTAMP''',
          (message.business_connection_id, message.chat.id, message.message_id,
           message.from_user.id if message.from_user else None, text, message.content_type, payload, int(edited)))
        # Persist sender identity separately: Telegram deletion updates contain no sender.
        sender = message.from_user
        name = (' '.join(filter(None, (sender.first_name, sender.last_name))).strip()
                if sender else (message.sender_business_bot.username if message.sender_business_bot else None))
        # In a private chat chat.username is a useful fallback for incoming messages.
        username = (sender.username if sender else None) or (message.chat.username if message.chat.type == 'private' else None)
        con.execute('''UPDATE messages SET sender_name=COALESCE(?,sender_name),
            sender_username=COALESCE(?,sender_username),chat_title=COALESCE(?,chat_title),
            chat_username=COALESCE(?,chat_username)
            WHERE connection_id=? AND chat_id=? AND message_id=?''',
            (name,username,message.chat.full_name or message.chat.title,
             message.chat.username,message.business_connection_id,message.chat.id,message.message_id))


MEDIA_DIR = Path(os.getenv('MEDIA_DIR') or ('/app/data/media' if Path('/app/data').is_dir() else str(Path(__file__).resolve().parent / 'media'))).resolve()

async def store_media(message: Message, bot: Bot) -> bool:
    # Cache accessible incoming media before it can be deleted. The Telegram
    # file_id is saved first as a fallback; local downloads use retries and an
    # atomic rename so interrupted downloads aren't mistaken for valid files.
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

    # The standard hosted Bot API only downloads files up to 20 MB. Keep the
    # file_id for re-sending, but skip futile download retries when size is known.
    file_size = getattr(item, 'file_size', None)
    max_download_mb = max(1, int(os.getenv('BOT_API_DOWNLOAD_MAX_MB', '20')))
    if file_size and file_size > max_download_mb * 1024 * 1024:
        media_diagnostic(message, media_kind(message) or 'media', 'file_id_only_size_limit',
                         f'bytes={file_size}; download_limit_mb={max_download_mb}')
        logger.info('Media exceeds configured Bot API download limit: chat=%s message=%s bytes=%s',
                    chat_id, message_id, file_size)
        return False

    # Preserve the Telegram file_id even if the host's media directory is
    # unavailable, so deletion notifications can still try to re-send the file.
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        media_diagnostic(message, media_kind(message) or 'media', 'file_id_saved_disk_unavailable',
                         f'{type(exc).__name__}: {exc}')
        logger.exception('Cannot create media cache folder for chat=%s message=%s', chat_id, message_id)
        return False

    # Some Telegram files cannot be downloaded via the hosted Bot API (for
    # example when they exceed its download limit). Keep file_id regardless.
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


# Send accessible incoming media to the connected Business account owner immediately.
# Telegram does not expose view-once media to bots when its file payload is absent.
AUTO_FORWARD_MEDIA = False  # Never forward ordinary incoming media

def media_diagnostic(message: Message, kind: str, status: str, details: str = ''):
    # Avoid recording private message contents or bot credentials in diagnostics.
    with db() as con:
        con.execute('INSERT INTO media_diagnostics(connection_id,chat_id,message_id,kind,status,details) VALUES(?,?,?,?,?,?)',
                    (message.business_connection_id, message.chat.id, message.message_id, kind, status, str(details)[:350]))


async def deliver_incoming_media_now(message: Message, bot: Bot, force: bool = False):
    """Send accessible Business media immediately to its owner."""
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

    # Подпись целиком отображается в цитате; эмодзи оставлен прежним.
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
    """Restore media using its original Telegram type (including stickers)."""
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

    # Handle .antimute before the ordinary command allow-list.
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
                else:  # status
                    remaining = max(0, int(expires - now))
                    if remaining:
                        notice = f'🛡 Антимут включён в этом чате. Осталось примерно {max(1, (remaining + 59) // 60)} мин.'
                    else:
                        notice = '🛡 Антимут выключен в этом чате.'
        # Send the status in the current dialogue. Mark the returned message id
        # before releasing the lock so it can never be replaced by its own mode.
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
        with db() as con:
            row = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(owner,)).fetchone()
        is_premium = bool(row and datetime.fromisoformat(row['expires_at'])>datetime.now(timezone.utc))
        limit = 50 if is_premium else 20
        cooldown = 180 if is_premium else 300
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
    """Best-effort detection of a view-once marker exposed by Telegram.

    Bot API versions may preserve unknown fields in Pydantic's ``model_extra``.
    Inspect both the message and media objects. Do not infer view-once from a
    reply alone, a spoiler, or protected-content flag: those are not equivalent.
    Returns (detected, marker_name) for diagnostics without logging message data.
    """
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

    # Inspect the message plus each media object explicitly. For photos, also
    # inspect individual PhotoSize entries; some clients attach extra fields there.
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
    """Recover a file_id from a saved Bot API payload if media_files is missing."""
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
    # Defensive fallback for payloads whose content type was not stored correctly.
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
    """Any reply from the Business owner requests the replied-to media.

    No special command is needed. Telegram may omit the media payload on
    view-once messages; when absent, only already-cached media can be sent.
    """
    owner = await business_owner(message)
    replied = message.reply_to_message
    if not owner or not message.from_user or message.from_user.id != owner:
        return False
    if not replied:
        return False

    # --- ГЛАВНАЯ ПРОВЕРКА: ТОЛЬКО ОДНОРАЗОВЫЕ (ИЛИ ЗАЩИЩЁННЫЕ) ---
    # Если в ответе нет флага has_protected_content — это обычное сообщение, игнорируем.
    # Telethon видит одноразовые по ttl_seconds, а Bot API — по has_protected_content.
    is_protected = getattr(replied, 'has_protected_content', False)
    
    item = (replied.photo[-1] if replied.photo else replied.video or replied.voice or
            replied.video_note or replied.document or replied.audio or replied.animation or replied.sticker)
    ttl = getattr(item, 'ttl_seconds', None)

    if not is_protected and not ttl:
        # Это обычное фото/видео. Не сохраняем.
        media_diagnostic(message, 'reply', 'not_view_once_ignored',
                         f'type={replied.content_type}; protected={is_protected}; ttl={ttl}')
        return False

    # Ответ на стикер не вызывает немедленную пересылку: только удаление.
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

    # Если Telegram прислал файл в ответе, но мы его ещё не скачали — качаем.
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

    # Если в ответе не было медиа, но оно есть в кэше — достаём из кэша
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
    # Leave albums, commands, forwarded messages and protected content alone.
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
    # Replace an eligible owner message only after Telegram confirms the copy.
    if not message.business_connection_id or not is_owner_message(message):
        return False
    key = (message.business_connection_id, message.chat.id)
    message_key = (*key, message.message_id)

    async with ANTIMUTE_LOCK:
        now = time.monotonic()
        # Ignore updates produced by messages this handler itself sent.
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
            return True  # Do not delete the original if no replacement was sent.

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
                # Original reply target is no longer available: send a standalone copy.
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
                # Copy is already confirmed; deletion failure leaves the original intact.
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
    # Cache incoming messages/media silently for possible future deletion/edit.
    # Any owner reply to media triggers a save/send attempt below.
    # Owner's own messages are not archived.
    if not is_owner_message(message):
        save_message(message)
        if not media_kind(message):
            # Official Bot API may represent view-once media as a placeholder
            # with no usable file_id. Record metadata only, not message text.
            hints = []
            for field in ('has_media_spoiler', 'ttl_seconds', 'view_once'):
                value = getattr(message, field, None)
                if value is not None:
                    hints.append(f'{field}={value}')
            media_diagnostic(message, 'incoming', 'no_media_payload',
                             f'type={message.content_type}; ' + ','.join(hints))
        # Cache incoming media quietly before deletion. Never forward ordinary
        # media on arrival; it is delivered only after a deletion update.
        # The sender can mark a message as protected; this flag does not
        # grant the bot access to view-once file bytes.
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
    # Commands are handled first, so replying to media with .antimute or another
    # command cannot accidentally invoke the replied-media path.
    if await handle_business_command(message, bot):
        return
    # Process antimute before the optional replied-media capture. This also
    # catches our own replacement-message IDs and prevents replacement loops.
    if is_owner_message(message) and await _copy_owner_message_for_antimute(message, bot):
        return
    # Existing replied-media behavior remains when antimute did not handle it.
    if is_owner_message(message) and message.reply_to_message:
        await capture_replied_media(message, bot)
    with db() as con:
        muted = con.execute('SELECT 1 FROM muted_chats WHERE connection_id=? AND chat_id=?',
                            (message.business_connection_id,message.chat.id)).fetchone()
    owner = await business_owner(message)
    if muted and owner and (not message.from_user or message.from_user.id != owner):
        await delete_business_command(bot,message)


def quote_block(value, placeholder='Сообщение без текста', limit=1500):
    """Telegram native expandable? No: standard blockquote supported by HTML Bot API."""
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
    """Edit in place, including images; replace message only when Telegram can't change media type."""
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
            # Keep the old photo when changing screen: no extra message.
            await msg.edit_caption(caption=shown[:1024], parse_mode='HTML', reply_markup=markup)
        else:
            await msg.edit_text(shown[:4096], reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower():
            logger.exception('Cannot edit menu screen')


async def send_home(message):
    name = escape(message.from_user.first_name or 'друг') if message.from_user else 'друг'
    default = f'Привет, {name}!\n\nВыберите раздел:'
    design, text = render_design('home', default)
    if design['photo_id']:
        await message.answer_photo(design['photo_id'], caption=text[:1024], parse_mode='HTML', reply_markup=menu(message.from_user.id))
    else:
        await message.answer(text[:4096], reply_markup=menu(message.from_user.id), parse_mode='HTML')


def identity_label(row, fallback_chat=None):
    # Match PHP senderLabel: readable name + @username when known.
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
    """Only jump to chat and, for deletions, restore to that chat."""
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
    """Readable representation of any content, even when no text is present."""
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
    # No archived original means the original text cannot be recovered.
    # Some edits only change the caption or include non-text media.
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
        body = (f'✏️ <b>Изменено сообщение от {author}:</b>'
                f'\n\n<b>Было:</b> {quote_block(old_text, "Исходный текст недоступен")}'
                f'\n\n<b>Стало:</b> {quote_block(new_text)}')
        await owner_alert(bot,message.business_connection_id,body,parse_mode='HTML',
                          reply_markup=deleted_keyboard(message.chat.id,
                              (current['chat_username'] or current['sender_username']) if current else None))
        # No automatic media duplicate on edit; text notification is sufficient.


@dp.deleted_business_messages()
async def deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot):
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
    # The Bot API deletion update contains IDs only. If a message was never
    # delivered to the bot (or was delivered without a usable payload), don't
    # silently drop the deletion event: send a truthful metadata-only notice.
    known_ids = {int(row['message_id']) for row in rows}
    missing_ids = [int(mid) for mid in event.message_ids if int(mid) not in known_ids]
    for missing_id in missing_ids:
        try:
            body = ('🗑 <b>Сообщение удалено</b>\n\n'
                    'HadesSave не получил содержимое этого сообщения до удаления, '
                    'поэтому восстановить его текст или медиа не удалось.')
            await bot.send_message(
                owner['user_id'], body,
                reply_markup=deleted_keyboard(event.chat.id, getattr(event.chat, 'username', None)),
                parse_mode='HTML', disable_web_page_preview=True)
            media_diagnostic(
                Message.model_construct(
                    business_connection_id=event.business_connection_id,
                    chat=event.chat, message_id=missing_id, content_type='unknown'),
                'deletion', 'missing_message_snapshot',
                f'message_id={missing_id}; deletion_update_had_no_content')
        except Exception:
            logger.exception('Cannot notify about deleted message without a saved snapshot')
    for row in rows:
        # Skip notifications for the Business owner's own messages.
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
        body = (f'🗑 <b>Удалено сообщение от {author}:</b>\n\n'
                + quote_block(content,limit=2800))
        if ct in ('photo','video','voice','video_note','sticker','animation','document','audio') and media_present:
            # Deliver the deleted file only now, with controls attached to the
            # media message whenever that media type supports a caption.
            from aiogram.types import FSInputFile
            src = (FSInputFile(row['local_path']) if row['local_path'] and Path(row['local_path']).is_file() else available_file_id)
            if src and prefs(owner['user_id'])['deleted_media']:
                try:
                    kw = dict(reply_markup=keyboard, business_connection_id=None)
                    kw.pop('business_connection_id')
                    if ct == 'photo':
                        await bot.send_photo(owner['user_id'],src,caption=f'🗑 Фото удалено.\nОтправитель: {author}',parse_mode='HTML',**kw)
                    elif ct == 'video':
                        await bot.send_video(owner['user_id'],src,caption=f'🗑 Видео удалено.\nОтправитель: {author}',parse_mode='HTML',**kw)
                    elif ct == 'voice':
                        await bot.send_voice(owner['user_id'],src,caption=f'🗑 Голосовое удалено.\nОтправитель: {author}',parse_mode='HTML',**kw)
                    elif ct == 'animation':
                        await bot.send_animation(owner['user_id'],src,caption=f'🗑 GIF удалён.\nОтправитель: {author}',parse_mode='HTML',**kw)
                    elif ct == 'document':
                        await bot.send_document(owner['user_id'],src,caption=f'🗑 Файл удалён.\nОтправитель: {author}',parse_mode='HTML',**kw)
                    elif ct == 'audio':
                        await bot.send_audio(owner['user_id'],src,caption=f'🗑 Аудио удалено.\nОтправитель: {author}',parse_mode='HTML',**kw)
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
    """Restore previously deleted content using the authorized connection."""
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
            rows.append([('🚀 Подключить HadesSave', 'menu:setup')])
    rows.extend([
        [('🤖 Мой HadesSave', 'menu:profile')],
        [('🗑 Удалённые', 'menu:deleted'), ('ℹ️ Изменённые', 'menu:edited')],
        [('💬 Команды', 'menu:commands'), ('📈 Статистика', 'menu:statistics')],
        [('🎁 Пригласить друга', 'menu:invite')],
        [('⚙️ Настройки', 'menu:settings')],
        [('💎 HadesSave Premium', 'menu:premium')],
        [('💡 Поддержка', 'menu:support')],
    ])
    return kb(*rows)


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
    commands = ['info', 'mute', 'unmute', 'spam', 'archive', 'ref', 'premium', 'support', 'settings', 'mediacheck']
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
    'spam': ('.spam [N] [текст]', 'Отправляет N копий текста. Лимит: 20 сообщений (Premium — 50), не более 20 слов; задержка перед повтором. Кнопка «Стоп» и команда .bspam пока не реализованы.'),
    'archive': ('/archive', 'Показывает последние 15 сообщений из твоего архива.'),
    'ref': ('/ref', 'Показывает реферальную ссылку и число приглашённых.'),
    'premium': ('/premium', 'Показывает Premium и тарифы Telegram Stars.'),
    'support': ('/support Текст вопроса', 'Отправляет обращение в поддержку.'),
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
    await message.answer(f'👥 Приглашено: {count}\nТвоя ссылка: https://t.me/{me.username}?start=ref_{message.from_user.id}')


@dp.message(Command('premium'))
async def premium(message: Message):
    if not message.from_user:
        return
    with db() as con:
        row = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(message.from_user.id,)).fetchone()
    status = f"Активен до {row['expires_at']} UTC" if row and datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc) else 'Не активен'
    buttons = [[InlineKeyboardButton(text=f'⭐ {days} дней — {stars} Stars', callback_data=f'buy:{days}')] for days, stars in ((7,50),(14,100),(30,300))]
    await message.answer(f'⭐ Premium: {status}',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith('buy:'))
async def buy(callback: CallbackQuery, bot: Bot):
    days = int(callback.data.split(':')[1])
    plans = {7:50,14:100,30:300}
    if days not in plans:
        return await callback.answer('Тариф не найден', show_alert=True)
    await bot.send_invoice(chat_id=callback.from_user.id,title=f'Premium на {days} дней',description='Подписка Premium',payload=f'premium:{days}',provider_token='',currency='XTR',prices=[LabeledPrice(label=f'{days} дней',amount=plans[days])])
    await callback.answer()


@dp.pre_checkout_query()
async def checkout(query: PreCheckoutQuery):
    await query.answer(ok=query.invoice_payload in {'premium:7','premium:14','premium:30'})


@dp.message(F.successful_payment)
async def paid(message: Message):
    if not message.from_user or not message.successful_payment:
        return
    payload = message.successful_payment.invoice_payload
    if not payload.startswith('premium:'):
        return
    try:
        days = int(payload.split(':')[1])
    except ValueError:
        return
    if days not in (7,14,30) or message.successful_payment.currency != 'XTR' or message.successful_payment.total_amount != {7:50,14:100,30:300}[days]:
        return
    with db() as con:
        old = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(message.from_user.id,)).fetchone()
        now = datetime.now(timezone.utc)
        start = max(now,datetime.fromisoformat(old['expires_at'])) if old else now
        expires = (start + timedelta(days=days)).isoformat()
        con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at',(message.from_user.id,expires))
    await message.answer(f'⭐ Premium активирован до {expires}')


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


@dp.message(Command('support'))
async def support(message: Message):
    if not message.from_user:
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts)<2:
        return await message.answer('🆘 Напиши: /support Текст твоего обращения')
    with db() as con:
        con.execute('INSERT INTO support(user_id,body) VALUES(?,?)',(message.from_user.id,parts[1][:3000]))
    await message.answer('✅ Обращение сохранено.')
    await alert(message.bot,f'🆘 Обращение от {message.from_user.id}: {parts[1][:2500]}')


@dp.message(Command('broadcast'))
async def broadcast(message: Message, bot: Bot):
    if not message.from_user or message.from_user.id != ADMIN_ID or not ADMIN_ID:
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts)<2:
        return await message.answer('Используй: /broadcast текст рассылки')
    with db() as con:
        ids = [r['id'] for r in con.execute('SELECT id FROM users').fetchall()]
    success=0
    for uid in ids:
        try:
            await bot.send_message(uid,parts[1][:4096]); success+=1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f'Рассылка завершена: {success}/{len(ids)}')


@dp.message(Command('grant'))
async def grant(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID or not ADMIN_ID:
        return
    parts = (message.text or '').split()
    if len(parts)!=3:
        return await message.answer('Используй: /grant USER_ID DAYS')
    try:
        uid,days=int(parts[1]),int(parts[2])
        if days < 1 or days > 3650: raise ValueError
    except ValueError:
        return await message.answer('Неверный ID или срок')
    expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
    with db() as con:
        con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at',(uid,expires))
    await message.answer(f'⭐ Premium выдан пользователю {uid} до {expires}')


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
        text = (screen('PREMIUM', 'Особые возможности HadesSave') + f'\n\n⭐ <b>Подписка:</b> {escape(row["expires_at"] if row else "не активна")}\n\n💫 <b>Выбери подходящий тариф:</b>\nОплата через Telegram Stars 👇')
        markup = kb([('💎 7 дней · 50 ⭐', 'buy:7')], [('💎 14 дней · 100 ⭐', 'buy:14')], [('💎 30 дней · 300 ⭐', 'buy:30')], [('📜 Условия', 'menu:premium_terms'), ('🆘 Оплата', 'menu:payment_support')], [('🏠 Главное меню', 'menu:home')])
    elif section == 'premium_terms':
        text = (screen('УСЛОВИЯ PREMIUM', 'Прозрачные тарифы') + '\n\n💎 <b>7 дней</b> · 50 ⭐\n💎 <b>14 дней</b> · 100 ⭐\n💎 <b>30 дней</b> · 300 ⭐\n\n<i>Отдельные функции оригинального Premium пока недоступны.</i>')
        markup = kb([('💎 Тарифы', 'menu:premium')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('support','payment_support'):
        text = (screen('ПОДДЕРЖКА', 'Мы на связи') + '\n\n💬 <b>Нужна помощь?</b>\nОпиши проблему одной командой:\n\n<code>/support Твой вопрос</code>\n\n<i>Чем точнее описание, тем проще помочь.</i>')
        markup = kb([('💳 Вопрос об оплате', 'menu:payment_support')] if section=='support' else [('🆘 Общая поддержка', 'menu:support')], [('🏠 Главное меню', 'menu:home')])
    elif section in ('invite','ref'):
        me = await bot.get_me()
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id=?',(uid,)).fetchone()[0]
        text = (screen('ПРИГЛАШЕНИЯ', 'Вместе удобнее') + f'\n\n👥 <b>Приглашено друзей:</b> {count}\n\n🔗 <b>Твоя персональная ссылка:</b>\n<code>https://t.me/{escape(me.username or "")}?start=ref_{uid}</code>\n\n<i>Нажми на ссылку, чтобы скопировать её.</i>')
    elif section == 'profile':
        with db() as con:
            count = con.execute('SELECT COUNT(*) FROM connections WHERE user_id=? AND enabled=1',(uid,)).fetchone()[0]
        text = (screen('МОЙ ПРОФИЛЬ', 'Твой аккаунт HadesSave') + f'\n\n🆔 <b>Telegram ID:</b> <code>{uid}</code>\n🔌 <b>Подключений:</b> {count}\n\n<i>Управляй функциями через главное меню.</i>')
    elif section == 'archive':
        text = (screen('АРХИВ', 'Твоя история сообщений') + '\n\n📚 Чтобы открыть последние сохранённые записи, отправь команду:\n\n<code>/archive</code>')
    else:
        text = 'Раздел пока не перенесён.'
    await show_screen(callback, text, markup, section=section)


ADMIN_SECTIONS = {
    'home': '🏠 Главное меню', 'commands': '💬 Команды',
    'support': '💡 Поддержка', 'statistics': '📈 Статистика',
    'settings': '⚙️ Настройки', 'premium': '💎 Premium',
    'deleted': '🗑 Удалённые', 'edited': 'ℹ️ Изменённые',
    'setup': '🚀 Подключение', 'profile': '🤖 Мой HadesSave',
    'invite': '🎁 Пригласить друга',
}


def admin_allowed(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID


def admin_keyboard():
    buttons = [InlineKeyboardButton(text=label, callback_data='adm:section:' + section)
               for section, label in ADMIN_SECTIONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0, len(buttons), 2)])


def admin_section_keyboard(section):
    return kb([('🖼 Загрузить фото', 'adm:photo:'+section)],
              [('✏️ Изменить текст', 'adm:text:'+section)],
              [('👁 Предпросмотр', 'adm:preview:'+section)],
              [('🗑 Удалить фото', 'adm:remove_photo:'+section)],
              [('🧹 Сбросить текст', 'adm:remove_text:'+section)],
              [('↩ Назад', 'adm:home')])


@dp.message(Command('admin'))
async def admin_panel(message: Message):
    if not message.from_user or not admin_allowed(message.from_user.id):
        return
    await message.answer('👑 <b>HadesSave — Админ-панель</b>\n\nВыбери раздел для оформления:',
                         parse_mode='HTML', reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith('adm:'))
async def admin_callbacks(call: CallbackQuery):
    if not admin_allowed(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    parts = call.data.split(':')
    action = parts[1] if len(parts)>1 else 'home'
    section = parts[2] if len(parts)>2 else None
    if action == 'home':
        await call.answer()
        return await show_screen(call, '👑 <b>HadesSave — Админ-панель</b>\n\nВыбери раздел для оформления:', admin_keyboard())
    if section not in ADMIN_SECTIONS:
        return await call.answer('Раздел не найден', show_alert=True)
    if action in ('photo','text'):
        with db() as con:
            con.execute('INSERT INTO admin_edit_state(user_id,section,mode) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET section=excluded.section,mode=excluded.mode',
                        (call.from_user.id,section,action))
        await call.answer()
        return await show_screen(call, f'🎨 <b>{ADMIN_SECTIONS[section]}</b>\n\n'+
                                 ('Отправь мне фотографию следующим сообщением.' if action == 'photo' else
                                  'Отправь новый текст следующим сообщением (можно HTML-разметку).'),
                                 kb([('↩ Отмена', 'adm:section:'+section)]))
    if action in ('remove_photo','remove_text'):
        with db() as con:
            con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)', (section,))
            col = 'photo_id' if action == 'remove_photo' else 'custom_text'
            con.execute(f'UPDATE ui_design SET {col}=NULL WHERE section=?', (section,))
        await call.answer('Сохранено')
    else:
        await call.answer()
    with db() as con:
        con.execute('DELETE FROM admin_edit_state WHERE user_id=?',(call.from_user.id,))
    design=get_design(section)
    if action == 'preview':
        # Shows the actual installed image; regular navigation uses this same design.
        txt=design['custom_text'] or f'{ADMIN_SECTIONS[section]}\n\nТекст раздела по умолчанию.'
        if design['photo_id']:
            await call.message.answer_photo(design['photo_id'], caption=txt[:1024], parse_mode='HTML')
        else:
            await call.message.answer(txt[:4096], parse_mode='HTML')
    await show_screen(call,
        f'🎨 <b>{ADMIN_SECTIONS[section]}</b>\n\n🖼 Фото: '+('установлено' if design['photo_id'] else 'не установлено')+
        '\n✏️ Текст: '+('свой' if design['custom_text'] is not None else 'стандартный'),
        admin_section_keyboard(section))


@dp.message(F.chat.type == 'private', F.photo)
async def admin_photo_upload(message: Message):
    if not message.from_user or not admin_allowed(message.from_user.id):
        return
    with db() as con:
        row = con.execute('SELECT section,mode FROM admin_edit_state WHERE user_id=?',(message.from_user.id,)).fetchone()
        if not row or row['mode'] != 'photo':
            return
        section=row['section']
        con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)',(section,))
        con.execute('UPDATE ui_design SET photo_id=? WHERE section=?',(message.photo[-1].file_id, section))
        con.execute('DELETE FROM admin_edit_state WHERE user_id=?',(message.from_user.id,))
    await message.answer(f'✅ Фото для раздела «{ADMIN_SECTIONS[section]}» сохранено.',
                         reply_markup=admin_section_keyboard(section))


@dp.message(F.chat.type == 'private', F.text)
async def admin_text_upload(message: Message):
    if not message.from_user or not admin_allowed(message.from_user.id):
        return
    with db() as con:
        row = con.execute('SELECT section,mode FROM admin_edit_state WHERE user_id=?',(message.from_user.id,)).fetchone()
        if not row or row['mode'] != 'text':
            return
        section=row['section']
        con.execute('INSERT OR IGNORE INTO ui_design(section) VALUES(?)',(section,))
        # Escape raw text: no untrusted/broken HTML in captions.
        con.execute('UPDATE ui_design SET custom_text=? WHERE section=?',(escape(message.text[:4000]),section))
        con.execute('DELETE FROM admin_edit_state WHERE user_id=?',(message.from_user.id,))
    await message.answer(f'✅ Текст для раздела «{ADMIN_SECTIONS[section]}» сохранён.',
                         reply_markup=admin_section_keyboard(section))



async def main():
    if not TOKEN:
        raise SystemExit('Укажи BOT_TOKEN в переменных окружения')
    with db():
        pass
    logger.info('Starting HadesSave build %s; media_dir=%s; unmarked_reply_capture=%s',
                BUILD_ID, MEDIA_DIR, CAPTURE_UNMARKED_REPLIED_MEDIA)
    bot = Bot(TOKEN)
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
