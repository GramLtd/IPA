"""Фоновый воркер: публикует запланированные задания и опрашивает их статус."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import now
from .tiktok import STATUS_DONE, STATUS_FAILED, TikTokClient, TikTokError

log = logging.getLogger("panel.worker")

# Обновляем access_token заранее, если до истечения меньше этого запаса
REFRESH_MARGIN = timedelta(minutes=10)


def _ts(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(
        timespec="seconds"
    )


def save_tokens(conn: sqlite3.Connection, tok: dict, user: dict | None = None) -> int:
    """Создаёт или обновляет аккаунт по open_id и возвращает его id."""
    user = user or {}
    conn.execute(
        """INSERT INTO accounts (open_id, display_name, avatar_url, access_token, refresh_token,
                                 expires_at, refresh_expires_at, scope, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(open_id) DO UPDATE SET
             display_name = COALESCE(excluded.display_name, display_name),
             avatar_url = COALESCE(excluded.avatar_url, avatar_url),
             access_token = excluded.access_token,
             refresh_token = excluded.refresh_token,
             expires_at = excluded.expires_at,
             refresh_expires_at = excluded.refresh_expires_at,
             scope = excluded.scope""",
        (
            tok["open_id"],
            user.get("display_name"),
            user.get("avatar_url"),
            tok["access_token"],
            tok["refresh_token"],
            _ts(tok.get("expires_in", 86400)),
            _ts(tok.get("refresh_expires_in", 31536000)),
            tok.get("scope"),
            now(),
        ),
    )
    conn.commit()
    return conn.execute("SELECT id FROM accounts WHERE open_id=?", (tok["open_id"],)).fetchone()[0]


async def get_token(conn: sqlite3.Connection, tt: TikTokClient, account_id: int) -> str:
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acc is None:
        raise TikTokError("аккаунт удалён")
    expires = datetime.fromisoformat(acc["expires_at"])
    if expires - datetime.now(timezone.utc) > REFRESH_MARGIN:
        return acc["access_token"]
    if datetime.fromisoformat(acc["refresh_expires_at"]) <= datetime.now(timezone.utc):
        raise TikTokError("refresh_token истёк — переподключите аккаунт")
    tok = await tt.refresh(acc["refresh_token"])
    tok.setdefault("open_id", acc["open_id"])
    save_tokens(conn, tok)
    return tok["access_token"]


def _set(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


async def publish_job(conn: sqlite3.Connection, tt: TikTokClient, job: sqlite3.Row) -> None:
    video = conn.execute("SELECT * FROM videos WHERE id=?", (job["video_id"],)).fetchone()
    _set(conn, job["id"], status="uploading", error=None)
    try:
        token = await get_token(conn, tt, job["account_id"])
        post_info = None
        if job["mode"] == "direct":
            creator = await tt.creator_info(token)
            options = creator.get("privacy_level_options") or []
            if job["privacy_level"] not in options:
                raise TikTokError(
                    f"privacy_level {job['privacy_level']} недоступен для аккаунта; "
                    f"доступно: {', '.join(options) or '—'}"
                )
            post_info = {
                "title": job["title"],
                "privacy_level": job["privacy_level"],
                "disable_comment": bool(job["disable_comment"]),
                "disable_duet": bool(job["disable_duet"]),
                "disable_stitch": bool(job["disable_stitch"]),
            }
        publish_id, upload_url = await tt.init_post(token, video["size"], post_info, job["mode"])
        _set(conn, job["id"], publish_id=publish_id)
        await tt.upload_file(upload_url, video["path"])
        _set(conn, job["id"], status="processing")
    except Exception as e:  # noqa: BLE001 — ошибку показываем в панели
        log.warning("job %s failed: %s", job["id"], e)
        _set(conn, job["id"], status="failed", error=str(e))


async def poll_job(conn: sqlite3.Connection, tt: TikTokClient, job: sqlite3.Row) -> None:
    try:
        token = await get_token(conn, tt, job["account_id"])
        data = await tt.fetch_status(token, job["publish_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("poll %s: %s", job["id"], e)
        return
    status = data.get("status", "")
    if status in STATUS_DONE:
        _set(conn, job["id"], status="done", error=None)
    elif status in STATUS_FAILED:
        _set(conn, job["id"], status="failed", error=data.get("fail_reason") or status)


async def tick(conn: sqlite3.Connection, tt: TikTokClient) -> None:
    due = conn.execute(
        "SELECT * FROM jobs WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at",
        (now(),),
    ).fetchall()
    for job in due:
        await publish_job(conn, tt, job)
    for job in conn.execute("SELECT * FROM jobs WHERE status='processing'").fetchall():
        await poll_job(conn, tt, job)


async def run_forever(conn: sqlite3.Connection, tt: TikTokClient, interval: int) -> None:
    # Задания, прерванные перезапуском во время загрузки, возвращаем в очередь
    conn.execute("UPDATE jobs SET status='scheduled' WHERE status='uploading'")
    conn.commit()
    while True:
        try:
            await tick(conn, tt)
        except Exception:  # noqa: BLE001
            log.exception("worker tick failed")
        await asyncio.sleep(interval)
