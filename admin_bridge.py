"""Optional Cloudflare Worker bridge for HadesSave admin (no inbound ports)."""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp

LOG = logging.getLogger('hades_admin_bridge')
WORKER_URL = os.getenv('ADMIN_WORKER_URL', '').strip().rstrip('/')
BRIDGE_KEY = os.getenv('ADMIN_BRIDGE_KEY', '').strip()


def database():
    # main.py runs as __main__ on Bothost; avoid importing it a second time.
    import __main__
    return __main__.db()


def prepare():
    with database() as con:
        con.execute('CREATE TABLE IF NOT EXISTS admin_blocks (user_id INTEGER PRIMARY KEY, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        con.execute('CREATE TABLE IF NOT EXISTS admin_config (name TEXT PRIMARY KEY, value TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS admin_task_results (id TEXT PRIMARY KEY, result TEXT, completed_at TEXT DEFAULT CURRENT_TIMESTAMP)')


def is_blocked(user_id):
    if not user_id:
        return False
    with database() as con:
        return bool(con.execute('SELECT 1 FROM admin_blocks WHERE user_id=?', (user_id,)).fetchone())


def snapshot():
    with database() as con:
        users = [dict(r) for r in con.execute('''
            SELECT u.id, u.username, u.first_name, u.joined_at,
                   p.expires_at AS premium_until,
                   CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END AS blocked,
                   (SELECT COUNT(*) FROM connections c WHERE c.user_id=u.id AND c.enabled=1) AS connections,
                   (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.id) AS referrals
            FROM users u LEFT JOIN premium p ON p.user_id=u.id
            LEFT JOIN admin_blocks b ON b.user_id=u.id ORDER BY u.joined_at DESC LIMIT 5000
        ''')]
        support = [dict(r) for r in con.execute('SELECT id,user_id,body,status,created_at FROM support ORDER BY id DESC LIMIT 200')]
        counts = {
            'users': con.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'business': con.execute('SELECT COUNT(*) FROM connections WHERE enabled=1').fetchone()[0],
            'messages': con.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
            'deleted': con.execute('SELECT COUNT(*) FROM messages WHERE deleted=1').fetchone()[0],
            'edited': con.execute('SELECT COUNT(*) FROM messages WHERE edited=1').fetchone()[0],
            'premium': con.execute('SELECT COUNT(*) FROM premium WHERE expires_at>?', (datetime.now(timezone.utc).isoformat(),)).fetchone()[0],
            'blocked': con.execute('SELECT COUNT(*) FROM admin_blocks').fetchone()[0],
        }
        config = {r['name']:r['value'] for r in con.execute('SELECT name,value FROM admin_config')}
    # No private message contents or media are synced to GitHub / Cloudflare.
    return {'users':users, 'support':support, 'counts':counts, 'config':config,
            'synced_at':datetime.now(timezone.utc).isoformat()}


async def execute_task(task, bot):
    action = task.get('action')
    data = task.get('data') or {}
    uid = int(data.get('user_id', 0)) if str(data.get('user_id', '0')).isdigit() else 0
    if action in ('premium_grant','premium_revoke','block','unblock','support_reply') and not uid:
        raise ValueError('Укажи корректный Telegram ID')
    if action == 'premium_grant':
        days = int(data.get('days', 0))
        if not 1 <= days <= 3650:
            raise ValueError('Срок Premium должен быть 1–3650 дней')
        with database() as con:
            existing = con.execute('SELECT expires_at FROM premium WHERE user_id=?',(uid,)).fetchone()
            now = datetime.now(timezone.utc)
            since = max(now, datetime.fromisoformat(existing['expires_at'])) if existing else now
            till = (since+timedelta(days=days)).isoformat()
            con.execute('INSERT INTO premium(user_id,expires_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at',(uid,till))
        return f'Premium до {till}'
    if action == 'premium_revoke':
        with database() as con:
            con.execute('DELETE FROM premium WHERE user_id=?',(uid,))
        return 'Premium отключён'
    if action == 'block':
        with database() as con:
            con.execute('INSERT OR IGNORE INTO admin_blocks(user_id,reason) VALUES(?,?)',(uid,str(data.get('reason',''))[:250]))
        return 'Доступ к боту заблокирован'
    if action == 'unblock':
        with database() as con:
            con.execute('DELETE FROM admin_blocks WHERE user_id=?',(uid,))
        return 'Блокировка снята'
    if action == 'support_reply':
        message = str(data.get('text','')).strip()[:3500]
        if not message:
            raise ValueError('Пустой ответ')
        await bot.send_message(uid, '🆘 <b>Ответ поддержки HadesSave</b>\n\n'+__import__('html').escape(message),parse_mode='HTML')
        ticket_id = int(data.get('ticket_id',0))
        if ticket_id:
            with database() as con:
                con.execute('UPDATE support SET status=? WHERE id=? AND user_id=?',('answered',ticket_id,uid))
        return 'Ответ отправлен'
    if action == 'broadcast':
        message = str(data.get('text','')).strip()[:4000]
        if not message:
            raise ValueError('Пустая рассылка')
        with database() as con:
            ids = [r[0] for r in con.execute('SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM admin_blocks)')]
        success = 0
        for recipient in ids:
            try:
                await bot.send_message(recipient, message)
                success += 1
            except Exception:
                pass
            await asyncio.sleep(0.06)
        return f'Отправлено {success} из {len(ids)}'
    if action == 'config_set':
        key = str(data.get('key', ''))
        if key not in ('broadcast_enabled','registration_enabled'):
            raise ValueError('Неизвестная настройка')
        with database() as con:
            con.execute('INSERT INTO admin_config(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (key, '1' if data.get('enabled') else '0'))
        return 'Настройка сохранена'
    raise ValueError('Неизвестная команда: '+str(action))


async def run_bridge(bot):
    if not WORKER_URL or not BRIDGE_KEY:
        LOG.info('Admin bridge disabled (ADMIN_WORKER_URL / ADMIN_BRIDGE_KEY not configured)')
        return
    prepare()
    headers = {'Authorization':f'Bearer {BRIDGE_KEY}'}
    timeout = aiohttp.ClientTimeout(total=22)
    async with aiohttp.ClientSession(timeout=timeout,headers=headers) as http:
        last_sync = 0.0
        while True:
            try:
                loop = asyncio.get_running_loop()
                if loop.time()-last_sync > 180:
                    async with http.post(WORKER_URL+'/bot/sync',json=snapshot()) as response:
                        response.raise_for_status()
                    last_sync = loop.time()
                async with http.get(WORKER_URL+'/bot/tasks') as response:
                    response.raise_for_status()
                    tasks = (await response.json()).get('tasks',[])
                for task in tasks[:20]:
                    task_id = str(task.get('id',''))
                    if not task_id:
                        continue
                    with database() as con:
                        prior = con.execute('SELECT result FROM admin_task_results WHERE id=?',(task_id,)).fetchone()
                    if prior:
                        result = {'ok':True,'message':prior['result']}
                    else:
                        try:
                            msg = await execute_task(task,bot)
                            result = {'ok':True,'message':msg}
                            with database() as con:
                                con.execute('INSERT OR IGNORE INTO admin_task_results(id,result) VALUES(?,?)',(task_id,msg))
                        except Exception as exc:
                            LOG.exception('Admin task failed: %s',task_id)
                            result = {'ok':False,'message':str(exc)[:350]}
                    async with http.post(WORKER_URL+'/bot/ack',json={'id':task_id,**result}) as response:
                        response.raise_for_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('Admin bridge sync failed; retrying')
            await asyncio.sleep(12)
