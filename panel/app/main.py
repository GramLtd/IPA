from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import config
from .db import connect, now
from .tiktok import TikTokClient, TikTokError
from .worker import run_forever, save_tokens

logging.basicConfig(level=logging.INFO)

ALLOWED_EXT = {".mp4", ".mov", ".webm"}
PRIVACY_LEVELS = ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"]
STATUS_RU = {
    "scheduled": "запланировано",
    "uploading": "загрузка",
    "processing": "обработка в TikTok",
    "done": "опубликовано",
    "failed": "ошибка",
}

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["STATUS_RU"] = STATUS_RU
security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.PANEL_PASSWORD:
        raise RuntimeError("Задайте PANEL_PASSWORD в .env")
    config.VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    app.state.db = connect(config.DB_PATH)
    app.state.tt = TikTokClient(config.CLIENT_KEY, config.CLIENT_SECRET, config.REDIRECT_URI)
    task = asyncio.create_task(run_forever(app.state.db, app.state.tt, config.WORKER_INTERVAL))
    yield
    task.cancel()
    await app.state.tt.aclose()
    app.state.db.close()


def require_admin(creds: HTTPBasicCredentials = Depends(security)) -> None:
    ok_user = secrets.compare_digest(creds.username.encode(), config.PANEL_USER.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), config.PANEL_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="TikTok Posting Panel", lifespan=lifespan, dependencies=[Depends(require_admin)])


def db(request: Request):
    return request.app.state.db


def tt(request: Request) -> TikTokClient:
    return request.app.state.tt


# ---------- Аккаунты ----------


@app.get("/")
def accounts_page(request: Request, error: str | None = None):
    rows = db(request).execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"accounts": rows, "error": error, "configured": bool(config.CLIENT_KEY)},
    )


@app.get("/oauth/start")
def oauth_start(request: Request):
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(tt(request).auth_url(state))
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    if error or not code:
        return RedirectResponse(f"/?error={error or 'нет кода авторизации'}", 303)
    if not state or state != request.cookies.get("oauth_state"):
        return RedirectResponse("/?error=неверный state, повторите подключение", 303)
    try:
        tok = await tt(request).exchange_code(code)
        user = await tt(request).user_info(tok["access_token"])
    except TikTokError as e:
        return RedirectResponse(f"/?error={e}", 303)
    save_tokens(db(request), tok, user)
    resp = RedirectResponse("/", 303)
    resp.delete_cookie("oauth_state")
    return resp


@app.post("/accounts/{account_id}/delete")
def delete_account(request: Request, account_id: int):
    conn = db(request)
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()
    return RedirectResponse("/", 303)


# ---------- Загрузка ----------


@app.get("/upload")
def upload_page(request: Request):
    rows = db(request).execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return templates.TemplateResponse(
        request, "upload.html", {"accounts": rows, "privacy_levels": PRIVACY_LEVELS}
    )


@app.post("/upload")
def upload(
    request: Request,
    video: UploadFile = File(...),
    account_ids: list[int] = Form(...),
    title: str = Form(""),
    privacy_level: str = Form("SELF_ONLY"),
    mode: str = Form("direct"),
    scheduled_at: str = Form(""),
    tz_offset: int = Form(0),
    interval_min: int = Form(0),
    disable_comment: bool = Form(False),
    disable_duet: bool = Form(False),
    disable_stitch: bool = Form(False),
):
    ext = Path(video.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Разрешены файлы: {', '.join(sorted(ALLOWED_EXT))}")
    if mode not in ("direct", "inbox") or privacy_level not in PRIVACY_LEVELS:
        raise HTTPException(400, "Неверные параметры")
    if len(title) > 2200:
        raise HTTPException(400, "Описание длиннее 2200 символов")

    # datetime-local приходит без часового пояса; tz_offset в минутах = getTimezoneOffset() браузера
    if scheduled_at:
        local = datetime.fromisoformat(scheduled_at)
        start = (local + timedelta(minutes=tz_offset)).replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc)

    path = config.VIDEO_DIR / f"{uuid.uuid4().hex}{ext}"
    with path.open("wb") as out:
        shutil.copyfileobj(video.file, out, length=1024 * 1024)
    size = path.stat().st_size
    if size == 0:
        path.unlink()
        raise HTTPException(400, "Пустой файл")

    conn = db(request)
    cur = conn.execute(
        "INSERT INTO videos (filename, path, size, created_at) VALUES (?,?,?,?)",
        (video.filename, str(path), size, now()),
    )
    video_id = cur.lastrowid
    for i, account_id in enumerate(account_ids):
        when = start + timedelta(minutes=interval_min * i)
        conn.execute(
            """INSERT INTO jobs (video_id, account_id, title, privacy_level, disable_comment,
                                 disable_duet, disable_stitch, mode, scheduled_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                video_id,
                account_id,
                title,
                privacy_level,
                int(disable_comment),
                int(disable_duet),
                int(disable_stitch),
                mode,
                when.isoformat(timespec="seconds"),
                now(),
            ),
        )
    conn.commit()
    return RedirectResponse("/jobs", 303)


# ---------- Задания ----------


@app.get("/jobs")
def jobs_page(request: Request):
    rows = db(request).execute(
        """SELECT j.*, a.display_name, v.filename FROM jobs j
           JOIN accounts a ON a.id = j.account_id
           JOIN videos v ON v.id = j.video_id
           ORDER BY j.id DESC LIMIT 300"""
    ).fetchall()
    return templates.TemplateResponse(request, "jobs.html", {"jobs": rows})


@app.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int):
    conn = db(request)
    conn.execute(
        "UPDATE jobs SET status='scheduled', error=NULL, publish_id=NULL, scheduled_at=?, "
        "updated_at=? WHERE id=? AND status='failed'",
        (now(), now(), job_id),
    )
    conn.commit()
    return RedirectResponse("/jobs", 303)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int):
    conn = db(request)
    conn.execute("DELETE FROM jobs WHERE id=? AND status='scheduled'", (job_id,))
    conn.commit()
    return RedirectResponse("/jobs", 303)
