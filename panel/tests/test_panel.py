import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db as dbmod
from app import worker
from app.tiktok import MAX_CHUNK, TikTokClient, TikTokError, chunk_plan, chunk_ranges


def test_chunk_plan_small_file_single_chunk():
    assert chunk_plan(3_000_000) == (3_000_000, 1)
    assert chunk_plan(MAX_CHUNK) == (MAX_CHUNK, 1)


def test_chunk_plan_large_file_last_chunk_absorbs_remainder():
    size = 100 * 1024 * 1024 + 123
    chunk, count = chunk_plan(size)
    assert (chunk, count) == (10 * 1024 * 1024, 10)
    ranges = chunk_ranges(size, chunk, count)
    assert ranges[0] == (0, chunk - 1)
    assert ranges[-1][1] == size - 1
    assert sum(e - s + 1 for s, e in ranges) == size


def test_chunk_plan_rejects_empty():
    with pytest.raises(ValueError):
        chunk_plan(0)


class FakeTikTok:
    """Имитация open.tiktokapis.com для httpx.MockTransport."""

    def __init__(self, privacy=("SELF_ONLY",), final_status="PUBLISH_COMPLETE"):
        self.privacy = list(privacy)
        self.final_status = final_status
        self.uploaded = b""
        self.init_payload = None
        self.refreshed = False

    def ok(self, data):
        return httpx.Response(200, json={"data": data, "error": {"code": "ok", "message": ""}})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v2/oauth/token/":
            self.refreshed = True
            return httpx.Response(200, json={
                "access_token": "new-token", "refresh_token": "new-refresh",
                "expires_in": 86400, "refresh_expires_in": 31536000, "open_id": "u1",
                "scope": "video.publish",
            })
        if p == "/v2/post/publish/creator_info/query/":
            return self.ok({"privacy_level_options": self.privacy})
        if p in ("/v2/post/publish/video/init/", "/v2/post/publish/inbox/video/init/"):
            self.init_payload = json.loads(req.content)
            return self.ok({"publish_id": "pub1", "upload_url": "https://upload.test/x"})
        if req.url.host == "upload.test":
            assert req.headers["Content-Range"].startswith("bytes ")
            self.uploaded += req.content
            return httpx.Response(201)
        if p == "/v2/post/publish/status/fetch/":
            return self.ok({"status": self.final_status, "fail_reason": "spam_risk"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


def setup(tmp_path, fake, expires_in=86400):
    conn = dbmod.connect(tmp_path / "t.db")
    worker.save_tokens(conn, {
        "open_id": "u1", "access_token": "tok", "refresh_token": "ref",
        "expires_in": expires_in, "refresh_expires_in": 31536000,
    }, {"display_name": "Test"})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    conn.execute("INSERT INTO videos (filename,path,size,created_at) VALUES ('v.mp4',?,1000,?)",
                 (str(video), dbmod.now()))
    conn.execute(
        "INSERT INTO jobs (video_id,account_id,title,privacy_level,mode,scheduled_at,updated_at) "
        "VALUES (1,1,'hello #test','SELF_ONLY','direct',?,?)",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), dbmod.now()),
    )
    conn.commit()
    tt = TikTokClient("k", "s", "https://x/cb", http=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return conn, tt


def job(conn):
    return conn.execute("SELECT * FROM jobs WHERE id=1").fetchone()


def test_full_publish_flow(tmp_path):
    fake = FakeTikTok()
    conn, tt = setup(tmp_path, fake)
    asyncio.run(worker.tick(conn, tt))  # публикация + опрос статуса
    assert fake.uploaded == b"x" * 1000
    assert fake.init_payload["post_info"]["title"] == "hello #test"
    assert fake.init_payload["source_info"] == {
        "source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    assert job(conn)["status"] == "done"
    assert job(conn)["publish_id"] == "pub1"


def test_privacy_not_allowed_fails_job(tmp_path):
    fake = FakeTikTok(privacy=["PUBLIC_TO_EVERYONE"])
    conn, tt = setup(tmp_path, fake)
    asyncio.run(worker.tick(conn, tt))
    assert job(conn)["status"] == "failed"
    assert "SELF_ONLY" in job(conn)["error"]
    assert fake.uploaded == b""


def test_tiktok_reports_failure(tmp_path):
    fake = FakeTikTok(final_status="FAILED")
    conn, tt = setup(tmp_path, fake)
    asyncio.run(worker.tick(conn, tt))
    assert job(conn)["status"] == "failed"
    assert job(conn)["error"] == "spam_risk"


def test_expiring_token_is_refreshed(tmp_path):
    fake = FakeTikTok()
    conn, tt = setup(tmp_path, fake, expires_in=60)
    asyncio.run(worker.tick(conn, tt))
    assert fake.refreshed
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    assert acc["access_token"] == "new-token" and acc["display_name"] == "Test"


def test_api_error_raises():
    def handler(req):
        return httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "bad"}})
    tt = TikTokClient("k", "s", "r", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TikTokError, match="access_token_invalid"):
        asyncio.run(tt.creator_info("t"))


def test_web_upload_creates_staggered_jobs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, main

    monkeypatch.setattr(config, "PANEL_PASSWORD", "pw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "p.db")
    monkeypatch.setattr(config, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(config, "WORKER_INTERVAL", 3600)
    with TestClient(main.app) as c:
        assert c.get("/").status_code == 401
        auth = ("admin", "pw")
        conn = main.app.state.db
        for oid in ("a", "b"):
            worker.save_tokens(conn, {"open_id": oid, "access_token": "t", "refresh_token": "r"})
        r = c.post("/upload", auth=auth, follow_redirects=False,
                   files={"video": ("clip.mp4", b"data", "video/mp4")},
                   data={"account_ids": ["1", "2"], "title": "t", "privacy_level": "SELF_ONLY",
                         "mode": "direct", "scheduled_at": "2030-01-01T12:00",
                         "tz_offset": "-180", "interval_min": "30"})
        assert r.status_code == 303
        times = [row[0] for row in conn.execute("SELECT scheduled_at FROM jobs ORDER BY id")]
        assert times == ["2030-01-01T09:00:00+00:00", "2030-01-01T09:30:00+00:00"]
        for page in ("/", "/upload", "/jobs"):
            assert c.get(page, auth=auth).status_code == 200
        bad = c.post("/upload", auth=auth, files={"video": ("x.exe", b"1", "application/x")},
                     data={"account_ids": ["1"]})
        assert bad.status_code == 400
