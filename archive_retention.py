"""Periodic archive cleanup for HadesSave.

Active Premium users keep messages/media for 30 days; other users for 3 days.
Only orphaned files inside MEDIA_DIR are removed. The Mini App avatar cache is
stored in a sibling directory and is intentionally outside this cleanup root.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger('hades.retention')


def cleanup_once(db, media_dir, now=None):
    media_root = Path(media_dir).resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    removed_messages = 0
    removed_versions = 0
    removed_media_rows = 0
    referenced = set()

    with db() as con:
        premium_users = set()
        for row in con.execute('SELECT user_id, expires_at FROM premium').fetchall():
            try:
                expires = datetime.fromisoformat(str(row['expires_at']))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires.astimezone(timezone.utc) > now:
                    premium_users.add(int(row['user_id']))
            except (TypeError, ValueError, OverflowError):
                continue

        connections = con.execute('SELECT id, user_id FROM connections').fetchall()
        for connection in connections:
            days = 30 if int(connection['user_id'] or 0) in premium_users else 3
            cutoff = (now - timedelta(days=days)).isoformat()
            cursor = con.execute(
                '''DELETE FROM messages
                   WHERE connection_id=?
                     AND datetime(COALESCE(message_date,updated_at)) < datetime(?)''',
                (connection['id'], cutoff),
            )
            removed_messages += max(cursor.rowcount, 0)

        cursor = con.execute('''DELETE FROM message_versions
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.connection_id=message_versions.connection_id
                  AND m.chat_id=message_versions.chat_id
                  AND m.message_id=message_versions.message_id
            )''')
        removed_versions = max(cursor.rowcount, 0)

        # These small bookkeeping tables must not grow after their parent
        # messages have expired from the archive.
        con.execute('''DELETE FROM deleted_notified
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.connection_id=deleted_notified.connection_id
                  AND m.chat_id=deleted_notified.chat_id
                  AND m.message_id=deleted_notified.message_id
            )''')
        con.execute('''DELETE FROM reply_captures
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.connection_id=reply_captures.connection_id
                  AND m.chat_id=reply_captures.chat_id
                  AND m.message_id=reply_captures.message_id
            )''')

        cursor = con.execute('''DELETE FROM media_files
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.connection_id=media_files.connection_id
                  AND m.chat_id=media_files.chat_id
                  AND m.message_id=media_files.message_id
            )''')
        removed_media_rows = max(cursor.rowcount, 0)

        for row in con.execute('''SELECT local_path FROM media_files
            WHERE local_path IS NOT NULL AND length(local_path) > 0'''):
            raw_path = row['local_path']
            try:
                resolved = Path(raw_path).resolve()
                if resolved.is_file() and resolved.is_relative_to(media_root):
                    referenced.add(resolved)
                else:
                    con.execute('DELETE FROM media_files WHERE local_path=?', (raw_path,))
            except (OSError, RuntimeError, ValueError, TypeError):
                con.execute('DELETE FROM media_files WHERE local_path=?', (raw_path,))
        con.commit()

    removed_files = 0
    for file_path in media_root.rglob('*'):
        try:
            if not file_path.is_file():
                continue
            resolved = file_path.resolve()
            if resolved.is_relative_to(media_root) and resolved not in referenced:
                resolved.unlink(missing_ok=True)
                removed_files += 1
        except (OSError, RuntimeError, ValueError):
            log.warning('Could not remove orphan media file: %s', file_path, exc_info=True)

    return {
        'messages': removed_messages,
        'versions': removed_versions,
        'media_rows': removed_media_rows,
        'files': removed_files,
    }


async def cleanup_loop(db, media_dir):
    """Run cleanup once per hour; retry after one minute on failures."""
    while True:
        try:
            await asyncio.sleep(3600)
            result = cleanup_once(db, media_dir)
            log.info(
                'Retention completed: %(messages)s messages, %(versions)s versions, '
                '%(media_rows)s media rows, %(files)s files removed', result,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Retention cleanup failed')
            await asyncio.sleep(60)
