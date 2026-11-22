"""Railway bucket storage, dead campaign assets and expired YouTube cookies (no network, no database).
Run: cd posts && python -m pytest tests/test_storage.py"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402
from app.integrations import blob  # noqa: E402
from app.workers import render  # noqa: E402


def _bucket(monkeypatch):
    for k, val in {"BUCKET_NAME": "clips-x", "BUCKET_ENDPOINT": "https://storage.example", "BUCKET_REGION": "auto",
                   "BUCKET_ACCESS_KEY_ID": "id", "BUCKET_SECRET_ACCESS_KEY": "secret",
                   "PUBLIC_API_URL": "https://api.example"}.items():
        monkeypatch.setattr(config, k, val)
    blob._s3.cache_clear()


def test_bucket_upload_returns_our_media_url(monkeypatch, tmp_path):
    _bucket(monkeypatch)
    put = {}

    class S3:
        def put_object(self, **kw):
            put.update(kw, Body=kw["Body"].read())

    monkeypatch.setattr(blob, "_s3", lambda: S3())
    f = tmp_path / "final.mp4"
    f.write_bytes(b"video")
    url = blob.upload(f, "clips/strogo-12.mp4", "video/mp4")
    assert put["Bucket"] == "clips-x" and put["ContentType"] == "video/mp4" and put["Body"] == b"video"
    assert put["Key"].startswith("clips/strogo-12-") and put["Key"].endswith(".mp4")
    assert url == f"https://api.example/media/{put['Key']}"


def test_signed_link_is_s3v4(monkeypatch):
    _bucket(monkeypatch)
    link = blob.signed("clips/a-1-abc.mp4")
    assert "X-Amz-Signature=" in link and "clips/a-1-abc.mp4" in link and "X-Amz-Expires=518400" in link
    blob._s3.cache_clear()


def test_dead_banner_pauses_campaign(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    logs = []
    monkeypatch.setattr(render.db, "log", lambda *a: logs.append(a))
    render._asset_warned.clear()

    def boom(cmd, **kw):
        raise RuntimeError("python failed: HTTP Error 403: Forbidden")

    monkeypatch.setattr(render, "sh", boom)
    c = {"id": 9, "name": "BigBalls", "overlay_url": None, "banner_url": "https://old.blob/b.mp4"}
    assert render.assets_ready(c) is False
    assert render.assets_ready(c) is False
    assert len(logs) == 1 and "paused" in logs[0][1]      # warned once, not every run
    assert not list((tmp_path / "assets").glob("*.part"))


def test_cached_banner_needs_no_download(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    monkeypatch.setattr(render, "sh", lambda cmd, **kw: Path(cmd[-1]).write_bytes(b"mp4"))
    c = {"id": 3, "name": "X", "overlay_url": "https://a/o.png", "banner_url": "https://a/b.mp4"}
    assert render.assets_ready(c)
    monkeypatch.setattr(render, "sh", lambda *a, **k: pytest.fail("downloaded again"))
    assert render.assets_ready(c) and render.banner_file("https://a/b.mp4").read_bytes() == b"mp4"


def test_expired_cookies_mean_blocked_not_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    monkeypatch.setattr(config, "YTDLP_COOKIES", "# Netscape HTTP Cookie File\n")

    def expired(cmd, **kw):
        raise RuntimeError("python failed: The provided YouTube account cookies are no longer valid.")

    monkeypatch.setattr(render, "sh", expired)
    with pytest.raises(render.SourceBlocked):
        render.download_trend(tmp_path, "https://www.youtube.com/shorts/x", tmp_path / "src.mp4")

    monkeypatch.setattr(render, "sh", lambda cmd, **kw: (_ for _ in ()).throw(RuntimeError("Video unavailable")))
    with pytest.raises(RuntimeError) as e:
        render.download_trend(tmp_path, "https://www.youtube.com/shorts/x", tmp_path / "src.mp4")
    assert not isinstance(e.value, render.SourceBlocked)


def test_cookie_file_kept_until_new_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path)
    monkeypatch.setattr(config, "YTDLP_COOKIES", "one")
    f = render.cookie_file()
    f.write_text("one, refreshed by yt-dlp")
    assert render.cookie_file().read_text() == "one, refreshed by yt-dlp"
    monkeypatch.setattr(config, "YTDLP_COOKIES", "two")
    assert render.cookie_file().read_text() == "two"


def test_restore_rerenders_newest_dead_clip_and_marks_failures(monkeypatch):
    _bucket(monkeypatch)
    calls, marked = [], []

    def q(sql, params=None):
        if sql.lstrip().startswith("SELECT cl.id"):
            assert params == (render.DEAD_STORE,)
            return [{"id": 7, "campaign_id": 1}, {"id": 5, "campaign_id": 1}]
        marked.append(params)
        return []

    monkeypatch.setattr(render.db, "q", q)
    monkeypatch.setattr(render.db, "one", lambda sql, p=None: {"id": 1, "name": "X"})
    monkeypatch.setattr(render, "assets_ready", lambda c: True)

    def boom(cid):
        calls.append(cid)
        raise RuntimeError("source gone")

    monkeypatch.setattr(render, "rerender", boom)
    assert render.restore_one() is True
    assert calls == [7] and marked and marked[0][0].startswith("restore failed: source gone")
    blob._s3.cache_clear()
