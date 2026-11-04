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
from datetime import datetime, timezone, timedelta
from aiohttp import web
import mimetypes
from urllib.parse import quote
from pathlib import Path
from html import escape

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiogram.exceptions import TelegramForbiddenError

# Защита от отсутствия admin_bridge
try:
    from admin_bridge import snapshot, execute_task, prepare
except ImportError:
    def snapshot(): return {}
    async def execute_task(*args, **kwargs): return "OK"
    def prepare(): pass

log = logging.getLogger('hades.admin_http')
PASSWORD = 'hades_admin_2026_secure'
SECRET = 'hades_session_secret_key_2026_long_enough_for_hmac_sha256'
ORIGIN = 'https://hadessavemod-art.github.io'.rstrip('/')
MINIAPP_ORIGIN = ORIGIN
PORT = int(os.getenv('PORT', '3000')) 

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

async def get_miniapp_user_id(request):
    raw = request.headers.get('X-Telegram-Init-Data', '') or request.query.get('initData', '')
    if not raw:
        raise web.HTTPUnauthorized(text=json.dumps({'error': 'Нет данных авторизации'}), content_type='application/json')
    
    import __main__
    bot_token = getattr(__main__, 'TOKEN', None) or os.getenv('BOT_TOKEN')
    
    if not bot_token:
        raise web.HTTPInternalServerError(text=json.dumps({'error': 'Ошибка конфигурации сервера'}), content_type='application/json')

    try:
        identity = safe_parse_webapp_init_data(bot_token, raw)
        return identity
    except Exception as e:
        raise web.HTTPUnauthorized(text=json.dumps({'error': f'Неверная подпись Telegram: {e}'}), content_type='application/json')

async def get_admin_user_id(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    
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
            raise web.HTTPForbidden(text=json.dumps({'error': 'Доступ к админ-панели запрещен'}), content_type='application/json')
            
        return user_id, admin['role']

# --- API ДЛЯ MINI APP ---

async def miniapp_stats(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    
    with archive_db() as con:
        banned = con.execute('SELECT reason FROM bans WHERE user_id=?', (user_id,)).fetchone()
        if banned:
            raise web.HTTPForbidden(text=json.dumps({'error': f'Вы забанены: {banned["reason"]}'}), content_type='application/json')
            
        total = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? AND m.deleted=1', (user_id,)).fetchone()[0]
        chats_count = con.execute('SELECT COUNT(DISTINCT m.chat_id) FROM messages m JOIN connections c ON c.id=m.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        media_count = con.execute('SELECT COUNT(*) FROM media_files f JOIN connections c ON c.id=f.connection_id WHERE c.user_id=?', (user_id,)).fetchone()[0]
        
        is_premium = False
        premium_expires_at = None
        prem_row = con.execute('SELECT expires_at FROM premium WHERE user_id=?', (user_id,)).fetchone()
        if prem_row:
            try:
                if datetime.fromisoformat(prem_row['expires_at']) > datetime.now(timezone.utc):
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
        'total_messages': total, 
        'deleted_messages': deleted, 
        'chats_count': chats_count,
        'media_count': media_count,
        'user': name,
        'user_id': user_id,
        'username': identity.user.username or '',
        'is_premium': is_premium,
        'premium_expires_at': premium_expires_at,
        'role': role
    })

async def miniapp_get_settings(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    with archive_db() as con:
        row = con.execute('SELECT edits, deletions, deleted_media FROM notification_prefs WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            con.execute('INSERT OR IGNORE INTO notification_prefs(user_id) VALUES(?)', (user_id,))
            con.commit()
            row = con.execute('SELECT edits, deletions, deleted_media FROM notification_prefs WHERE user_id=?', (user_id,)).fetchone()
    return web.json_response({
        'edits': bool(row['edits']),
        'deletions': bool(row['deletions']),
        'deleted_media': bool(row['deleted_media'])
    })

async def miniapp_update_settings(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    data = await request.json()
    
    with archive_db() as con:
        con.execute('INSERT OR IGNORE INTO notification_prefs(user_id) VALUES(?)', (user_id,))
        if 'edits' in data:
            con.execute('UPDATE notification_prefs SET edits=? WHERE user_id=?', (int(bool(data['edits'])), user_id))
        if 'deletions' in data:
            con.execute('UPDATE notification_prefs SET deletions=? WHERE user_id=?', (int(bool(data['deletions'])), user_id))
        if 'deleted_media' in data:
            con.execute('UPDATE notification_prefs SET deleted_media=? WHERE user_id=?', (int(bool(data['deleted_media'])), user_id))
        con.commit()
    return web.json_response({'success': True})

async def miniapp_clear_archive(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    with archive_db() as con:
        con.execute('DELETE FROM messages WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (user_id,))
        con.execute('DELETE FROM media_files WHERE connection_id IN (SELECT id FROM connections WHERE user_id=?)', (user_id,))
        con.commit()
    return web.json_response({'success': True, 'message': 'Архив полностью очищен'})

async def miniapp_chats(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    filter_type = request.query.get('filter', 'all')
    
    with archive_db() as con:
        sql = '''
           SELECT m.chat_id, 
                  MAX(COALESCE(m.chat_title,m.sender_name,m.chat_username, m.sender_username, CAST(m.chat_id AS TEXT))) AS label,
                  MAX(COALESCE(m.chat_username, m.sender_username, '')) AS chat_username,
                  COUNT(*) AS total, SUM(m.deleted) AS deleted_count,
                  SUM(CASE WHEN m.content_type IN ('photo', 'video', 'voice', 'video_note', 'animation', 'document', 'audio') THEN 1 ELSE 0 END) AS media_count,
                  SUM(m.edited) AS edited_count, MAX(m.updated_at) AS last_at
           FROM messages m JOIN connections c ON c.id=m.connection_id
           WHERE c.user_id=? 
        '''
        params = [user_id]
        
        if filter_type == 'deleted':
            sql += ' AND m.deleted=1 '
        elif filter_type == 'media':
            sql += " AND m.content_type IN ('photo', 'video', 'voice', 'video_note', 'animation', 'document', 'audio') "
            
        sql += ' GROUP BY m.chat_id HAVING COUNT(*) > 0 ORDER BY last_at DESC LIMIT 100'
        
        rows = [dict(r) for r in con.execute(sql, params)]
    return web.json_response({'chats': rows})

async def miniapp_messages(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    chat_id = int(request.query.get('chat_id', '0'))
    filter_type = request.query.get('filter', 'all')
    
    if not chat_id:
        return web.json_response({'error': 'Не указан chat_id'}, status=400)
        
    offset = max(0, int(request.query.get('offset', '0')))
    with archive_db() as con:
        sql = '''
          SELECT m.message_id, m.sender_name, m.sender_username, m.text, m.content_type,
                 m.edited, m.deleted, m.updated_at, f.local_path,
                 CASE WHEN (f.local_path IS NOT NULL AND f.local_path!='') OR (f.file_id IS NOT NULL AND f.file_id!='') THEN 1 ELSE 0 END AS has_media
          FROM messages m JOIN connections c ON c.id=m.connection_id
          LEFT JOIN media_files f ON f.connection_id=m.connection_id AND f.chat_id=m.chat_id AND f.message_id=m.message_id
          WHERE c.user_id=? AND m.chat_id=? 
        '''
        params = [user_id, chat_id]
        
        if filter_type == 'deleted':
            sql += ' AND m.deleted=1 '
        elif filter_type == 'media':
            sql += " AND m.content_type IN ('photo', 'video', 'voice', 'video_note', 'animation', 'document', 'audio') "
            
        sql += ' ORDER BY m.updated_at DESC, m.message_id DESC LIMIT 60 OFFSET ?'
        params.append(offset)
        
        rows = [dict(r) for r in con.execute(sql, params)]
        
        for row in rows:
            row['media_ext'] = Path(row['local_path']).suffix.lower() if row.get('local_path') else ''
            row.pop('local_path', None)
            
    return web.json_response({'messages': rows, 'next_offset': offset + len(rows) if len(rows) == 60 else None})

async def miniapp_media(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    chat_id = int(request.query.get('chat_id', '0'))
    mid = int(request.query.get('message_id', '0'))
    if not chat_id or not mid:
        raise web.HTTPBadRequest(text='Не указан chat_id или message_id')

    with archive_db() as con:
        row = con.execute('''SELECT f.connection_id, f.local_path, f.file_id, m.content_type
           FROM media_files f
           JOIN connections c ON c.id=f.connection_id
           JOIN messages m ON m.connection_id=f.connection_id AND m.chat_id=f.chat_id
                         AND m.message_id=f.message_id
           WHERE c.user_id=? AND f.chat_id=? AND f.message_id=? LIMIT 1''', (user_id, chat_id, mid)).fetchone()

    if not row:
        raise web.HTTPNotFound(text='Медиа не найдено в базе')

    import __main__
    media_root = __main__.MEDIA_DIR.resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    path = Path(row['local_path']).resolve() if row['local_path'] else None

    if not path or not path.is_file() or not path.is_relative_to(media_root):
        if not row['file_id']:
            raise web.HTTPNotFound(text='Файл недоступен')
        suffix = {'photo': '.jpg', 'voice': '.ogg', 'audio': '.mp3',
                  'video': '.mp4', 'video_note': '.mp4', 'animation': '.gif'}.get(row['content_type'], '.bin')
        target = media_root / str(row['connection_id']) / str(chat_id) / f'{mid}{suffix}'
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await request.app['telegram_bot'].download(row['file_id'], destination=target)
            if not target.is_file() or not target.stat().st_size:
                raise IOError('Пустой файл')
        except Exception:
            target.unlink(missing_ok=True)
            raise web.HTTPNotFound(text='Ошибка скачивания файла')
        
        with archive_db() as con:
            con.execute('UPDATE media_files SET local_path=? WHERE connection_id=? AND chat_id=? AND message_id=?',
                        (str(target), row['connection_id'], chat_id, mid))
            con.commit()
        path = target.resolve()

    response = web.FileResponse(path)
    response.content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    response.headers['Content-Disposition'] = 'inline; filename="' + quote(path.name, safe='') + '"'
    return response

async def miniapp_request_media(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    data = await request.json()
    chat_id = int(data.get('chat_id', 0))
    mid = int(data.get('message_id', 0))
    
    if not chat_id or not mid:
        return web.json_response({'error': 'Неверные параметры (chat_id или message_id)'}, status=400)

    with archive_db() as con:
        row = con.execute('''
            SELECT f.local_path, f.file_id, m.content_type, m.text
            FROM media_files f
            JOIN connections c ON c.id=f.connection_id
            JOIN messages m ON m.connection_id=f.connection_id AND m.chat_id=f.chat_id AND m.message_id=f.message_id
            WHERE c.user_id=? AND f.chat_id=? AND f.message_id=? LIMIT 1
        ''', (user_id, chat_id, mid)).fetchone()

    if not row:
        return web.json_response({'error': 'Медиа не найдено в базе данных'}, status=404)

    bot = request.app['telegram_bot']
    caption = row['text'] or ''
    
    import __main__
    from aiogram.types import FSInputFile
    
    media_root = __main__.MEDIA_DIR.resolve()
    path = Path(row['local_path']).resolve() if row['local_path'] else None
    
    if path and path.is_file():
        source = FSInputFile(path)
    elif row['file_id']:
        source = row['file_id']
    else:
        return web.json_response({'error': 'Файл физически отсутствует на сервере'}, status=404)

    ct = row['content_type']
    
    try:
        if ct == 'photo':
            await bot.send_photo(user_id, source, caption=caption[:1024])
        elif ct == 'video':
            await bot.send_video(user_id, source, caption=caption[:1024])
        elif ct == 'video_note':
            await bot.send_video_note(user_id, source)
            if caption: await bot.send_message(user_id, caption)
        elif ct == 'voice':
            await bot.send_voice(user_id, source, caption=caption[:1024])
        elif ct == 'animation':
            await bot.send_animation(user_id, source, caption=caption[:1024])
        elif ct == 'audio':
            await bot.send_audio(user_id, source, caption=caption[:1024])
        else:
            await bot.send_document(user_id, source, caption=caption[:1024])
            
        return web.json_response({'success': True, 'message': 'Файл отправлен в бота! Проверь личку.'})
        
    except Exception as e:
        log.error(f"Cannot send requested media as {ct}: {e}")
        try:
            error_text = f"⚠️ Не удалось отправить как {ct} (возможно, файл слишком большой).\nОтправляю как документ:\n\n{caption[:500]}"
            await bot.send_document(user_id, source, caption=error_text)
            return web.json_response({'success': True, 'message': 'Файл отправлен как документ!'})
        except Exception as fallback_e:
            log.error(f"Fallback document send failed: {fallback_e}")
            return web.json_response({'error': f'Ошибка Telegram: {str(e)[:100]}'}, status=500)

async def miniapp_referrals(request):
    identity = await get_miniapp_user_id(request)
    user_id = identity.user.id
    
    import __main__
    bot_username = getattr(__main__, 'BOT_USERNAME', 'HadesSave_bot')
    
    with archive_db() as con:
        count = con.execute('''
            SELECT COUNT(*) FROM referrals r
            JOIN connections c ON c.user_id = r.referred_id
            WHERE r.referrer_id = ? AND c.enabled = 1
        ''', (user_id,)).fetchone()[0]
        
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    return web.json_response({'link': link, 'count': count})

# --- АДМИНСКИЕ ЭНДПОИНТЫ ---

async def admin_stats(request):
    await get_admin_user_id(request)
    
    with archive_db() as con:
        users = con.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        conns = con.execute('SELECT COUNT(*) FROM connections WHERE enabled=1').fetchone()[0]
        msgs = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        deleted = con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0]
        prem = con.execute('SELECT COUNT(*) FROM premium WHERE expires_at > ?', (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
        media = con.execute('SELECT COUNT(*) FROM media_files').fetchone()[0]
        
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        week_prem = con.execute('SELECT COUNT(*) FROM payments WHERE created_at > ?', (week_ago,)).fetchone()[0]
        week_stars = con.execute('SELECT SUM(stars) FROM payments WHERE created_at > ?', (week_ago,)).fetchone()[0] or 0
        total_stars = con.execute('SELECT SUM(stars) FROM payments').fetchone()[0] or 0
        
    return web.json_response({
        'users': users, 'connections': conns, 'messages': msgs, 'deleted': deleted,
        'premium': prem, 'media': media,
        'week_premium': week_prem, 'week_stars': week_stars, 'total_stars': total_stars
    })

async def admin_grant(request):
    await get_admin_user_id(request)
    data = await request.json()
    target = data.get('target', '').strip()
    days = int(data.get('days', 0))
    
    if days < 1 or days > 3650:
        return web.json_response({'error': 'Неверное количество дней'}, status=400)
        
    with archive_db() as con:
        target_id = None
        if target.startswith('@'):
            row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
            if row: target_id = row['id']
        else:
            try: target_id = int(target)
            except ValueError: pass
            
        if not target_id:
            return web.json_response({'error': 'Пользователь не найден'}, status=404)
            
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at', (target_id, expires))
        con.commit()
    
    bot = request.app['telegram_bot']
    try:
        await bot.send_message(target_id, f'👑 <b>ПОДПИСКА АКТИВИРОВАНА</b>\nАдминистратор выдал тебе <b>Premium на {days} дней</b>!', parse_mode='HTML')
    except Exception as e:
        log.error(f"Cannot notify premium grant to {target_id}: {e}")
        
    return web.json_response({'success': True, 'message': f'Premium выдан {target_id} на {days} дней'})

async def admin_ban(request):
    await get_admin_user_id(request)
    data = await request.json()
    target = data.get('target', '').strip()
    reason = data.get('reason', 'Без причины').strip()
    
    with archive_db() as con:
        target_id = None
        if target.startswith('@'):
            row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
            if row: target_id = row['id']
        else:
            try: target_id = int(target)
            except ValueError: pass
            
        if not target_id:
            return web.json_response({'error': 'Пользователь не найден'}, status=404)
            
        admin = con.execute('SELECT role FROM admins WHERE user_id=?', (target_id,)).fetchone()
        if admin:
            return web.json_response({'error': 'Нельзя забанить администратора'}, status=403)
            
        con.execute('INSERT INTO bans(user_id, reason) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason', (target_id, reason))
        con.commit()
    
    bot = request.app['telegram_bot']
    try:
        await bot.send_message(target_id, f'⛔ <b>ДОСТУП ЗАБЛОКИРОВАН</b>\nПричина: <b>{reason}</b>', parse_mode='HTML')
    except Exception as e:
        log.error(f"Cannot notify ban to {target_id}: {e}")
        
    return web.json_response({'success': True, 'message': f'Пользователь {target_id} забанен'})

async def admin_roles(request):
    await get_admin_user_id(request)
    
    if request.method == 'GET':
        with archive_db() as con:
            rows = [dict(r) for r in con.execute('SELECT user_id, role FROM admins')]
        return web.json_response({'admins': rows})
        
    elif request.method == 'POST':
        data = await request.json()
        target = data.get('target', '').strip()
        role = data.get('role', '').strip().lower()
        
        if role not in ('owner', 'admin', 'aboba', 'none'):
            return web.json_response({'error': 'Неверный префикс.'}, status=400)
            
        with archive_db() as con:
            target_id = None
            if target.startswith('@'):
                row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
                if row: target_id = row['id']
            else:
                try: target_id = int(target)
                except ValueError: pass
                
            if not target_id:
                return web.json_response({'error': 'Пользователь не найден.'}, status=404)
                
            if role == 'none':
                con.execute('DELETE FROM admins WHERE user_id=?', (target_id,))
            else:
                con.execute('INSERT INTO admins(user_id, role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role', (target_id, role))
            con.commit()
        
        bot = request.app['telegram_bot']
        try:
            if role == 'none':
                await bot.send_message(target_id, "🛡 <b>АДМИН-ДОСТУП ОТОЗВАН</b>", parse_mode='HTML')
            else:
                await bot.send_message(target_id, f"👑 <b>АДМИН-ДОСТУП ВЫДАН</b>\nПрефикс: <b>{role}</b>", parse_mode='HTML')
        except Exception: pass
                
        return web.json_response({'success': True, 'message': f'Роль {role} обновлена'})

async def admin_broadcast(request):
    await get_admin_user_id(request)
    data = await request.json()
    text = data.get('text', '').strip()
    if not text:
        return web.json_response({'error': 'Пустой текст'}, status=400)
    
    with archive_db() as con:
        user_ids = [r['id'] for r in con.execute('SELECT id FROM users').fetchall()]
    
    bot = request.app['telegram_bot']
    success = 0
    blocked = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode='HTML')
            success += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception: pass
        await asyncio.sleep(0.05)
        
    return web.json_response({'success': True, 'message': f'Доставлено: {success}, Заблокировали: {blocked}'})

async def admin_bans(request):
    await get_admin_user_id(request)
    with archive_db() as con:
        rows = [dict(r) for r in con.execute('SELECT b.user_id, b.reason, b.banned_at, u.first_name, u.username FROM bans b LEFT JOIN users u ON u.id=b.user_id ORDER BY b.banned_at DESC')]
    return web.json_response({'bans': rows})

async def admin_remove_ban(request):
    await get_admin_user_id(request)
    data = await request.json()
    target = data.get('target', '').strip()
    if target.startswith('@'):
        with archive_db() as con:
            row = con.execute('SELECT id FROM users WHERE username = ?', (target[1:],)).fetchone()
            target_id = row['id'] if row else None
    else:
        try: target_id = int(target)
        except ValueError: target_id = None
        
    if not target_id:
        return web.json_response({'error': 'Пользователь не найден'}, status=404)
    
    with archive_db() as con:
        con.execute('DELETE FROM bans WHERE user_id=?', (target_id,))
        con.commit()
    
    return web.json_response({'success': True, 'message': f'Пользователь {target_id} разбанен'})

async def health(request):
    return web.json_response({'status': 'ok', 'service': 'HadesSave API'})

async def start_server(bot):
    prepare()
    app = web.Application(middlewares=[secure], client_max_size=64 * 1024)
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
