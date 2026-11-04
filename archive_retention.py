import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger('hades.retention')

async def cleanup_loop(db, media_dir):
    """Фоновая очистка: удаляет старые сообщения и файлы.
    Для премиум-пользователей хранит 30 дней, для остальных 3 дня.
    """
    media_dir = Path(media_dir).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    
    while True:
        try:
            await asyncio.sleep(3600)  # раз в час
            now = datetime.now(timezone.utc)
            
            with db() as con:
                # Получаем премиум-пользователей
                prem_rows = con.execute('SELECT user_id, expires_at FROM premium').fetchall()
                prem_users = set()
                for r in prem_rows:
                    try:
                        if datetime.fromisoformat(r['expires_at']) > now:
                            prem_users.add(r['user_id'])
                    except Exception:
                        pass
                
                # Получаем все соединения
                conns = con.execute('SELECT id, user_id FROM connections').fetchall()
                
                for conn in conns:
                    conn_id = conn['id']
                    uid = conn['user_id']
                    days = 30 if uid in prem_users else 3
                    cutoff = (now - timedelta(days=days)).isoformat()
                    
                    # Удаляем старые сообщения
                    con.execute(
                        'DELETE FROM messages WHERE connection_id=? AND updated_at < ?',
                        (conn_id, cutoff)
                    )
                
                con.commit()
                
                # Собираем все файлы, на которые есть ссылки в media_files
                referenced = set()
                for row in con.execute('SELECT local_path FROM media_files WHERE local_path IS NOT NULL AND local_path != ""'):
                    p = Path(row['local_path']).resolve()
                    if p.is_file():
                        referenced.add(p)
                    else:
                        con.execute('DELETE FROM media_files WHERE local_path=?', (row['local_path'],))
                
                con.commit()
            
            # Удаляем файлы, на которые нет ссылок
            for file in media_dir.rglob('*'):
                if file.is_file() and file not in referenced:
                    try:
                        file.unlink()
                    except Exception:
                        pass
            
            log.info('Retention cleanup completed')
            
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('Retention error: %s', e)
            await asyncio.sleep(60)
