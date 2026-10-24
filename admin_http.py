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

from admin_bridge import snapshot, execute_task, prepare

log = logging.getLogger('hades.admin_http')
PASSWORD = os.getenv('ADMIN_PASSWORD', '')
SECRET = os.getenv('ADMIN_SESSION_SECRET', '')
ORIGIN = os.getenv('ADMIN_ALLOWED_ORIGIN', 'https://hadessavemod-art.github.io').rstrip('/')
PORT = int(os.getenv('PORT', '8000'))
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
        response.headers['Access-Control-Allow-Origin'] = ORIGIN
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
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
        return web.json_response({'error': 'Слишком много попыток. Попробуй позже.'}, status=429)
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
    # This endpoint only exposes admin metadata; not message/media contents.
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
    # Avoid unbounded memory growth.
    for key, item in list(RESULTS.items()):
        if time.monotonic() - item['created'] > 3600:
            RESULTS.pop(key, None)
    return web.json_response({'id': job_id})


async def result(request):
    authenticate(request)
    job_id = request.query.get('id', '')
    return web.json_response({'result': RESULTS.get(job_id, {}).get('result')})


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
    app.router.add_route('OPTIONS', '/{tail:.*}', health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    log.info('Admin API started at 0.0.0.0:%s', PORT)
    return runner
