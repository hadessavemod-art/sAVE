"""Direct HTTPS API behind Bothost reverse proxy; no Cloudflare dependency."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from aiohttp import web
import mimetypes
from urllib.parse import quote
from pathlib import Path

# Импортируем валидатор для Mini App
from telegram_init_data import validate_init_data
from admin_bridge import snapshot, execute_task, prepare

log = logging.getLogger('hades.admin_http')
PASSWORD = 'hades_admin_2026_secure'
SECRET = 'hades_session_secret_key_2026_long_enough_for_hmac_sha256'
ORIGIN = 'https://hadessavemod-art.github.io'.rstrip('/')
MINIAPP_ORIGIN = ORIGIN
PORT = 8000 # Основной порт
TTL = 8 * 3600
RESULTS = {}
LOGIN_ATTEMPTS = defaultdict(deque)

def sign_token():
    payload = json.dumps({'exp': int(time.time()) + TTL, 'nonce': secrets.token_hex(16)}, separators=(',', ':')).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b'=').decode()
    sig = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + sig

def valid_token(token):
    try:
        encoded, signature = token.rsplit('.', 1)
        expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        return time.time() < int(payload.get('exp', 0))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False

@web.middleware
async def secure(request, handler):
    origin = request.headers.get('Origin')
    if origin and origin not in (ORIGIN, MINIAPP_ORIGIN):
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
            
    if origin in (ORIGIN, MINIAPP_ORIGIN):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Telegram-Init-Data'
        response.headers['Vary'] = 'Origin'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def authenticate(request):
    raw = request.headers.get('Authorization', '')
    if not raw.startswith('Bearer ') or not valid_token(raw[7:]):
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Войдите заново'}), content_type='application/json')

async def login(request):
    address = request.remote or 'unknown'
    now = time.monotonic()
    entries = LOGIN_ATTEMPTS[address]
    while entries and now - entries[0] > 900:
        entries.popleft()
    if len(entries) >= 8:
        return web.json_response({'error': 'Слишком много попыток.'}, status=429)
    data = await request.json()
    guess = str(data.get('password', ''))
    if not hmac.compare_digest(guess.encode(), PASSWORD.encode()):
        entries.append(now)
        await asyncio.sleep(0.3)
        return web.json_response({'error': 'Неверный пароль'}, status=401)
    entries.clear()
    return web.json_response({'token': sign_token()})

async def state(request):
    authenticate(request)
    return web.json_response({'state': snapshot()})

async def command(request):
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
            result = {'ok': True, 'message': str(value)}
        except Exception as exc:
            log.exception('Admin command %s failed', action)
            result = {'ok': False, 'message': str(exc)[:350]}
        RESULTS[job_id]['result'] = result

    asyncio.create_task(work())
    for key, item in list(RESULTS.items()):
        if time.monotonic() - item['created'] > 3600:
            RESULTS.pop(key, None)
    return web.json_response({'id': job_id})

async def result(request):
    authenticate(request)
    job_id = request.query.get('id', '')
    return web.json_response({'result': RESULTS.get(job_id, {}).get('result')})

def archive_db():
    import __main__
    return __main__.db()

def personal_archive_owner(request, requested_id=None):
    raw = '8609600422'
    admin_id = int(raw) if raw.isdecimal() and int(raw) > 0 else None
    if requested_id is None:
        return admin_id
    return int(requested_id)

async def miniapp_stats(request):
    raw = request.headers.get('X-Telegram-Init-Data', '')
    if not raw:
        return web.json_response({'error': 'Нет данных авторизации'}, status=401)
    import __main__
    bot_token = __main__.TOKEN 
    try:
        identity = validate_init_data(raw, bot_token, max_age=86400)
    except ValueError as e:
        return web.json_response({'error': str(e)}, status=401)
    user_id = identity['user']['id']
    with archive_db() as con:
        total = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.deleted=1', (user_id,)).fetchone()[0]
    return web.json_response({'total_messages': total, 'deleted_messages': deleted, 'user': identity['user'].get('first_name', '')})

async def archive_users(request):
    authenticate(request)
    admin_id = personal_archive_owner(request)
    with archive_db() as con:
        rows = [dict(r) for r in con.execute('''
            SELECT u.id, u.first_name, u.username,
              COUNT(DISTINCT c.id) AS connections,
              (SELECT COUNT(*) FROM messages m JOIN connections x ON x.id=m.connection_id
               WHERE x.user_id=u.id) AS message_count
            FROM users u JOIN connections c ON c.user_id=u.id
            GROUP BY u.id ORDER BY u.joined_at DESC
        ''')]
    for row in rows:
        row['archive_allowed'] = 1
    return web.json_response({'users': rows})

async def archive_chats(request):
    authenticate(request)
    uid = personal_archive_owner(request, request.match_info['user_id'])
    with archive_db() as con:
        rows = [dict(r) for r in con.execute('''
           SELECT m.chat_id, MAX(COALESCE(m.chat_title,m.sender_name,m.chat_username,
                    m.sender_username,CAST(m.chat_id AS TEXT))) AS label,
                  COUNT(*) AS total, SUM(m.deleted) AS deleted_count,
                  SUM(m.edited) AS edited_count, MAX(m.updated_at) AS last_at
           FROM messages m JOIN connections c ON c.id=m.connection_id
           WHERE c.user_id=? GROUP BY m.chat_id ORDER BY last_at DESC LIMIT 300
        ''', (uid,))]
    return web.json_response({'chats': rows})

async def archive_messages(request):
    authenticate(request)
    uid = personal_archive_owner(request, request.match_info['user_id'])
    chat_id = int(request.match_info['chat_id'])
    cursor = max(0, int(request.query.get('offset','0')))
    cursor = min(cursor, 100000)
    with archive_db() as con:
        rows = [dict(r) for r in con.execute('''
          SELECT m.connection_id, m.chat_id, m.message_id, m.sender_id,
                 m.sender_name, m.sender_username, m.text, m.content_type,
                 m.edited, m.deleted, m.updated_at,
                 CASE WHEN (f.local_path IS NOT NULL AND f.local_path!='') OR (f.file_id IS NOT NULL AND f.file_id!='') THEN 1 ELSE 0 END AS has_local_media
          FROM messages m JOIN connections c ON c.id=m.connection_id
          LEFT JOIN media_files f ON f.connection_id=m.connection_id
             AND f.chat_id=m.chat_id AND f.message_id=m.message_id
          WHERE c.user_id=? AND m.chat_id=? ORDER BY m.updated_at DESC,m.message_id DESC
          LIMIT 60 OFFSET ?
        ''', (uid,chat_id,cursor))]
        for row in rows:
            if row['edited']:
                row['versions'] = [dict(v) for v in con.execute('''
                    SELECT old_text,new_text,changed_at FROM message_versions
                    WHERE connection_id=? AND chat_id=? AND message_id=?
                    ORDER BY id DESC LIMIT 10''',
                    (row['connection_id'],chat_id,row['message_id']))]
            else:
                row['versions'] = []
            row.pop('connection_id',None)
    return web.json_response({'messages':rows,'next_offset':cursor+len(rows) if len(rows)==60 else None})

async def archive_media(request):
    authenticate(request)
    uid = personal_archive_owner(request, request.match_info['user_id'])
    chat_id = int(request.match_info['chat_id'])
    mid = int(request.match_info['message_id'])
    with archive_db() as con:
        row = con.execute('''SELECT f.connection_id, f.local_path, f.file_id, m.content_type
           FROM media_files f
           JOIN connections c ON c.id=f.connection_id
           JOIN messages m ON m.connection_id=f.connection_id AND m.chat_id=f.chat_id
                         AND m.message_id=f.message_id
           WHERE c.user_id=? AND f.chat_id=? AND f.message_id=? LIMIT 1''', (uid,chat_id,mid)).fetchone()
    if not row:
        raise web.HTTPNotFound(text='Медиа не зарегистрировано')
    import __main__
    media_root = __main__.MEDIA_DIR.resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    path = Path(row['local_path']).resolve() if row['local_path'] else None
    if not path or not path.is_file() or not path.is_relative_to(media_root):
        if not row['file_id']:
            raise web.HTTPNotFound(text='Файл не был сохранён ботом')
        suffix = {'photo': '.jpg', 'voice': '.ogg', 'audio': '.mp3',
                  'video': '.mp4', 'video_note': '.mp4', 'animation': '.gif',
                  'sticker': '.webp'}.get(row['content_type'], '.bin')
        target = media_root / str(row['connection_id']) / str(chat_id) / f'{mid}{suffix}'
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await request.app['telegram_bot'].download(row['file_id'], destination=target)
            if not target.is_file() or not target.stat().st_size:
                raise IOError('Пустой файл')
        except Exception:
            log.warning('Cannot recover Telegram media for message %s', mid, exc_info=True)
            target.unlink(missing_ok=True)
            raise web.HTTPNotFound(text='Telegram не предоставил файл; восстановление невозможно')
        with archive_db() as con:
            con.execute('UPDATE media_files SET local_path=? WHERE connection_id=? AND chat_id=? AND message_id=?',
                        (str(target),row['connection_id'],chat_id,mid))
            con.commit()
        path = target.resolve()
    if not path.is_relative_to(media_root) or not path.is_file():
        raise web.HTTPNotFound()
    response = web.FileResponse(path)
    response.content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    response.headers['Content-Disposition'] = 'inline; filename="' + quote(path.name, safe='') + '"'
    return response

async def health(request):
    return web.json_response({'status': 'ok', 'service': 'HadesSave API'})

async def start_server(bot):
    if len(PASSWORD) < 12 or len(SECRET) < 32 or not ORIGIN.startswith('https://'):
        raise RuntimeError('Укажи ADMIN_PASSWORD (>=12 символов), ADMIN_SESSION_SECRET (>=32 символов), ADMIN_ALLOWED_ORIGIN (https://...) в Bothost')
    prepare()
    app = web.Application(middlewares=[secure], client_max_size=64 * 1024)
    app['telegram_bot'] = bot
    
    app.router.add_get('/health', health)
    app.router.add_post('/admin/login', login)
    app.router.add_get('/admin/state', state)
    app.router.add_post('/admin/command', command)
    app.router.add_get('/admin/result', result)
    
    app.router.add_get('/admin/archive/users', archive_users)
    app.router.add_get('/admin/archive/users/{user_id:\\d+}/chats', archive_chats)
    app.router.add_get('/admin/archive/users/{user_id:\\d+}/chats/{chat_id:-?\\d+}/messages', archive_messages)
    app.router.add_get('/admin/archive/users/{user_id:\\d+}/chats/{chat_id:-?\\d+}/media/{message_id:\\d+}', archive_media)
    
    # API для Mini App
    app.router.add_get('/api/miniapp/stats', miniapp_stats)
    
    app.router.add_route('OPTIONS', '/{tail:.*}', health)
    
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    log.info('API started at 0.0.0.0:%s', PORT)
    return runner
