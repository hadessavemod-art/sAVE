"""Seven-day rolling retention for archived Business messages and downloaded media."""
import asyncio
import logging
from pathlib import Path

log = logging.getLogger('hades.retention')


def cleanup(db, media_dir):
    root = Path(media_dir).resolve()
    paths = []
    n = 0
    with db() as con:
        cols = {r[1] for r in con.execute('PRAGMA table_info(messages)')}
        if 'archived_at' not in cols:
            con.execute('ALTER TABLE messages ADD COLUMN archived_at TEXT')
            con.execute('UPDATE messages SET archived_at=COALESCE(updated_at,CURRENT_TIMESTAMP) WHERE archived_at IS NULL')
        con.execute('CREATE INDEX IF NOT EXISTS idx_messages_archived_at ON messages(archived_at)')
        con.execute('CREATE TEMP TABLE IF NOT EXISTS _expired (connection_id TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY(connection_id,chat_id,message_id))')
        con.execute('DELETE FROM _expired')
        con.execute("""INSERT OR IGNORE INTO _expired SELECT connection_id,chat_id,message_id FROM messages
            WHERE datetime(COALESCE(archived_at,updated_at)) <= datetime('now','-7 days')""")
        n = con.execute('SELECT COUNT(*) FROM _expired').fetchone()[0]
        paths = [r[0] for r in con.execute('''SELECT local_path FROM media_files mf WHERE local_path IS NOT NULL AND
            EXISTS(SELECT 1 FROM _expired e WHERE e.connection_id=mf.connection_id AND e.chat_id=mf.chat_id AND e.message_id=mf.message_id)''')]
        for table in ('media_files','message_versions','reply_captures'):
            con.execute(f'''DELETE FROM {table} WHERE EXISTS(SELECT 1 FROM _expired e
                WHERE e.connection_id={table}.connection_id AND e.chat_id={table}.chat_id AND e.message_id={table}.message_id)''')
        con.execute('''DELETE FROM messages WHERE EXISTS(SELECT 1 FROM _expired e
            WHERE e.connection_id=messages.connection_id AND e.chat_id=messages.chat_id AND e.message_id=messages.message_id)''')
        con.execute("DELETE FROM media_diagnostics WHERE datetime(created_at) <= datetime('now','-7 days')")
        con.execute("DELETE FROM business_update_diagnostics WHERE datetime(created_at) <= datetime('now','-7 days')")
        con.execute('DELETE FROM _expired')
    removed = 0
    for path in set(paths):
        try:
            file = Path(path).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                continue
            with db() as con:
                still_used = con.execute('SELECT 1 FROM media_files WHERE local_path=? LIMIT 1',(str(file),)).fetchone()
            if not still_used:
                file.unlink()
                removed += 1
        except OSError:
            log.exception('Failed to remove expired media')
    if n:
        log.info('Deleted %d expired archive messages and %d media files',n,removed)


async def cleanup_loop(db, media_dir):
    while True:
        try:
            await asyncio.to_thread(cleanup, db, media_dir)
        except Exception:
            log.exception('Archive retention cleanup failed')
        await asyncio.sleep(3600)
