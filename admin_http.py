"""Direct HTTPS API for HadesSave Mini App (Bothost reverse proxy)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiogram.exceptions import TelegramForbiddenError

log = logging.getLogger('hades.admin_http')
ORIGIN = os.getenv('MINIAPP_ORIGIN', 'https://hadessavemod-art.github.io').rstrip('/')
MINIAPP_ORIGIN = ORIGIN
PORT = int(os.getenv('PORT', '3000'))
TTL = 8 * 3600
RESULTS: dict[str, dict] = {}
LOGIN_ATTEMPTS: dict[str, deque] = defaultdict(deque)

# Set both values in Bothost environment. Do not hardcode production credentials.
PASSWORD = os.getenv('ADMIN_PASSWORD', '')
SECRET = os.getenv('ADMIN_SESSION_SECRET', '')

try:
    from admin_bridge import snapshot, execute_task, prepare
except ImportError:
    def snapshot():
        return {}
    async def execute_task(*args, **kwargs):
        return 'OK'
    def prepare():
        return None


def sign_token() -> str:
    if not SECRET or len(SECRET) < 32:
        raise RuntimeError('ADMIN_SESSION_SECRET не задан или короче 32 символов')
    payload = json.dumps({'exp': int(time.time()) + TTL, 'nonce': secrets.token_hex(16)}, separators=(',', ':')).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b'=').decode()
    sig = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + sig


def valid_token(token: str) -> bool:
    try:
        if not SECRET or len(SECRET) < 32:
            return False
        encoded, signature = token.rsplit('.', 1)
        expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        return time.time() < int(payload.get('exp', 0))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


@web.middleware
async def secure(request: web.Request, handler):
    origin = request.headers.get('Origin')
    if origin and origin != ORIGIN:
        return web.json_response({'error': 'Этот сайт не разрешён'}, status=403)
    if request.method == 'OPTIONS':
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except Exception:
            log.exception('Admin HTTP error')
            response = web.json_response({'error': 'Внутренняя ошибка сервера'}, status=500)
    if origin == ORIGIN:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Telegram-Init-Data'
        response.headers['Vary'] = 'Origin'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def authenticate(request: web.Request) -> None:
    raw = request.headers.get('Authorization', '')
    if not raw.startswith('Bearer ') or not valid_token(raw[7:]):
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Войдите заново'}), content_type='application/json')


async def login(request: web.Request):
    if not PASSWORD or not SECRET or len(SECRET) < 32:
        return web.json_response({'error': 'Админ-вход не настроен. Задайте ADMIN_PASSWORD и ADMIN_SESSION_SECRET.'}, status=503)
    address = request.remote or 'unknown'
    now = time.monotonic()
    entries = LOGIN_ATTEMPTS[address]
    while entries and now - entries[0] > 900:
        entries.popleft()
    if len(entries) >= 8:
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Некорректный JSON'}, status=400)
    guess = str(data.get('password', ''))
    if not hmac.compare_digest(guess.encode(), PASSWORD.encode()):
        entries.append(now)
        await asyncio.sleep(0.3)
        return web.json_response({'error': 'Неверный пароль'}, status=401)
    entries.clear()
    return web.json_response({'token': sign_token()})


async def state(request: web.Request):
    authenticate(request)
    return web.json_response({'state': snapshot()})


async def command(request: web.Request):
    authenticate(request)
    data = await request.json()
    action = str(data.get('action', ''))
    allowed = {'premium_grant', 'premium_revoke', 'block', 'unblock', 'broadcast', 'support_reply', 'config_set'}
    if action not in allowed or not isinstance(data.get('data', {}), dict):
        return web.json_response({'error': 'Неизвестная команда'}, status=400)
    job_id = secrets.token_hex(16)
    RESULTS[job_id] = {'result': None, 'created': time.monotonic()}
    bot = request.app['telegram_bot']

    async def work():
        try:
            value = await execute_task({'action': action, 'data': data.get('data') or {}}, bot)
            result_data = {'ok': True, 'message': str(value)}
        except Exception as exc:
            log.exception('Admin command %s failed', action)
            result_data = {'ok': False, 'message': str(exc)[:350]}
        if job_id in RESULTS:
            RESULTS[job_id]['result'] = result_data

    asyncio.create_task(work())
    for key, item in list(RESULTS.items()):
        if time.monotonic() - item['created'] > 3600:
            RESULTS.pop(key, None)
    return web.json_response({'id': job_id})


async def result(request: web.Request):
    authenticate(request)
    job_id = request.query.get('id', '')
    return web.json_response({'result': RESULTS.get(job_id, {}).get('result')})


def archive_db():
    import __main__
    return __main__.db()


def media_root() -> Path:
    import __main__
    root = Path(__main__.MEDIA_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


async def get_miniapp_user_id(request: web.Request):
    raw = request.headers.get('X-Telegram-Init-Data', '') or request.query.get('initData', '')
    if not raw:
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Нет данных авторизации'}), content_type='application/json')
    import __main__
    bot_token = getattr(__main__, 'TOKEN', None) or os.getenv('BOT_TOKEN')
    if not bot_token:
        raise web.HTTPInternalServerError(text=json.dumps({'error': 'Ошибка конфигурации сервера'}), content_type='application/json')
    try:
        identity = safe_parse_webapp_init_data(bot_token, raw)
        if not getattr(identity, 'user', None):
            raise ValueError('В initData нет пользователя')
        return identity
    except Exception:
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Неверная подпись Telegram'}), content_type='application/json')


async def get_admin_user_id(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    with archive_db() as con:
        banned = con.execute('SELECT reason FROM bans WHERE user_id=?', (user_id,)).fetchone()
        if banned:
            raise web.HTTPForbidden(text=json.dumps({'error': f'Вы забанены: {banned["reason"]}'}), content_type='application/json')
        import __main__
        try:
            admin_id_env = int(getattr(__main__, 'ADMIN_ID', 0) or 0)
        except (ValueError, TypeError):
            admin_id_env = 0
        if admin_id_env and user_id == admin_id_env:
            con.execute('INSERT OR IGNORE INTO admins (user_id, role) VALUES (?, ?)', (user_id, 'owner'))
            con.commit()
            return user_id, 'owner'
        admin = con.execute('SELECT role FROM admins WHERE user_id=?', (user_id,)).fetchone()
        if not admin:
            raise web.HTTPForbidden(text=json.dumps({'error': 'Доступ к админ-панели запрещён'}), content_type='application/json')
        return user_id, admin['role']


def parse_int(value, default=0, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def message_media_types():
    return ('photo', 'video', 'voice', 'video_note', 'animation', 'document', 'audio', 'sticker')


# ------------------------- MINI APP USER API ------------------------------
async def miniapp_stats(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    with archive_db() as con:
        banned = con.execute('SELECT reason FROM bans WHERE user_id=?', (user_id,)).fetchone()
        if banned:
            raise web.HTTPForbidden(text=json.dumps({'error': f'Вы забанены: {banned["reason"]}'}), content_type='application/json')
        total = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.deleted=1', (user_id,)).fetchone()[0]
        chats_count = con.execute('SELECT COUNT(DISTINCT m.connection_id || ":" || m.chat_id) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        media_count = con.execute('SELECT COUNT(*) FROM media_files f JOIN connections c ON c.id=f.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        is_premium = False
        premium_expires_at = None
        prem_row = con.execute('SELECT expires_at FROM premium WHERE user_id=?', (user_id,)).fetchone()
        if prem_row:
            try:
                expires = datetime.fromisoformat(prem_row['expires_at'])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires > datetime.now(timezone.utc):
                    is_premium = True
                    premium_expires_at = prem_row['expires_at']
            except Exception:
                pass
        import __main__
        try:
            admin_id_env = int(getattr(__main__, 'ADMIN_ID', 0) or 0)
        except (ValueError, TypeError):
            admin_id_env = 0
        role = None
        if admin_id_env and user_id == admin_id_env:
            con.execute('INSERT OR IGNORE INTO admins (user_id, role) VALUES (?, ?)', (user_id, 'owner'))
            con.commit()
            role = 'owner'
        else:
            admin = con.execute('SELECT role FROM admins WHERE user_id=?', (user_id,)).fetchone()
            role = admin['role'] if admin else None
    name = identity.user.first_name or 'Друг'
    return web.json_response({
        'total_messages': total, 'deleted_messages': deleted, 'chats_count': chats_count,
        'media_count': media_count, 'user': name, 'user_id': user_id,
        'username': identity.user.username or '', 'is_premium': is_premium,
        'premium_expires_at': premium_expires_at, 'role': role
    })


async def miniapp_get_settings(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    with archive_db() as con:
        row = con.execute('SELECT edits, deletions, deleted_media FROM notification_prefs WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            con.execute('INSERT OR IGNORE INTO notification_prefs(user_id) VALUES(?)', (user_id,))
            con.commit()
            row = con.execute('SELECT edits, deletions, deleted_media FROM notification_prefs WHERE user_id=?', (user_id,)).fetchone()
    return web.json_response({'edits': bool(row['edits']), 'deletions': bool(row['deletions']), 'deleted_media': bool(row['deleted_media'])})


async def miniapp_update_settings(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    data = await request.json()
    with archive_db() as con:
        con.execute('INSERT OR IGNORE INTO notification_prefs(user_id) VALUES(?)', (user_id,))
        for key in ('edits', 'deletions', 'deleted_media'):
            if key in data:
                con.execute(f'UPDATE notification_prefs SET {key}=? WHERE user_id=?', (int(bool(data[key])), user_id))
        con.commit()
    return web.json_response({'success': True})


async def miniapp_clear_archive(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    files = []
    with archive_db() as con:
        files = [r['local_path'] for r in con.execute('''SELECT f.local_path FROM media_files f JOIN connections c ON c.id=f.connection_id WHERE c.user_id=? AND f.local_path IS NOT NULL''', (user_id,))]
        con.execute('DELETE FROM message_versions WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (user_id,))
        con.execute('DELETE FROM media_files WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (user_id,))
        con.execute('DELETE FROM messages WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (user_id,))
        con.commit()
    root = media_root()
    for raw_path in files:
        try:
            path = Path(raw_path).resolve()
            if path.is_relative_to(root) and path.is_file():
                path.unlink()
        except Exception:
            log.warning('Could not remove archive file %s', raw_path, exc_info=True)
    return web.json_response({'success': True, 'message': 'Архив полностью очищен'})


async def miniapp_chats(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    filter_type = request.query.get('filter', 'all')
    search = str(request.query.get('q', '')).strip()[:150]
    media_values = message_media_types()
    media_sql = ','.join('?' for _ in media_values)
    sql = f'''
      SELECT m.connection_id, m.chat_id, c.user_id AS owner_id,
             COALESCE(MAX(NULLIF(m.chat_title,'')),MAX(NULLIF(m.chat_username,'')),
                      MAX(CASE WHEN m.sender_id!=c.user_id THEN NULLIF(m.sender_name,'') END),
                      MAX(CASE WHEN m.sender_id!=c.user_id THEN NULLIF(m.sender_username,'') END),
                      CAST(m.chat_id AS TEXT)) AS label,
             COALESCE(MAX(NULLIF(m.chat_username,'')),
                      MAX(CASE WHEN m.sender_id!=c.user_id THEN NULLIF(m.sender_username,'') END),'') AS chat_username,
             COUNT(*) AS total,
             SUM(CASE WHEN m.deleted=1 THEN 1 ELSE 0 END) AS deleted_count,
             SUM(CASE WHEN m.content_type IN ({media_sql}) THEN 1 ELSE 0 END) AS media_count,
             SUM(CASE WHEN m.edited=1 THEN 1 ELSE 0 END) AS edited_count,
             MAX(COALESCE(m.message_date,m.updated_at)) AS last_at,
             (SELECT COALESCE(m2.text,'') FROM messages m2
                WHERE m2.connection_id=m.connection_id AND m2.chat_id=m.chat_id
                ORDER BY m2.message_id DESC LIMIT 1) AS last_message,
             (SELECT m2.sender_id FROM messages m2
                WHERE m2.connection_id=m.connection_id AND m2.chat_id=m.chat_id
                ORDER BY m2.message_id DESC LIMIT 1) AS last_sender_id,
             (SELECT m2.content_type FROM messages m2
                WHERE m2.connection_id=m.connection_id AND m2.chat_id=m.chat_id
                ORDER BY m2.message_id DESC LIMIT 1) AS last_content_type,
             SUM(CASE WHEN m.sender_id=c.user_id THEN 1 ELSE 0 END) AS outgoing_count
      FROM messages m JOIN connections c ON c.id=m.connection_id
      WHERE c.user_id=?
      GROUP BY m.connection_id,m.chat_id,c.user_id
      HAVING COUNT(*)>0
    '''
    params = list(media_values) + [user_id]
    if search:
        sql += ''' AND SUM(CASE WHEN instr(unicode_casefold(COALESCE(m.text,'')),unicode_casefold(?))>0 THEN 1 ELSE 0 END)>0'''
        params.append(search)
    sql += ' ORDER BY last_at DESC LIMIT 200'
    with archive_db() as con:
        con.create_function('unicode_casefold', 1, lambda value: str(value or '').casefold())
        rows = [dict(r) for r in con.execute(sql, params)]
    for row in rows:
        if row.get('last_sender_id') is not None and int(row['last_sender_id']) == user_id:
            row['last_message'] = 'Вы: ' + str(row.get('last_message') or '')
        if search:
            row['match_query'] = search
    if filter_type == 'deleted':
        rows = [r for r in rows if r['deleted_count']]
    elif filter_type == 'media':
        rows = [r for r in rows if r['media_count']]
    return web.json_response({'chats': rows})


async def miniapp_messages(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    chat_id = parse_int(request.query.get('chat_id'), 0)
    connection_id = str(request.query.get('connection_id', '')).strip()
    if not chat_id or not connection_id:
        return web.json_response({'error': 'Не указан chat_id или connection_id'}, status=400)
    filter_type = request.query.get('filter', 'all')
    search = str(request.query.get('q', '')).strip()[:150]
    offset = parse_int(request.query.get('offset'), 0, 0, 1000000)
    limit = parse_int(request.query.get('limit'), 50, 1, 100)
    around_id = parse_int(request.query.get('around_message_id'), 0, 0)
    if around_id and not search and filter_type == 'all' and offset == 0:
        with archive_db() as con:
            newer_count = con.execute('''SELECT COUNT(*) FROM messages
                WHERE connection_id=? AND chat_id=? AND message_id>?''',
                (connection_id, chat_id, around_id)).fetchone()[0]
        offset = max(0, int(newer_count) - limit // 2)
    media_values = message_media_types()
    media_sql = ','.join('?' for _ in media_values)
    sql = f'''
      SELECT m.connection_id,m.message_id,m.chat_id,m.sender_id,m.sender_name,m.sender_username,
             m.text,m.content_type,m.edited,m.deleted,m.updated_at,
             COALESCE(m.message_date,m.updated_at) AS sent_at,
             m.reply_to_message_id,m.reply_to_sender_name,m.reply_to_text,
             f.local_path,f.file_id,
             CASE WHEN (f.local_path IS NOT NULL AND f.local_path!='') OR (f.file_id IS NOT NULL AND f.file_id!='') THEN 1 ELSE 0 END AS has_media,
             CASE WHEN m.sender_id=c.user_id THEN 'outgoing' ELSE 'incoming' END AS direction,
             CASE WHEN m.sender_id=c.user_id THEN 1 ELSE 0 END AS is_outgoing
      FROM messages m JOIN connections c ON c.id=m.connection_id
      LEFT JOIN media_files f ON f.connection_id=m.connection_id AND f.chat_id=m.chat_id AND f.message_id=m.message_id
      WHERE c.user_id=? AND m.connection_id=? AND m.chat_id=?
    '''
    params = [user_id, connection_id, chat_id]
    if filter_type == 'deleted':
        sql += ' AND m.deleted=1'
    elif filter_type == 'media':
        sql += f' AND m.content_type IN ({media_sql})'
        params.extend(media_values)
    if search:
        sql += ''' AND (instr(unicode_casefold(COALESCE(m.text,'')),unicode_casefold(?))>0
                        OR instr(unicode_casefold(COALESCE(m.sender_name,'')),unicode_casefold(?))>0)'''
        params.extend([search, search])
    sql += ' ORDER BY m.message_id DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])
    with archive_db() as con:
        con.create_function('unicode_casefold', 1, lambda value: str(value or '').casefold())
        rows = [dict(r) for r in con.execute(sql, params)]
        for row in rows:
            row['media_ext'] = Path(row['local_path']).suffix.lower() if row.get('local_path') else ''
            row.pop('local_path', None)
            row.pop('file_id', None)
    next_offset = offset + len(rows) if len(rows) == limit else None
    return web.json_response({'messages': rows, 'next_offset': next_offset})


async def miniapp_message_versions(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    chat_id = parse_int(request.query.get('chat_id'), 0)
    message_id = parse_int(request.query.get('message_id'), 0)
    connection_id = str(request.query.get('connection_id', '')).strip()
    if not chat_id or not message_id or not connection_id:
        return web.json_response({'error': 'Не указаны параметры сообщения'}, status=400)
    with archive_db() as con:
        owned = con.execute('''SELECT 1 FROM messages m JOIN connections c ON c.id=m.connection_id
            WHERE c.user_id=? AND m.connection_id=? AND m.chat_id=? AND m.message_id=? LIMIT 1''',
            (user_id, connection_id, chat_id, message_id)).fetchone()
        if not owned:
            raise web.HTTPNotFound(text='Сообщение не найдено')
        rows = [dict(r) for r in con.execute('''SELECT old_text,new_text,changed_at FROM message_versions
            WHERE connection_id=? AND chat_id=? AND message_id=? ORDER BY id ASC LIMIT 100''',
            (connection_id, chat_id, message_id))]
    return web.json_response({'versions': rows})


async def miniapp_avatar(request: web.Request):
    """Return the Telegram profile/chat photo for an archived conversation."""
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    chat_id = parse_int(request.query.get('chat_id'), 0)
    connection_id = str(request.query.get('connection_id', '')).strip()
    if not chat_id or not connection_id:
        raise web.HTTPBadRequest(text='Не указан chat_id или connection_id')

    with archive_db() as con:
        owned = con.execute('''SELECT m.raw_json FROM connections c
            JOIN messages m ON m.connection_id=c.id
            WHERE c.user_id=? AND c.id=? AND m.chat_id=?
            ORDER BY m.message_id DESC LIMIT 1''',
            (user_id, connection_id, chat_id)).fetchone()
    if not owned:
        raise web.HTTPNotFound(text='Чат не найден')

    root = media_root().parent / 'avatars'
    root.mkdir(parents=True, exist_ok=True)
    import hashlib
    key = hashlib.sha256(f'{user_id}:{connection_id}:{chat_id}'.encode()).hexdigest()
    target = root / f'{key}.jpg'
    if target.is_file() and target.stat().st_size > 0:
        return web.FileResponse(target, headers={'Cache-Control': 'public, max-age=3600'})

    bot = request.app['telegram_bot']
    file_id = None
    # Message.chat may already include the ChatPhoto file ids. Use that first
    # so we can still show an avatar when getChat is unavailable to the bot.
    try:
        saved = json.loads(owned['raw_json'] or '{}')
        chat_data = saved.get('chat') or {}
        chat_photo = chat_data.get('photo') or {}
        file_id = chat_photo.get('big_file_id') or chat_photo.get('small_file_id')
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        chat = await bot.get_chat(chat_id)
        photo = getattr(chat, 'photo', None)
        if photo:
            file_id = getattr(photo, 'big_file_id', None) or getattr(photo, 'small_file_id', None)
    except Exception as exc:
        log.debug('get_chat photo lookup failed for %s: %s', chat_id, exc)

    if not file_id and chat_id > 0:
        try:
            photos = await bot.get_user_profile_photos(chat_id, limit=1)
            if photos.total_count and photos.photos:
                file_id = photos.photos[0][-1].file_id
        except Exception as exc:
            log.debug('get_user_profile_photos failed for %s: %s', chat_id, exc)

    if not file_id:
        raise web.HTTPNotFound(text='У собеседника нет доступного фото профиля')
    temp = target.with_suffix('.part')
    try:
        await bot.download(file_id, destination=temp)
        if not temp.is_file() or not temp.stat().st_size:
            raise IOError('Telegram вернул пустой файл аватара')
        temp.replace(target)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        log.warning('Avatar download failed for chat %s: %s', chat_id, exc)
        raise web.HTTPNotFound(text='Не удалось получить фото профиля')
    return web.FileResponse(target, headers={'Cache-Control': 'public, max-age=3600'})


async def miniapp_media(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    chat_id = parse_int(request.query.get('chat_id'), 0)
    mid = parse_int(request.query.get('message_id'), 0)
    connection_id = str(request.query.get('connection_id', '')).strip()
    if not chat_id or not mid or not connection_id:
        raise web.HTTPBadRequest(text='Не указан chat_id, connection_id или message_id')
    with archive_db() as con:
        row = con.execute('''SELECT f.connection_id,f.local_path,f.file_id,m.content_type
            FROM media_files f JOIN connections c ON c.id=f.connection_id
            JOIN messages m ON m.connection_id=f.connection_id AND m.chat_id=f.chat_id AND m.message_id=f.message_id
            WHERE c.user_id=? AND f.connection_id=? AND f.chat_id=? AND f.message_id=? LIMIT 1''',
            (user_id, connection_id, chat_id, mid)).fetchone()
    if not row:
        raise web.HTTPNotFound(text='Медиа не найдено в базе')
    root = media_root()
    path = Path(row['local_path']).resolve() if row['local_path'] else None
    if not path or not path.is_file() or not path.is_relative_to(root):
        if not row['file_id']:
            raise web.HTTPNotFound(text='Файл недоступен')
        suffix = {'photo':'.jpg','voice':'.ogg','audio':'.mp3','video':'.mp4','video_note':'.mp4','animation':'.gif','sticker':'.webp'}.get(row['content_type'], '.bin')
        target = root / str(row['connection_id']) / str(chat_id) / f'{mid}{suffix}'
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await request.app['telegram_bot'].download(row['file_id'], destination=target)
            if not target.is_file() or not target.stat().st_size:
                raise IOError('Пустой файл')
        except Exception as exc:
            target.unlink(missing_ok=True)
            log.warning('Media download failed: %s', exc)
            raise web.HTTPNotFound(text='Ошибка скачивания файла')
        with archive_db() as con:
            con.execute('UPDATE media_files SET local_path=? WHERE connection_id=? AND chat_id=? AND message_id=?',
                        (str(target), row['connection_id'], chat_id, mid))
            con.commit()
        path = target.resolve()
    response = web.FileResponse(path)
    response.content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    disposition = 'attachment' if request.query.get('download') == '1' else 'inline'
    response.headers['Content-Disposition'] = disposition + '; filename="' + quote(path.name, safe='') + '"'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


async def miniapp_request_media(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    data = await request.json()
    chat_id = parse_int(data.get('chat_id'), 0)
    mid = parse_int(data.get('message_id'), 0)
    connection_id = str(data.get('connection_id', '')).strip()
    if not chat_id or not mid or not connection_id:
        return web.json_response({'error': 'Неверные параметры запроса'}, status=400)
    with archive_db() as con:
        row = con.execute('''SELECT f.local_path,f.file_id,m.content_type,m.text FROM media_files f
            JOIN connections c ON c.id=f.connection_id
            JOIN messages m ON m.connection_id=f.connection_id AND m.chat_id=f.chat_id AND m.message_id=f.message_id
            WHERE c.user_id=? AND f.connection_id=? AND f.chat_id=? AND f.message_id=? LIMIT 1''',
            (user_id, connection_id, chat_id, mid)).fetchone()
    if not row:
        return web.json_response({'error': 'Медиа не найдено в базе данных'}, status=404)
    import __main__
    from aiogram.types import FSInputFile
    root = media_root()
    path = Path(row['local_path']).resolve() if row['local_path'] else None
    if path and path.is_file() and path.is_relative_to(root):
        source = FSInputFile(path)
    elif row['file_id']:
        source = row['file_id']
    else:
        return web.json_response({'error': 'Файл физически отсутствует на сервере'}, status=404)
    bot = request.app['telegram_bot']
    caption = row['text'] or ''
    ct = row['content_type']
    try:
        if ct == 'photo': await bot.send_photo(user_id, source, caption=caption[:1024])
        elif ct == 'video': await bot.send_video(user_id, source, caption=caption[:1024])
        elif ct == 'video_note':
            await bot.send_video_note(user_id, source)
            if caption: await bot.send_message(user_id, caption)
        elif ct == 'voice': await bot.send_voice(user_id, source, caption=caption[:1024])
        elif ct == 'animation': await bot.send_animation(user_id, source, caption=caption[:1024])
        elif ct == 'audio': await bot.send_audio(user_id, source, caption=caption[:1024])
        else: await bot.send_document(user_id, source, caption=caption[:1024])
        return web.json_response({'success': True, 'message': 'Файл отправлен в бота! Проверь личку.'})
    except Exception as exc:
        log.warning('Cannot send requested media as %s: %s', ct, exc)
        try:
            await bot.send_document(user_id, source, caption=('Не удалось отправить в исходном формате.\n\n' + caption)[:1024])
            return web.json_response({'success': True, 'message': 'Файл отправлен как документ!'})
        except Exception as fallback_exc:
            log.exception('Fallback document send failed')
            return web.json_response({'error': f'Ошибка Telegram: {str(fallback_exc)[:100]}'}, status=500)


async def miniapp_referrals(request: web.Request):
    identity = await get_miniapp_user_id(request)
    user_id = int(identity.user.id)
    import __main__
    bot_username = getattr(__main__, 'BOT_USERNAME', 'HadesSave_bot')
    with archive_db() as con:
        count = con.execute('''SELECT COUNT(*) FROM referrals r JOIN connections c ON c.user_id=r.referred_id
            WHERE r.referrer_id=? AND c.enabled=1''', (user_id,)).fetchone()[0]
    return web.json_response({'link': f'https://t.me/{bot_username}?start=ref_{user_id}', 'count': count})


# ----------------------------- ADMIN API ---------------------------------
async def admin_stats(request: web.Request):
    await get_admin_user_id(request)
    with archive_db() as con:
        users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        conns = con.execute('SELECT COUNT(*) FROM connections WHERE enabled=1').fetchone()[0]
        msgs = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0]
        prem = con.execute('SELECT COUNT(*) FROM premium WHERE expires_at>?', (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
        media = con.execute('SELECT COUNT(*) FROM media_files').fetchone()[0]
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        week_prem = con.execute('SELECT COUNT(*) FROM payments WHERE created_at>?', (week_ago,)).fetchone()[0]
        week_stars = con.execute('SELECT SUM(stars) FROM payments WHERE created_at>?', (week_ago,)).fetchone()[0] or 0
        total_stars = con.execute('SELECT SUM(stars) FROM payments').fetchone()[0] or 0
    return web.json_response({'users':users,'connections':conns,'messages':msgs,'deleted':deleted,'premium':prem,'media':media,'week_premium':week_prem,'week_stars':week_stars,'total_stars':total_stars})


async def admin_grant(request: web.Request):
    await get_admin_user_id(request)
    data = await request.json()
    target = str(data.get('target','')).strip()
    days = parse_int(data.get('days'), 0, 0, 3650)
    if days < 1:
        return web.json_response({'error':'Неверное количество дней (1–3650)'}, status=400)
    with archive_db() as con:
        target_id = None
        if target.startswith('@'):
            row = con.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(target[1:],)).fetchone()
            if row: target_id = row['id']
        else:
            target_id = parse_int(target, 0, 1)
        if not target_id or not con.execute('SELECT 1 FROM users WHERE id=?',(target_id,)).fetchone():
            return web.json_response({'error':'Пользователь не найден'}, status=404)
        expires = (datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
        con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at',(target_id,expires))
        con.commit()
    try: await request.app['telegram_bot'].send_message(target_id,f'👑 <b>PREMIUM АКТИВИРОВАН</b>\nПодписка выдана на {days} дней.',parse_mode='HTML')
    except Exception: log.info('Could not notify user %s about Premium', target_id)
    return web.json_response({'success':True,'message':f'Premium выдан пользователю {target_id} на {days} дней'})


async def admin_ban(request: web.Request):
    await get_admin_user_id(request)
    data = await request.json()
    target = str(data.get('target','')).strip()
    reason = str(data.get('reason','Без причины')).strip()[:500] or 'Без причины'
    with archive_db() as con:
        if target.startswith('@'):
            row = con.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(target[1:],)).fetchone()
            target_id = row['id'] if row else None
        else:
            target_id = parse_int(target, 0, 1)
        if not target_id: return web.json_response({'error':'Пользователь не найден'}, status=404)
        if con.execute('SELECT 1 FROM admins WHERE user_id=?',(target_id,)).fetchone():
            return web.json_response({'error':'Нельзя забанить администратора'}, status=403)
        con.execute('INSERT INTO bans(user_id,reason) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason',(target_id,reason))
        con.commit()
    try: await request.app['telegram_bot'].send_message(target_id,f'⛔ <b>ДОСТУП ЗАБЛОКИРОВАН</b>\nПричина: {reason}',parse_mode='HTML')
    except Exception: pass
    return web.json_response({'success':True,'message':f'Пользователь {target_id} забанен'})


async def admin_roles(request: web.Request):
    actor_id, actor_role = await get_admin_user_id(request)
    if request.method == 'GET':
        with archive_db() as con:
            rows = [dict(r) for r in con.execute('SELECT user_id,role FROM admins ORDER BY user_id')]
        return web.json_response({'admins':rows})
    data = await request.json()
    target = str(data.get('target','')).strip()
    role = str(data.get('role','')).strip().lower()
    if role not in ('owner','admin','aboba','none'):
        return web.json_response({'error':'Неверная роль'}, status=400)
    if actor_role != 'owner':
        return web.json_response({'error':'Изменять роли может только владелец'}, status=403)
    with archive_db() as con:
        if target.startswith('@'):
            row = con.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(target[1:],)).fetchone()
            target_id = row['id'] if row else None
        else: target_id = parse_int(target,0,1)
        if not target_id: return web.json_response({'error':'Пользователь не найден'}, status=404)
        if target_id == actor_id and role != 'owner':
            return web.json_response({'error':'Нельзя снять собственную роль владельца'}, status=403)
        if role == 'none': con.execute('DELETE FROM admins WHERE user_id=?',(target_id,))
        else: con.execute('INSERT INTO admins(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role',(target_id,role))
        con.commit()
    try:
        msg = '🛡 <b>АДМИН-ДОСТУП ОТОЗВАН</b>' if role == 'none' else f'👑 <b>АДМИН-ДОСТУП ВЫДАН</b>\nРоль: <b>{role}</b>'
        await request.app['telegram_bot'].send_message(target_id,msg,parse_mode='HTML')
    except Exception: pass
    return web.json_response({'success':True,'message':f'Роль пользователя {target_id} обновлена: {role}'})


async def admin_broadcast(request: web.Request):
    await get_admin_user_id(request)
    data = await request.json()
    message = str(data.get('text','')).strip()[:4000]
    if not message: return web.json_response({'error':'Пустой текст'}, status=400)
    with archive_db() as con:
        ids = [r[0] for r in con.execute('SELECT id FROM users').fetchall()]
    success = blocked = 0
    for uid in ids:
        try: await request.app['telegram_bot'].send_message(uid,message,parse_mode='HTML'); success += 1
        except TelegramForbiddenError: blocked += 1
        except Exception: pass
        await asyncio.sleep(.05)
    return web.json_response({'success':True,'message':f'Доставлено: {success}, заблокировали бота: {blocked}'})


async def admin_bans(request: web.Request):
    await get_admin_user_id(request)
    with archive_db() as con:
        rows = [dict(r) for r in con.execute('''SELECT b.user_id,b.reason,b.banned_at,u.first_name,u.username
            FROM bans b LEFT JOIN users u ON u.id=b.user_id ORDER BY b.banned_at DESC''')]
    return web.json_response({'bans':rows})


async def admin_remove_ban(request: web.Request):
    await get_admin_user_id(request)
    data = await request.json()
    target = str(data.get('target','')).strip()
    if target.startswith('@'):
        with archive_db() as con:
            row = con.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE',(target[1:],)).fetchone()
            target_id = row['id'] if row else None
    else: target_id = parse_int(target,0,1)
    if not target_id: return web.json_response({'error':'Пользователь не найден'}, status=404)
    with archive_db() as con:
        con.execute('DELETE FROM bans WHERE user_id=?',(target_id,)); con.commit()
    return web.json_response({'success':True,'message':f'Пользователь {target_id} разбанен'})


async def health(request: web.Request):
    return web.json_response({'status':'ok','service':'HadesSave API'})


async def start_server(bot):
    prepare()
    app = web.Application(middlewares=[secure], client_max_size=64*1024)
    app['telegram_bot'] = bot
    app.router.add_get('/health', health)
    app.router.add_post('/admin/login', login)
    app.router.add_get('/admin/state', state)
    app.router.add_post('/admin/command', command)
    app.router.add_get('/admin/result', result)
    app.router.add_get('/api/miniapp/stats', miniapp_stats)
    app.router.add_get('/api/miniapp/settings', miniapp_get_settings)
    app.router.add_post('/api/miniapp/settings', miniapp_update_settings)
    app.router.add_post('/api/miniapp/clear_archive', miniapp_clear_archive)
    app.router.add_get('/api/miniapp/chats', miniapp_chats)
    app.router.add_get('/api/miniapp/messages', miniapp_messages)
    app.router.add_get('/api/miniapp/avatar', miniapp_avatar)
    app.router.add_get('/api/miniapp/message_versions', miniapp_message_versions)
    app.router.add_get('/api/miniapp/media', miniapp_media)
    app.router.add_post('/api/miniapp/request_media', miniapp_request_media)
    app.router.add_get('/api/miniapp/referrals', miniapp_referrals)
    app.router.add_get('/api/miniapp/admin/stats', admin_stats)
    app.router.add_post('/api/miniapp/admin/grant', admin_grant)
    app.router.add_post('/api/miniapp/admin/ban', admin_ban)
    app.router.add_post('/api/miniapp/admin/unban', admin_remove_ban)
    app.router.add_get('/api/miniapp/admin/roles', admin_roles)
    app.router.add_post('/api/miniapp/admin/roles', admin_roles)
    app.router.add_post('/api/miniapp/admin/broadcast', admin_broadcast)
    app.router.add_get('/api/miniapp/admin/bans', admin_bans)
    app.router.add_route('OPTIONS', '/{tail:.*}', health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    log.info('API started at 0.0.0.0:%s', PORT)
    return runner
