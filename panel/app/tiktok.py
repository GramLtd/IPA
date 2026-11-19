"""Минимальный клиент официального TikTok API: OAuth v2 и Content Posting API.

Документация:
  https://developers.tiktok.com/doc/oauth-user-access-token-management
  https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
SCOPES = "user.info.basic,video.publish,video.upload"

MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024

# Финальные статусы публикации
STATUS_DONE = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}
STATUS_FAILED = {"FAILED"}


class TikTokError(Exception):
    pass


def chunk_plan(size: int, chunk: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """Возвращает (chunk_size, total_chunk_count) по правилам TikTok.

    Файл до 64 МБ можно отправить одним куском. Иначе берём куски по `chunk`
    байт, а последний кусок забирает остаток (он может быть больше, до 128 МБ).
    """
    if size <= 0:
        raise ValueError("пустой файл")
    if size <= MAX_CHUNK:
        return size, 1
    if not MIN_CHUNK <= chunk <= MAX_CHUNK:
        raise ValueError("chunk должен быть от 5 до 64 МБ")
    return chunk, size // chunk


def chunk_ranges(size: int, chunk: int, count: int) -> list[tuple[int, int]]:
    """Диапазоны байтов (start, end включительно) для каждого куска."""
    ranges = []
    for i in range(count):
        start = i * chunk
        end = size - 1 if i == count - 1 else start + chunk - 1
        ranges.append((start, end))
    return ranges


class TikTokClient:
    def __init__(
        self,
        client_key: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx.AsyncClient | None = None,
    ):
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, read=300))

    async def aclose(self) -> None:
        await self.http.aclose()

    # ---------- OAuth ----------

    def auth_url(self, state: str) -> str:
        params = {
            "client_key": self.client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        data = {"client_key": self.client_key, "client_secret": self.client_secret, **data}
        r = await self.http.post(
            f"{API_BASE}/v2/oauth/token/",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = r.json()
        if r.status_code != 200 or "access_token" not in body:
            raise TikTokError(
                f"OAuth: {body.get('error')} {body.get('error_description', '')}".strip()
            )
        return body

    async def exchange_code(self, code: str) -> dict[str, Any]:
        return await self._token_request(
            {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri}
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    # ---------- API ----------

    async def _api(self, method: str, path: str, token: str, **kw) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        if "json" in kw:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        r = await self.http.request(method, f"{API_BASE}{path}", headers=headers, **kw)
        try:
            body = r.json()
        except ValueError:
            raise TikTokError(f"{path}: HTTP {r.status_code}") from None
        err = body.get("error") or {}
        if r.status_code != 200 or err.get("code", "ok") != "ok":
            raise TikTokError(f"{path}: {err.get('code')} {err.get('message', '')}".strip())
        return body.get("data") or {}

    async def user_info(self, token: str) -> dict[str, Any]:
        data = await self._api(
            "GET",
            "/v2/user/info/",
            token,
            params={"fields": "open_id,union_id,avatar_url,display_name"},
        )
        return data.get("user", {})

    async def creator_info(self, token: str) -> dict[str, Any]:
        """Настройки автора: доступные privacy_level, лимит длительности и т.д."""
        return await self._api("POST", "/v2/post/publish/creator_info/query/", token, json={})

    async def init_post(
        self, token: str, size: int, post_info: dict[str, Any] | None, mode: str = "direct"
    ) -> tuple[str, str]:
        """Начинает публикацию. mode='direct' публикует сразу,
        mode='inbox' отправляет черновик во «Входящие» приложения TikTok."""
        chunk, count = chunk_plan(size)
        payload: dict[str, Any] = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk,
                "total_chunk_count": count,
            }
        }
        if mode == "direct":
            path = "/v2/post/publish/video/init/"
            payload["post_info"] = post_info or {}
        elif mode == "inbox":
            path = "/v2/post/publish/inbox/video/init/"
        else:
            raise ValueError(f"неизвестный режим {mode}")
        data = await self._api("POST", path, token, json=payload)
        return data["publish_id"], data["upload_url"]

    async def upload_file(self, upload_url: str, path: str) -> None:
        size = os.path.getsize(path)
        chunk, count = chunk_plan(size)
        with open(path, "rb") as f:
            for start, end in chunk_ranges(size, chunk, count):
                f.seek(start)
                part = f.read(end - start + 1)
                r = await self.http.put(
                    upload_url,
                    content=part,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(part)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                )
                if r.status_code not in (200, 201, 206):
                    raise TikTokError(f"upload: HTTP {r.status_code} {r.text[:200]}")

    async def fetch_status(self, token: str, publish_id: str) -> dict[str, Any]:
        return await self._api(
            "POST", "/v2/post/publish/status/fetch/", token, json={"publish_id": publish_id}
        )
